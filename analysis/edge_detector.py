"""
Edge detector — combines price, sentiment, whale, and arb signals
to find statistically significant outliers with actionable edges.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from sqlalchemy import func

from config import ZSCORE_THRESHOLD
from db.database import get_db
from db.models import (
    Market, MarketSnapshot, SentimentRecord,
    WhaleTrade, ArbitrageOpportunity, EdgeSignal
)

logger = logging.getLogger(__name__)

# Weights for composite edge score (must sum to 1.0)
WEIGHT_PRICE_SENTIMENT = 0.20
WEIGHT_WHALE            = 0.15
WEIGHT_VOLUME_ANOMALY   = 0.35
WEIGHT_ARB              = 0.05
WEIGHT_BASE_RATE        = 0.25

# SQLite max variables per query — stay under the 999 default limit
_CHUNK = 500


def _chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


class EdgeDetector:

    def run(self) -> list[dict]:
        """
        Full edge detection cycle. Returns list of new EdgeSignal dicts.
        Persists signals to DB automatically.

        Uses 5 bulk queries (one per sub-signal data source) instead of
        opening a new DB session per market, which previously caused
        thousands of concurrent connections and SQLite I/O errors.
        """
        with get_db() as db:
            rows = (
                db.query(Market)
                .filter(
                    Market.is_active == True,
                    Market.yes_price != None,
                    Market.yes_price > 0.05,
                    Market.yes_price < 0.95,
                    Market.volume_24h > 500,      # raised from 100 — filter thin markets
                    Market.liquidity > 1000,       # require meaningful order book depth
                )
                .order_by(Market.volume_24h.desc())
                .limit(5000)
                .all()
            )
            active_markets = [
                type("M", (), {
                    "id": m.id, "question": m.question, "category": m.category,
                    "yes_price": m.yes_price, "no_price": m.no_price,
                    "volume_24h": m.volume_24h, "liquidity": m.liquidity,
                    "open_interest": m.open_interest, "end_date": m.end_date,
                    "yes_token_id": m.yes_token_id, "no_token_id": m.no_token_id,
                })()
                for m in rows
            ]

        logger.info("Running edge detection on %d active markets", len(active_markets))

        if not active_markets:
            return []

        market_ids = [m.id for m in active_markets]

        # ── 5 bulk queries, replacing ~25k individual DB calls ────────────────
        price_stats  = self._bulk_price_stats(market_ids)
        volume_stats = self._bulk_volume_stats(market_ids)
        sentiments   = self._bulk_sentiments(market_ids)
        whale_net    = self._bulk_whale_pressure(market_ids)
        arb_map      = self._bulk_arb_signals(market_ids)

        signals = []
        for market in active_markets:
            signal = self._evaluate_market(
                market,
                price_stat  = price_stats.get(market.id),
                volume_stat = volume_stats.get(market.id),
                sentiment   = sentiments.get(market.id),
                whale_net   = whale_net.get(market.id, 0.0),
                arb_val     = arb_map.get(market.id, (0.0, 0.0)),
            )
            if signal:
                signals.append(signal)

        signals.sort(key=lambda s: s["edge_score"], reverse=True)
        self._persist_signals(signals)

        logger.info(
            "Edge detection complete — %d signals found (top score: %.1f)",
            len(signals),
            signals[0]["edge_score"] if signals else 0,
        )
        return signals

    # ─── Bulk Prefetch Methods ────────────────────────────────

    def _bulk_price_stats(self, market_ids: list[str]) -> dict:
        """
        Returns dict[market_id -> (mean_price, std_price, n)].
        Uses SQL aggregation over last 24h snapshots.
        """
        cutoff = datetime.utcnow() - timedelta(hours=24)
        result = {}
        with get_db() as db:
            for chunk in _chunks(market_ids, _CHUNK):
                rows = (
                    db.query(
                        MarketSnapshot.market_id,
                        func.avg(MarketSnapshot.yes_price).label("mean_p"),
                        func.avg(
                            MarketSnapshot.yes_price * MarketSnapshot.yes_price
                        ).label("avg_p2"),
                        func.count().label("n"),
                    )
                    .filter(
                        MarketSnapshot.market_id.in_(chunk),
                        MarketSnapshot.timestamp >= cutoff,
                        MarketSnapshot.yes_price != None,
                    )
                    .group_by(MarketSnapshot.market_id)
                    .all()
                )
                for row in rows:
                    if row.n >= 3:
                        mean = row.mean_p
                        var = max(0.0, row.avg_p2 - mean * mean)
                        result[row.market_id] = (mean, var ** 0.5, row.n)
        return result

    def _bulk_volume_stats(self, market_ids: list[str]) -> dict:
        """
        Returns dict[market_id -> (mean_vol, std_vol, n)].
        Uses SQL aggregation over last 7-day snapshots.
        """
        cutoff = datetime.utcnow() - timedelta(days=7)
        result = {}
        with get_db() as db:
            for chunk in _chunks(market_ids, _CHUNK):
                rows = (
                    db.query(
                        MarketSnapshot.market_id,
                        func.avg(MarketSnapshot.volume_24h).label("mean_v"),
                        func.avg(
                            MarketSnapshot.volume_24h * MarketSnapshot.volume_24h
                        ).label("avg_v2"),
                        func.count().label("n"),
                    )
                    .filter(
                        MarketSnapshot.market_id.in_(chunk),
                        MarketSnapshot.timestamp >= cutoff,
                        MarketSnapshot.volume_24h != None,
                    )
                    .group_by(MarketSnapshot.market_id)
                    .all()
                )
                for row in rows:
                    if row.n >= 5:
                        mean = row.mean_v
                        var = max(0.0, row.avg_v2 - mean * mean)
                        result[row.market_id] = (mean, var ** 0.5, row.n)
        return result

    def _bulk_sentiments(self, market_ids: list[str]) -> dict:
        """
        Returns dict[market_id -> compound_score (float)].
        Fetches the latest aggregate sentiment record per market.
        """
        result = {}
        with get_db() as db:
            for chunk in _chunks(market_ids, _CHUNK):
                # Subquery: max timestamp per market
                subq = (
                    db.query(
                        SentimentRecord.market_id,
                        func.max(SentimentRecord.timestamp).label("max_ts"),
                    )
                    .filter(SentimentRecord.market_id.in_(chunk))
                    .group_by(SentimentRecord.market_id)
                    .subquery()
                )
                rows = (
                    db.query(
                        SentimentRecord.market_id,
                        SentimentRecord.compound_score,
                    )
                    .join(
                        subq,
                        (SentimentRecord.market_id == subq.c.market_id)
                        & (SentimentRecord.timestamp == subq.c.max_ts),
                    )
                    .all()
                )
                for row in rows:
                    result[row.market_id] = row.compound_score
        return result

    def _bulk_whale_pressure(self, market_ids: list[str], hours: int = 24) -> dict:
        """
        Returns dict[market_id -> net_pressure (-1 to +1)].
        Aggregates buy vs sell volume over last N hours for leaderboard-sourced whales only.
        Auto-detected wallets are excluded — they are noise, not smart money.
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        buys: dict[str, float] = defaultdict(float)
        sells: dict[str, float] = defaultdict(float)

        from db.models import WhaleWallet as _WhaleWallet
        with get_db() as db:
            quality_addresses = [
                row.address for row in db.query(_WhaleWallet.address)
                .filter(_WhaleWallet.source == "leaderboard")
                .all()
            ]
            if not quality_addresses:
                return {}

            for chunk in _chunks(market_ids, _CHUNK):
                rows = (
                    db.query(
                        WhaleTrade.market_id,
                        WhaleTrade.action,
                        WhaleTrade.size_usdc,
                    )
                    .filter(
                        WhaleTrade.market_id.in_(chunk),
                        WhaleTrade.timestamp >= cutoff,
                        WhaleTrade.wallet_address.in_(quality_addresses),
                    )
                    .all()
                )
                for row in rows:
                    if row.action == "BUY":
                        buys[row.market_id] += row.size_usdc or 0
                    else:
                        sells[row.market_id] += row.size_usdc or 0

        result = {}
        for mid in set(list(buys) + list(sells)):
            total = buys[mid] + sells[mid]
            result[mid] = (buys[mid] - sells[mid]) / total if total > 0 else 0.0
        return result

    def _bulk_arb_signals(self, market_ids: list[str]) -> dict:
        """
        Returns dict[market_id -> (score 0-1, profit_pct)].
        Fetches the best active arb opportunity per market.
        """
        result = {}
        with get_db() as db:
            for chunk in _chunks(market_ids, _CHUNK):
                rows = (
                    db.query(
                        ArbitrageOpportunity.polymarket_market_id,
                        ArbitrageOpportunity.profit_pct,
                    )
                    .filter(
                        ArbitrageOpportunity.polymarket_market_id.in_(chunk),
                        ArbitrageOpportunity.is_active == True,
                    )
                    .order_by(ArbitrageOpportunity.profit_pct.desc())
                    .all()
                )
                for row in rows:
                    if row.polymarket_market_id not in result:
                        score = min(1.0, row.profit_pct / 5.0)
                        result[row.polymarket_market_id] = (score, row.profit_pct)
        return result

    # ─── Per-market Evaluation ────────────────────────────────

    def _evaluate_market(
        self,
        market,
        price_stat:  Optional[tuple],
        volume_stat: Optional[tuple],
        sentiment:   Optional[float],
        whale_net:   float,
        arb_val:     tuple,
    ) -> Optional[dict]:
        """Compute composite edge score from prefetched data. No DB calls."""
        scores = {}
        directions = {}

        # 1. Price-Sentiment Divergence
        ps_score, ps_dir, ps_z = self._price_sentiment_signal(
            market, price_stat, sentiment
        )
        scores["price_sentiment"] = ps_score
        directions["price_sentiment"] = ps_dir

        # 2. Whale Accumulation Signal — leaderboard-quality whales only
        whale_score, whale_dir = self._whale_signal(whale_net)
        scores["whale"] = whale_score
        directions["whale"] = whale_dir

        # 3. Volume Anomaly + price-momentum direction
        vol_score, vol_z = self._volume_anomaly_signal(market, volume_stat)
        scores["volume"] = vol_score
        vol_dir: Optional[str] = None
        # When volume spikes, infer direction from price z-score:
        # price moved up during the spike → momentum is YES; down → NO
        if vol_score > 0.4 and price_stat:
            mean_p, _, _ = price_stat
            vol_dir = "YES" if market.yes_price > mean_p else "NO"
        directions["volume"] = vol_dir

        # 4. Arbitrage Signal
        arb_score, arb_profit = arb_val
        scores["arb"] = arb_score

        # 5. Base Rate Mismatch — only count as a direction signal when
        # deviation is large enough to be meaningful (>20c off base rate)
        base_score, base_dir = self._base_rate_signal(market)
        scores["base_rate"] = base_score
        cat = (market.category or "").lower()
        BASE_RATES = {
            "politics": 0.50, "crypto": 0.50, "sports": 0.50,
            "economics": 0.50, "geopolitics": 0.35,
            "entertainment": 0.50, "science": 0.40,
        }
        base_rate = BASE_RATES.get(cat, 0.50)
        directions["base_rate"] = None  # base_rate score boosts edge but must not drive direction — all markets use 50% default which bets against market consensus

        # Composite weighted score (0–100)
        composite = (
            scores["price_sentiment"] * WEIGHT_PRICE_SENTIMENT
            + scores["whale"]         * WEIGHT_WHALE
            + scores["volume"]        * WEIGHT_VOLUME_ANOMALY
            + scores["arb"]           * WEIGHT_ARB
            + scores["base_rate"]     * WEIGHT_BASE_RATE
        ) * 100

        if composite < 10:
            return None

        direction = self._consensus_direction(directions)

        agreeing = sum(
            1 for d in directions.values()
            if d == direction and d not in ("NEUTRAL", None)
        )
        confidence = min(
            1.0, agreeing / max(1, len([d for d in directions.values() if d]))
        )

        signal_type = self._primary_signal_type(scores)

        notes_parts = []
        if ps_score > 0.3:
            notes_parts.append(f"Sentiment divergence z={ps_z:.2f}")
        if whale_score > 0.3:
            notes_parts.append(f"Whale pressure {whale_dir}")
        if vol_score > 0.3:
            notes_parts.append(f"Volume spike z={vol_z:.2f}")
        if arb_score > 0.3:
            notes_parts.append(f"Arb gap {arb_profit:.1f}%")
        if base_score > 0.3:
            notes_parts.append(f"Base rate mismatch price={market.yes_price:.2f}")

        fair_price = self._estimate_fair_price(market, sentiment, whale_net, vol_score, vol_dir)

        return {
            "market_id": market.id,
            "question": market.question,
            "category": market.category,
            "end_date": market.end_date,
            "signal_type": signal_type,
            "edge_score": round(composite, 2),
            "confidence": round(confidence, 3),
            "zscore": round(ps_z, 3) if ps_z else None,
            "direction": direction,
            "current_price": market.yes_price,
            "implied_fair_price": fair_price,
            "sentiment_score": sentiment,
            "whale_signal": whale_net,
            "arb_profit_pct": arb_profit,
            "volume_24h": market.volume_24h,
            "liquidity": market.liquidity,
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "notes": " | ".join(notes_parts) if notes_parts else "Composite signal",
            "sub_scores": scores,
        }

    # ─── Sub-signal Computations (no DB calls) ────────────────

    def _price_sentiment_signal(
        self,
        market,
        price_stat: Optional[tuple],
        sentiment: Optional[float],
    ) -> tuple[float, Optional[str], float]:
        """Detect divergence between market price and social sentiment.

        Falls back to price momentum (z-score vs 24h avg) when no sentiment data.
        A large z-score indicates recent price movement that may present a mean-reversion
        or momentum opportunity.
        """
        z = self._price_zscore_from_stat(market.yes_price, price_stat)

        if sentiment is None or market.yes_price is None:
            # No social data — use price momentum only (capped at 0.5 weight)
            if abs(z) < ZSCORE_THRESHOLD:
                return 0.0, None, z
            score = min(0.5, abs(z) / ZSCORE_THRESHOLD * 0.5)
            # Momentum direction: price moved up → more likely YES, down → more likely NO
            direction = "YES" if z > 0 else "NO"
            return score, direction, z

        implied_p = 0.5 + sentiment * 0.35
        price_gap = market.yes_price - implied_p

        divergence = abs(price_gap)
        score = min(1.0, divergence / 0.20)

        if divergence < 0.08:
            return score * 0.3, None, z

        direction = "NO" if price_gap > 0 else "YES"
        return score, direction, z

    def _price_zscore_from_stat(
        self, current_price: Optional[float], price_stat: Optional[tuple]
    ) -> float:
        """Compute z-score of current price vs 24h rolling stats."""
        if price_stat is None or current_price is None:
            return 0.0
        mean, std, n = price_stat
        if n < 3 or std < 1e-9:
            return 0.0
        return (current_price - mean) / std

    def _whale_signal(self, whale_net: float) -> tuple[float, Optional[str]]:
        """Detect recent whale accumulation from prefetched net pressure."""
        score = min(1.0, abs(whale_net))
        if abs(whale_net) < 0.4:  # require stronger conviction before signalling direction
            return score * 0.3, None
        direction = "YES" if whale_net > 0 else "NO"
        return score, direction

    def _volume_anomaly_signal(
        self, market, volume_stat: Optional[tuple]
    ) -> tuple[float, float]:
        """Detect volume spikes vs 7-day rolling average."""
        if not market.volume_24h or volume_stat is None:
            return 0.0, 0.0
        mean_vol, std_vol, n = volume_stat
        if n < 5 or std_vol < 1e-9:
            return 0.0, 0.0
        z = (market.volume_24h - mean_vol) / std_vol
        score = min(1.0, abs(z) / ZSCORE_THRESHOLD)
        # Require a minimum absolute volume in addition to z-score
        # to avoid firing on low-liquidity markets with noisy spikes
        if market.volume_24h < 500:
            return score * 0.2, z
        return (score if abs(z) >= ZSCORE_THRESHOLD else score * 0.4), z

    def _base_rate_signal(self, market) -> tuple[float, Optional[str]]:
        """Detect extreme pricing deviating from category base rates."""
        p = market.yes_price
        if p is None:
            return 0.0, None

        BASE_RATES = {
            "politics":      0.50,
            "crypto":        0.50,
            "sports":        0.50,
            "economics":     0.50,
            "geopolitics":   0.35,
            "entertainment": 0.50,
            "science":       0.40,
        }
        cat = (market.category or "").lower()
        base_rate = BASE_RATES.get(cat, 0.50)

        deviation = abs(p - base_rate)
        liquidity_penalty = (
            max(0.1, min(1.0, market.liquidity / 50000)) if market.liquidity else 0.1
        )
        score = min(1.0, (deviation / 0.3) * (1 - liquidity_penalty * 0.5))
        direction = (
            "NO" if p > base_rate + 0.15
            else ("YES" if p < base_rate - 0.15 else None)
        )
        return score, direction

    # ─── Helpers ──────────────────────────────────────────────

    def _estimate_fair_price(
        self,
        market,
        sentiment: Optional[float],
        whale_net: float,
        vol_score: float = 0.0,
        vol_dir: Optional[str] = None,
    ) -> Optional[float]:
        """
        Blend market price, sentiment, whale pressure, and volume momentum into a fair
        price estimate. Market price is the anchor; sentiment gets the highest weight.
        """
        if market.yes_price is None:
            return None

        p = market.yes_price
        estimates = []
        weights = []

        # ── Current price (anchor) ──────────────────────────
        estimates.append(p)
        weights.append(1.0)

        # ── Sentiment signal ─────────────────────────────────
        if sentiment is not None:
            sentiment_implied = 0.5 + sentiment * 0.35
            estimates.append(sentiment_implied)
            weights.append(1.5)   # outweighs base rate when available

        # ── Whale pressure ────────────────────────────────────
        if abs(whale_net) > 0.1:
            whale_adjusted = min(0.99, max(0.01, p + whale_net * 0.08))
            estimates.append(whale_adjusted)
            weights.append(1.2)

        # ── Volume momentum — nudge price in direction of the spike ──
        if vol_score > 0.4 and vol_dir in ("YES", "NO"):
            momentum = 0.06 * vol_score  # up to +/-0.06 at full score
            if vol_dir == "YES":
                vol_adjusted = min(0.99, p + momentum)
            else:
                vol_adjusted = max(0.01, p - momentum)
            estimates.append(vol_adjusted)
            weights.append(1.0)

        total_weight = sum(weights)
        fair = sum(e * w for e, w in zip(estimates, weights)) / total_weight
        return round(float(np.clip(fair, 0.01, 0.99)), 3)

    def _consensus_direction(self, directions: dict) -> Optional[str]:
        counts = {"YES": 0, "NO": 0, "NEUTRAL": 0}
        for d in directions.values():
            if d in counts:
                counts[d] += 1
        if counts["YES"] > counts["NO"]:
            return "YES"
        elif counts["NO"] > counts["YES"]:
            return "NO"
        return "NEUTRAL"

    def _primary_signal_type(self, scores: dict) -> str:
        mapping = {
            "price_sentiment": "price_sentiment_divergence",
            "whale":           "whale_accumulation",
            "volume":          "volume_spike",
            "arb":             "arbitrage",
            "base_rate":       "base_rate_mismatch",
        }
        top = max(scores, key=scores.get)
        return mapping.get(top, "composite")

    def _persist_signals(self, signals: list[dict]):
        """Deactivate old signals and write new ones to DB."""
        with get_db() as db:
            db.query(EdgeSignal).filter_by(is_active=True).update({"is_active": False})
            for s in signals:
                record = EdgeSignal(
                    market_id=s["market_id"],
                    signal_type=s["signal_type"],
                    edge_score=s["edge_score"],
                    confidence=s["confidence"],
                    zscore=s.get("zscore"),
                    direction=s.get("direction"),
                    current_price=s["current_price"],
                    implied_fair_price=s.get("implied_fair_price"),
                    sentiment_score=s.get("sentiment_score"),
                    whale_signal=s.get("whale_signal"),
                    arb_profit_pct=s.get("arb_profit_pct"),
                    notes=s.get("notes"),
                    raw_data=s.get("sub_scores"),
                    is_active=True,
                )
                db.add(record)
