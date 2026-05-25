"""
Settlement layer — polls open positions against Polymarket resolution data
and closes them with final P&L when markets resolve.

Three close triggers:
  1. Market resolved  — YES price hits 1.0 or 0.0 (definitive outcome)
  2. Take profit      — price has moved ≥ TAKE_PROFIT_PCT toward fair price
  3. Stop loss        — position is down ≥ STOP_LOSS_PCT of stake

Runs as part of the main monitor loop every SETTLEMENT_INTERVAL seconds.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from alerts.notifier import send_telegram_alert
from config import (
    POLYMARKET_GAMMA_API,
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
)
from db.database import get_db
from db.models import Position, ExecutedTrade, Market

logger = logging.getLogger(__name__)

RESOLVED_THRESHOLD = 0.98   # YES price >= this → resolved YES; <= (1-this) → resolved NO


class SettlementEngine:
    def __init__(self, poly_scraper=None):
        self.scraper = poly_scraper

    async def run_cycle(self):
        """Check all open positions for resolution, take-profit, or stop-loss."""
        open_positions = self._load_open_positions()
        if not open_positions:
            return

        logger.info("Settlement check: %d open positions", len(open_positions))
        settled = 0

        for pos in open_positions:
            market_price = self._get_current_yes_price(pos["market_id"])
            if market_price is None:
                continue

            close_reason, exit_price = self._check_close_condition(pos, market_price)
            if close_reason:
                pnl = self._calculate_pnl(pos, exit_price)
                self._close_position(pos["id"], exit_price, pnl, close_reason)
                self._send_settlement_alert(pos, exit_price, pnl, close_reason)
                settled += 1

        if settled:
            logger.info("Settled %d positions", settled)

    # ─── Close condition checks ───────────────────────────────

    def _check_close_condition(
        self, pos: dict, current_yes_price: float
    ) -> tuple[Optional[str], Optional[float]]:
        """
        Returns (reason, exit_price) if position should be closed, else (None, None).
        """
        side = pos["side"]
        entry = pos["entry_price"]

        # Current price of the position's side
        current_side_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)

        # 1. Market resolved
        if current_yes_price >= RESOLVED_THRESHOLD:
            exit_price = 1.0 if side == "YES" else 0.0
            return "resolved", exit_price
        if current_yes_price <= (1.0 - RESOLVED_THRESHOLD):
            exit_price = 0.0 if side == "YES" else 1.0
            return "resolved", exit_price

        # 2. Take profit — price moved ≥ TAKE_PROFIT_PCT toward fair value
        if pos.get("implied_fair_price") and entry > 0:
            if side == "YES":
                fair_side = pos["implied_fair_price"]
            else:
                fair_side = 1.0 - pos["implied_fair_price"]

            target_move = abs(fair_side - entry) * TAKE_PROFIT_PCT
            actual_move = current_side_price - entry  # positive = moving our way

            if actual_move >= target_move > 0:
                return "take_profit", current_side_price

        # 3. Stop loss — position down ≥ STOP_LOSS_PCT
        if entry > 0:
            loss_pct = (entry - current_side_price) / entry
            if loss_pct >= STOP_LOSS_PCT:
                return "stop_loss", current_side_price

        # 4. Time-based stop — cut losers that haven't recovered after 8 hours
        if pos.get("opened_at") and entry > 0:
            age_hours = (datetime.utcnow() - pos["opened_at"]).total_seconds() / 3600
            if age_hours > 8:
                loss_pct = (entry - current_side_price) / entry
                if loss_pct > 0.10:
                    return "stop_loss", current_side_price

        return None, None

    # ─── P&L calculation ─────────────────────────────────────

    def _calculate_pnl(self, pos: dict, exit_price: float) -> float:
        """
        P&L = (exit_price - entry_price) * shares
        Positive = profit, negative = loss.
        """
        entry = pos["entry_price"]
        shares = pos["shares"]
        if not shares or not entry:
            return 0.0
        return round((exit_price - entry) * shares, 4)

    # ─── DB helpers ───────────────────────────────────────────

    def _load_open_positions(self) -> list[dict]:
        with get_db() as db:
            rows = (
                db.query(
                    Position.id,
                    Position.market_id,
                    Position.side,
                    Position.entry_price,
                    Position.shares,
                    Position.size_usdc,
                    Position.kelly_fraction,
                    Position.edge_score,
                    Position.is_paper,
                    Position.opened_at,
                    Position.clob_order_id,
                )
                .filter_by(status="open")
                .all()
            )

            # Also get implied_fair_price from matching edge signal
            result = []
            for row in rows:
                # Look up latest fair price for this market
                from db.models import EdgeSignal
                sig = (
                    db.query(EdgeSignal.implied_fair_price)
                    .filter_by(market_id=row.market_id)
                    .order_by(EdgeSignal.detected_at.desc())
                    .first()
                )
                result.append({
                    "id":                row.id,
                    "market_id":         row.market_id,
                    "side":              row.side,
                    "entry_price":       row.entry_price,
                    "shares":            row.shares,
                    "size_usdc":         row.size_usdc,
                    "kelly_fraction":    row.kelly_fraction,
                    "edge_score":        row.edge_score,
                    "is_paper":          row.is_paper,
                    "opened_at":         row.opened_at,
                    "clob_order_id":     row.clob_order_id,
                    "implied_fair_price": sig[0] if sig else None,
                })
            return result

    def _get_current_yes_price(self, market_id: str) -> Optional[float]:
        """Fetch latest YES price from DB (updated each market cycle)."""
        with get_db() as db:
            row = db.query(Market.yes_price).filter_by(id=market_id).first()
            return row[0] if row else None

    def _close_position(
        self,
        position_id: int,
        exit_price: float,
        pnl: float,
        reason: str,
    ):
        """Mark position as closed and update the trade record."""
        with get_db() as db:
            pos = db.query(Position).filter_by(id=position_id).first()
            if not pos:
                return

            pos.status = "closed"
            pos.exit_price = exit_price
            pos.realized_pnl = pnl
            pos.exit_reason = reason
            pos.closed_at = datetime.utcnow()

            # Update the most recent ExecutedTrade for this position
            trade = (
                db.query(ExecutedTrade)
                .filter_by(market_id=pos.market_id, side=pos.side)
                .order_by(ExecutedTrade.executed_at.desc())
                .first()
            )
            if trade:
                trade.fill_price = exit_price
                trade.realized_pnl = pnl

    # ─── Telegram alert ───────────────────────────────────────

    def _send_settlement_alert(
        self,
        pos: dict,
        exit_price: float,
        pnl: float,
        reason: str,
    ):
        pnl_icon = "✅" if pnl >= 0 else "❌"
        reason_labels = {
            "resolved":    "Market Resolved",
            "take_profit": "Take Profit Hit",
            "stop_loss":   "Stop Loss Hit",
        }
        mode = "PAPER" if pos["is_paper"] else "LIVE"
        hold_hours = (
            (datetime.utcnow() - pos["opened_at"]).total_seconds() / 3600
            if pos["opened_at"] else 0
        )
        pnl_pct = (pnl / pos["size_usdc"] * 100) if pos["size_usdc"] else 0

        msg = (
            f"{pnl_icon} <b>[{mode}] POSITION CLOSED — {reason_labels.get(reason, reason)}</b>\n"
            f"\n"
            f"Side: <b>{pos['side']}</b> | Entry: {pos['entry_price']:.3f} → Exit: {exit_price:.3f}\n"
            f"Size: ${pos['size_usdc']:,.2f} | Shares: {pos['shares']:.2f}\n"
            f"P&amp;L: <b>${pnl:+,.2f}</b> ({pnl_pct:+.1f}%)\n"
            f"Held: {hold_hours:.1f}h | Kelly: {pos['kelly_fraction']:.1%}\n"
            f"Market: {pos['market_id'][:30]}"
        )
        send_telegram_alert(msg)
        logger.info(
            "[%s] CLOSED %s %s entry=%.3f exit=%.3f pnl=$%+.2f reason=%s",
            mode, pos["side"], pos["market_id"][:20],
            pos["entry_price"], exit_price, pnl, reason,
        )

    # ─── Portfolio summary ────────────────────────────────────

    def portfolio_report(self) -> dict:
        """Aggregate P&L across all closed positions."""
        with get_db() as db:
            closed = db.query(
                Position.realized_pnl,
                Position.size_usdc,
                Position.exit_reason,
                Position.side,
            ).filter_by(status="closed").all()

            open_count = db.query(Position).filter_by(status="open").count()
            open_deployed = sum(
                s or 0
                for (s,) in db.query(Position.size_usdc).filter_by(status="open").all()
            )

        if not closed:
            return {"open": open_count, "closed": 0, "realized_pnl": 0}

        total_pnl = sum(r or 0 for (r, *_) in closed)
        wins = [(r, s) for r, s, *_ in closed if (r or 0) > 0]
        losses = [(r, s) for r, s, *_ in closed if (r or 0) <= 0]
        total_staked = sum(s or 0 for _, s, *_ in closed)
        roi = (total_pnl / total_staked * 100) if total_staked else 0

        return {
            "open_positions":  open_count,
            "deployed_usdc":   round(open_deployed, 2),
            "closed_positions": len(closed),
            "realized_pnl":    round(total_pnl, 2),
            "roi_pct":         round(roi, 2),
            "win_rate":        round(len(wins) / len(closed), 3) if closed else 0,
            "wins":            len(wins),
            "losses":          len(losses),
            "avg_win":         round(sum(r for r, _ in wins) / len(wins), 2) if wins else 0,
            "avg_loss":        round(sum(r for r, _ in losses) / len(losses), 2) if losses else 0,
        }
