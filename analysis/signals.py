"""Signal query helpers — pull ranked signals from the DB for display/alerts."""

from datetime import datetime, timedelta

from db.database import get_db
from db.models import EdgeSignal, Market, ArbitrageOpportunity, WhaleTrade


def get_top_signals(limit: int = 20, min_score: float = 25.0) -> list[dict]:
    """Return top active edge signals ranked by edge_score."""
    with get_db() as db:
        signals = (
            db.query(EdgeSignal, Market)
            .join(Market, EdgeSignal.market_id == Market.id)
            .filter(
                EdgeSignal.is_active == True,
                EdgeSignal.edge_score >= min_score,
            )
            .order_by(EdgeSignal.edge_score.desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "id": sig.id,
                "market_id": sig.market_id,
                "question": market.question,
                "category": market.category,
                "signal_type": sig.signal_type,
                "edge_score": sig.edge_score,
                "confidence": sig.confidence,
                "direction": sig.direction,
                "current_price": sig.current_price,
                "implied_fair_price": sig.implied_fair_price,
                "sentiment_score": sig.sentiment_score,
                "whale_signal": sig.whale_signal,
                "arb_profit_pct": sig.arb_profit_pct,
                "volume_24h": market.volume_24h,
                "liquidity": market.liquidity,
                "notes": sig.notes,
                "detected_at": sig.detected_at,
            }
            for sig, market in signals
        ]


def get_top_arbitrage(limit: int = 10) -> list[dict]:
    """Return active arbitrage opportunities ranked by profit %."""
    with get_db() as db:
        opps = (
            db.query(ArbitrageOpportunity, Market)
            .join(Market, ArbitrageOpportunity.polymarket_market_id == Market.id)
            .filter(ArbitrageOpportunity.is_active == True)
            .order_by(ArbitrageOpportunity.profit_pct.desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "id": opp.id,
                "description": opp.description,
                "poly_yes_price": opp.poly_yes_price,
                "kalshi_yes_price": opp.kalshi_yes_price,
                "price_gap": opp.price_gap,
                "profit_pct": opp.profit_pct,
                "arb_type": opp.arb_type,
                "direction": opp.direction,
                "kalshi_ticker": opp.kalshi_ticker,
                "detected_at": opp.detected_at,
            }
            for opp, market in opps
        ]


def get_recent_whale_activity(hours: int = 6, limit: int = 50) -> list[dict]:
    """Return recent whale trades, newest first."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    with get_db() as db:
        trades = (
            db.query(WhaleTrade)
            .filter(WhaleTrade.timestamp >= cutoff)
            .order_by(WhaleTrade.timestamp.desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "tx_hash": t.tx_hash,
                "wallet": t.wallet_address,
                "market_id": t.market_id,
                "side": t.side,
                "action": t.action,
                "size_usdc": t.size_usdc,
                "price": t.price,
                "timestamp": t.timestamp,
            }
            for t in trades
        ]


def get_signal_history(market_id: str, limit: int = 100) -> list[dict]:
    """Return historical edge signals for a specific market."""
    with get_db() as db:
        signals = (
            db.query(EdgeSignal)
            .filter(EdgeSignal.market_id == market_id)
            .order_by(EdgeSignal.detected_at.desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "signal_type": s.signal_type,
                "edge_score": s.edge_score,
                "direction": s.direction,
                "current_price": s.current_price,
                "implied_fair_price": s.implied_fair_price,
                "detected_at": s.detected_at,
            }
            for s in signals
        ]
