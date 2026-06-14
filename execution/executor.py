"""
Order executor — paper trading by default, live via Polymarket CLOB API.

Paper trading mode (PAPER_TRADING=true in .env):
  Simulates all orders, records them to DB, sends Telegram alerts.
  No real money moved.

Live trading mode (PAPER_TRADING=false):
  Places real limit orders on Polymarket CLOB via py-clob-client.
  Requires POLYMARKET_API_KEY + POLYMARKET_PRIVATE_KEY in .env.
"""

import logging
from datetime import datetime
from typing import Optional

from alerts.notifier import send_telegram_alert
from config import (
    PAPER_TRADING,
    BANKROLL_USDC,
    MAX_KELLY_FRACTION,
    HALF_KELLY,
    MAX_POSITION_USDC,
    MIN_POSITION_USDC,
    MIN_EDGE_SCORE_TO_TRADE,
    MAX_EDGE_SCORE_TO_TRADE,
    MIN_CONFIDENCE_TO_TRADE,
    MIN_PRICE_GAP_TO_TRADE,
    MAX_OPEN_POSITIONS,
    MAX_DAILY_LOSS_PCT,
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    POLYMARKET_CLOB_API,
    DISABLED_SIGNAL_TYPES,
)
from db.database import get_db
from db.models import Position, ExecutedTrade
from execution.kelly import size_position, KellyResult
from execution.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self):
        self.paper = PAPER_TRADING
        self.risk = RiskManager(
            bankroll_usdc=BANKROLL_USDC,
            max_open_positions=MAX_OPEN_POSITIONS,
            max_position_usdc=MAX_POSITION_USDC,
            max_daily_loss_pct=MAX_DAILY_LOSS_PCT,
            min_edge_score=MIN_EDGE_SCORE_TO_TRADE,
        )
        self._clob_client = None
        if not self.paper:
            self._init_clob()

        mode = "PAPER" if self.paper else "LIVE"
        logger.info("Executor initialized in %s mode (bankroll $%.0f)", mode, BANKROLL_USDC)

    def _init_clob(self):
        """Initialize Polymarket CLOB client for live trading."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON
            from config import POLYMARKET_PRIVATE_KEY
            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE,
            ) if POLYMARKET_API_KEY else None
            self._clob_client = ClobClient(
                host=POLYMARKET_CLOB_API,
                chain_id=POLYGON,
                key=POLYMARKET_PRIVATE_KEY,
                creds=creds,
                signature_type=1,   # L1 wallet signing
            )
            logger.info("CLOB client initialized for live trading")
        except ImportError:
            logger.error(
                "py-clob-client not installed — falling back to paper trading. "
                "Run: pip install py-clob-client"
            )
            self.paper = True
        except Exception as exc:
            logger.error("CLOB client init failed: %s — falling back to paper trading", exc)
            self.paper = True

    def evaluate_and_execute(self, signal: dict):
        """
        Given an edge signal, compute Kelly size, run risk checks, and execute.
        Entry point called from main loop after edge detection.
        """
        market_id  = signal.get("market_id")
        direction  = signal.get("direction")          # "YES" or "NO"
        fair_price = signal.get("implied_fair_price")
        curr_price = signal.get("current_price")
        edge_score = signal.get("edge_score", 0)

        if fair_price is None or curr_price is None:
            logger.debug("SKIP bad-data %s fair=%s price=%s", market_id, fair_price, curr_price)
            return
        if edge_score < MIN_EDGE_SCORE_TO_TRADE:
            return
        if edge_score > MAX_EDGE_SCORE_TO_TRADE:
            logger.debug("SKIP over-score %s score=%.1f (max=%.0f)", market_id[:16], edge_score, MAX_EDGE_SCORE_TO_TRADE)
            return

        signal_type = signal.get("signal_type", "")
        if signal_type in DISABLED_SIGNAL_TYPES:
            logger.debug("SKIP disabled-signal %s type=%s", market_id[:16], signal_type)
            return

        # Require at least one directional signal (sentiment/whale/arb) before checking direction.
        # sub_scores values are 0–1 raw fractions; multiply by weights (same as edge detector)
        # to get edge-point contribution out of 100. Require >= 5 edge points from directional signals.
        sub = signal.get("sub_scores") or {}
        directional_pts = (
            (sub.get("price_sentiment", 0) or 0) * 20 +
            (sub.get("whale", 0) or 0) * 15 +
            (sub.get("arb", 0) or 0) * 15 +
            (sub.get("volume", 0) or 0) * 35
        )
        if directional_pts < 10:
            logger.debug(
                "SKIP no-direction %s (dir_pts=%.1f sentiment=%.2f whale=%.2f arb=%.2f vol=%.2f)",
                market_id[:16], directional_pts,
                sub.get("price_sentiment", 0) or 0,
                sub.get("whale", 0) or 0,
                sub.get("arb", 0) or 0,
                sub.get("volume", 0) or 0,
            )
            return

        if direction not in ("YES", "NO"):
            logger.debug("SKIP neutral %s", market_id[:16])
            return

        # Require at least 2 signals agreeing on direction (confidence > 0.5)
        confidence = signal.get("confidence", 0)
        if confidence < MIN_CONFIDENCE_TO_TRADE:
            logger.info(
                "SKIP low-conf %s dir=%s conf=%.2f",
                market_id[:16], direction, confidence,
            )
            return

        # Require meaningful price gap between fair and market price
        price_gap = abs(fair_price - curr_price)
        if price_gap < MIN_PRICE_GAP_TO_TRADE:
            logger.info(
                "SKIP thin-gap %s gap=%.4f (min=%.3f)", market_id[:16], price_gap, MIN_PRICE_GAP_TO_TRADE
            )
            return

        # Skip markets resolving within 4 hours — price may already reflect known outcome
        end_date = signal.get("end_date")
        if end_date:
            from datetime import timezone
            now = datetime.utcnow()
            ed = end_date.replace(tzinfo=None) if hasattr(end_date, 'tzinfo') and end_date.tzinfo else end_date
            if (ed - now).total_seconds() < 4 * 3600:
                logger.info("SKIP near-expiry %s (closes in %.1fh)", market_id[:16], (ed - now).total_seconds() / 3600)
                return

        # Skip markets outside the 0.35–0.65 fair-payoff zone.
        # Applied symmetrically: for YES bets check YES price; for NO bets check NO price.
        # At YES>0.65 the break-even win rate exceeds what our signals achieve (~65% needed,
        # ~49% observed), and at YES<0.35 the same problem applies to NO bets (NO>0.65).
        # Tighter than the old 0.30–0.70 range based on observed ROI by price bucket.
        entry_price_of_side = curr_price if direction == "YES" else (1.0 - curr_price)
        if entry_price_of_side < 0.35 or entry_price_of_side > 0.65:
            logger.info(
                "SKIP bad-payoff %s dir=%s side_price=%.4f",
                market_id[:16], direction, entry_price_of_side,
            )
            return

        # Skip YES price 0.50-0.59 range — 36.6% WR and -7.8% ROI historically.
        # The 0.60-0.69 range (50% WR, +2.4% ROI) is where YES-side edge lives.
        if direction == "YES" and 0.50 <= curr_price < 0.60:
            logger.info("SKIP poor-yes-range %s price=%.4f", market_id[:16], curr_price)
            return

        # ── Kelly sizing ──────────────────────────────────────
        kelly = size_position(
            true_prob     = fair_price,
            market_price  = curr_price,
            direction     = direction,
            bankroll_usdc = BANKROLL_USDC,
            half_kelly    = HALF_KELLY,
            max_fraction  = MAX_KELLY_FRACTION,
            max_usdc      = MAX_POSITION_USDC,
            min_usdc      = MIN_POSITION_USDC,
        )

        if kelly is None:
            logger.info(
                "No Kelly edge: %s dir=%s price=%.4f fair=%.4f (edge too small)",
                market_id[:16], direction, curr_price, fair_price,
            )
            return

        # ── Risk checks ───────────────────────────────────────
        approved, reason = self.risk.check(signal, kelly.usdc_size)
        if not approved:
            logger.info("Trade blocked [%s]: %s", market_id[:16], reason)
            return

        # ── Execute ───────────────────────────────────────────
        if self.paper:
            self._paper_execute(signal, kelly)
        else:
            self._live_execute(signal, kelly)

    # ─── Paper Trading ────────────────────────────────────────

    def _paper_execute(self, signal: dict, kelly: KellyResult):
        """Record a simulated trade and notify via Telegram."""
        market_id = signal["market_id"]
        token_id  = signal.get(
            "yes_token_id" if kelly.side == "YES" else "no_token_id"
        )

        with get_db() as db:
            position = Position(
                market_id    = market_id,
                token_id     = token_id,
                side         = kelly.side,
                entry_price  = kelly.market_price,
                size_usdc    = kelly.usdc_size,
                shares       = round(kelly.usdc_size / kelly.market_price, 4),
                kelly_fraction = kelly.scaled_fraction,
                edge_score   = signal.get("edge_score"),
                status       = "open",
                is_paper     = True,
            )
            db.add(position)

            trade = ExecutedTrade(
                market_id    = market_id,
                token_id     = token_id,
                side         = kelly.side,
                action       = "BUY",
                price        = kelly.market_price,
                size_usdc    = kelly.usdc_size,
                shares       = position.shares,
                kelly_fraction = kelly.scaled_fraction,
                edge_score   = signal.get("edge_score"),
                is_paper     = True,
                signal_type  = signal.get("signal_type"),
            )
            db.add(trade)

        logger.info(
            "[PAPER] BUY %s %s @ %.3f | $%.2f | Kelly %.1f%% | EV %.3f | Score %.1f",
            kelly.side, market_id[:20], kelly.market_price,
            kelly.usdc_size, kelly.scaled_fraction * 100,
            kelly.expected_value, signal.get("edge_score", 0),
        )

        self._send_trade_alert(signal, kelly, paper=True)

    # ─── Live Trading ─────────────────────────────────────────

    def _live_execute(self, signal: dict, kelly: KellyResult):
        """Place a real limit order on Polymarket CLOB."""
        if not self._clob_client:
            logger.error("CLOB client not available for live trade")
            return

        token_id = signal.get(
            "yes_token_id" if kelly.side == "YES" else "no_token_id"
        )
        if not token_id:
            logger.warning("No token_id for market %s side %s", signal["market_id"], kelly.side)
            return

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            order_args = OrderArgs(
                token_id   = token_id,
                price      = round(kelly.market_price, 4),
                size       = round(kelly.usdc_size / kelly.market_price, 2),  # shares
                side       = kelly.side,
                order_type = OrderType.GTC,
            )
            resp = self._clob_client.create_and_post_order(order_args)
            order_id = resp.get("orderID") or resp.get("id", "unknown")

            with get_db() as db:
                position = Position(
                    market_id      = signal["market_id"],
                    token_id       = token_id,
                    side           = kelly.side,
                    entry_price    = kelly.market_price,
                    size_usdc      = kelly.usdc_size,
                    shares         = round(kelly.usdc_size / kelly.market_price, 4),
                    kelly_fraction = kelly.scaled_fraction,
                    edge_score     = signal.get("edge_score"),
                    clob_order_id  = order_id,
                    status         = "open",
                    is_paper       = False,
                )
                db.add(position)

                trade = ExecutedTrade(
                    market_id      = signal["market_id"],
                    token_id       = token_id,
                    side           = kelly.side,
                    action         = "BUY",
                    price          = kelly.market_price,
                    size_usdc      = kelly.usdc_size,
                    shares         = position.shares,
                    kelly_fraction = kelly.scaled_fraction,
                    edge_score     = signal.get("edge_score"),
                    clob_order_id  = order_id,
                    is_paper       = False,
                    signal_type    = signal.get("signal_type"),
                )
                db.add(trade)

            logger.info(
                "[LIVE] BUY %s %s @ %.3f | $%.2f | order_id=%s",
                kelly.side, signal["market_id"][:20],
                kelly.market_price, kelly.usdc_size, order_id,
            )
            self._send_trade_alert(signal, kelly, paper=False)

        except Exception as exc:
            logger.error("Live order failed for %s: %s", signal["market_id"], exc)

    # ─── Telegram Alert ───────────────────────────────────────

    def _send_trade_alert(self, signal: dict, kelly: KellyResult, paper: bool):
        mode_tag = "PAPER" if paper else "LIVE"
        dir_icon = "\U0001f7e2" if kelly.side == "YES" else "\U0001f534"
        question = (signal.get('question', '') or '')[:80].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        msg = (
            f"{dir_icon} <b>[{mode_tag}] TRADE EXECUTED</b>\n"
            f"Market: {question}\n"
            f"Category: {signal.get('category', '?')}\n"
            f"\n"
            f"Side: <b>{kelly.side}</b> @ {kelly.market_price:.3f}\n"
            f"Size: <b>${kelly.usdc_size:,.2f} USDC</b>\n"
            f"Kelly fraction: {kelly.scaled_fraction:.1%}\n"
            f"\n"
            f"Edge score: {signal.get('edge_score', 0):.1f}/100\n"
            f"Fair price: {signal.get('implied_fair_price', 0):.3f} "
            f"vs market {signal.get('current_price', 0):.3f}\n"
            f"Expected value: {kelly.expected_value:+.3f} per $1\n"
            f"Edge: {kelly.edge_pct:+.1f}%\n"
            f"Signal: {signal.get('signal_type', '?')}\n"
            f"Notes: {signal.get('notes', '')}"
        )
        send_telegram_alert(msg)

    # ─── Portfolio Summary ────────────────────────────────────

    def portfolio_summary(self) -> dict:
        """Return a snapshot of current portfolio state."""
        with get_db() as db:
            open_positions = db.query(Position).filter_by(status="open").all()
            all_trades = db.query(ExecutedTrade).all()

            total_deployed = sum(p.size_usdc for p in open_positions)
            total_realized_pnl = sum(t.realized_pnl or 0 for t in all_trades)
            total_trades = len(all_trades)
            wins = sum(1 for t in all_trades if (t.realized_pnl or 0) > 0)

            return {
                "bankroll_usdc": BANKROLL_USDC,
                "deployed_usdc": round(total_deployed, 2),
                "available_usdc": round(BANKROLL_USDC - total_deployed, 2),
                "open_positions": len(open_positions),
                "total_trades": total_trades,
                "realized_pnl": round(total_realized_pnl, 2),
                "win_rate": round(wins / total_trades, 3) if total_trades else 0,
                "mode": "paper" if self.paper else "live",
            }
