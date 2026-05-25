"""Cross-platform arbitrage detector — Polymarket vs Kalshi."""

import logging
import re
from datetime import datetime
from typing import Optional

from db.database import get_db
from db.models import Market, KalshiMarket, ArbitrageOpportunity
from config import MIN_ARB_PROFIT_PCT

logger = logging.getLogger(__name__)

# Minimum Jaccard similarity (0–1) to match markets across platforms
SIMILARITY_THRESHOLD = 0.20

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "by", "be", "in", "on", "at", "to", "of",
    "or", "and", "yes", "no", "is", "it", "for", "with", "that", "this",
    "are", "was", "has", "have", "had", "not", "from", "its", "which",
})


def _token_set(text: str) -> frozenset:
    """Tokenize and remove stopwords. Returns frozenset for fast intersection."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return frozenset(w for w in text.split() if w not in _STOPWORDS and len(w) > 2)


def _jaccard(s1: frozenset, s2: frozenset) -> float:
    """Jaccard similarity: |intersection| / |union|. O(min(|s1|,|s2|))."""
    if not s1 or not s2:
        return 0.0
    inter = len(s1 & s2)
    if inter == 0:
        return 0.0
    return inter / len(s1 | s2)


def _build_inverted_index(kalshi_markets: list) -> dict:
    """token → list of kalshi market indices (for candidate pruning)."""
    idx: dict[str, list[int]] = {}
    for i, km in enumerate(kalshi_markets):
        for tok in km.get("_tokens", frozenset()):
            idx.setdefault(tok, []).append(i)
    return idx


class ArbitrageMonitor:
    def __init__(self):
        pass

    def detect_opportunities(self) -> list[dict]:
        """
        Compare all active Polymarket markets with Kalshi markets.
        Returns list of arb opportunity dicts for any pair exceeding MIN_ARB_PROFIT_PCT.
        """
        opportunities = []

        # Snapshot to plain dicts inside session to avoid detached-instance errors
        with get_db() as db:
            poly_markets = [
                {"id": m.id, "question": m.question, "yes_price": m.yes_price,
                 "no_price": m.no_price, "volume_24h": m.volume_24h}
                for m in db.query(Market)
                .filter(
                    Market.is_active == True,
                    Market.yes_price != None,
                    Market.yes_price > 0.05,   # exclude near-resolved (YES→0 or YES→1)
                    Market.yes_price < 0.95,
                    Market.volume_24h > 500,   # require meaningful liquidity
                )
                .order_by(Market.volume_24h.desc())
                .limit(2000)
                .all()
            ]
            kalshi_markets = [
                {"ticker": k.ticker, "title": k.title, "yes_price": k.yes_price,
                 "no_price": k.no_price, "volume": k.volume}
                for k in db.query(KalshiMarket)
                .filter(
                    KalshiMarket.yes_price != None,
                    KalshiMarket.yes_price > 0.05,  # exclude near-resolved Kalshi markets
                    KalshiMarket.yes_price < 0.95,
                )
                .all()
            ]

        if not poly_markets or not kalshi_markets:
            logger.debug("No markets to compare for arbitrage")
            return []

        # Pre-tokenize all Kalshi markets once and build inverted index
        for km in kalshi_markets:
            km["_tokens"] = _token_set(km.get("title") or "")
        inv_idx = _build_inverted_index(kalshi_markets)

        for pm in poly_markets:
            if not pm.get("yes_price") or not pm.get("question"):
                continue

            best_match = self._find_kalshi_match(pm, kalshi_markets, inv_idx)
            if not best_match:
                continue

            km, similarity = best_match
            if not km.get("yes_price"):
                continue

            arb = self._compute_arbitrage(pm, km, similarity)
            if arb:
                opportunities.append(arb)

        # Persist and return
        self._persist_opportunities(opportunities)
        return opportunities

    def _find_kalshi_match(
        self, pm: dict, kalshi_markets: list, inv_idx: dict
    ) -> Optional[tuple]:
        """Find best-matching Kalshi market using inverted-index Jaccard similarity.

        Uses the inverted token index to skip Kalshi markets with zero token overlap,
        reducing O(n*m) SequenceMatcher to O(candidates) Jaccard checks.
        """
        pm_tokens = _token_set(pm.get("question") or "")
        if not pm_tokens:
            return None

        # Collect candidate indices via inverted index (share ≥1 token)
        candidate_indices: set[int] = set()
        for tok in pm_tokens:
            candidate_indices.update(inv_idx.get(tok, []))

        best_score = 0.0
        best_km = None

        for i in candidate_indices:
            km = kalshi_markets[i]
            score = _jaccard(pm_tokens, km["_tokens"])
            if score > best_score:
                best_score = score
                best_km = km

        if best_score >= SIMILARITY_THRESHOLD:
            return best_km, best_score
        return None

    def _compute_arbitrage(self, pm: dict, km: dict, similarity: float) -> Optional[dict]:
        """
        Compute arbitrage potential between two matched markets.

        Strategy 1 — Buy YES on cheaper platform, Sell (buy NO) on expensive platform.
        Strategy 2 — Mechanical arb: YES + NO on same or cross-platform < $1.00.
        """
        poly_yes = pm.get("yes_price")
        poly_no = pm.get("no_price") or (1.0 - poly_yes if poly_yes else None)
        kalshi_yes = km.get("yes_price")
        kalshi_no = km.get("no_price") or (1.0 - kalshi_yes if kalshi_yes else None)

        if None in (poly_yes, poly_no, kalshi_yes, kalshi_no):
            return None

        # Reject near-resolved prices — they produce huge fake arb numbers
        if not (0.05 < poly_yes < 0.95) or not (0.05 < kalshi_yes < 0.95):
            return None

        results = []

        # ── Strategy A: Buy Poly YES + Buy Kalshi NO ──────────
        cost_a = poly_yes + kalshi_no
        if cost_a < 1.0:
            profit_pct_a = ((1.0 - cost_a) / cost_a) * 100
            results.append((
                profit_pct_a,
                f"buy_poly_yes_buy_kalshi_no",
                poly_yes, kalshi_yes,
                f"Buy YES on Polymarket @ {poly_yes:.3f}, Buy NO on Kalshi @ {kalshi_no:.3f}. "
                f"Cost: ${cost_a:.4f} → guaranteed $1.00. Profit: {profit_pct_a:.2f}%",
            ))

        # ── Strategy B: Buy Kalshi YES + Buy Poly NO ──────────
        cost_b = kalshi_yes + poly_no
        if cost_b < 1.0:
            profit_pct_b = ((1.0 - cost_b) / cost_b) * 100
            results.append((
                profit_pct_b,
                f"buy_kalshi_yes_buy_poly_no",
                poly_yes, kalshi_yes,
                f"Buy YES on Kalshi @ {kalshi_yes:.3f}, Buy NO on Polymarket @ {poly_no:.3f}. "
                f"Cost: ${cost_b:.4f} → guaranteed $1.00. Profit: {profit_pct_b:.2f}%",
            ))

        # ── Strategy C: Directional — platforms disagree on probability ──
        price_gap = abs(poly_yes - kalshi_yes)
        directional_pct = price_gap * 100
        if directional_pct >= MIN_ARB_PROFIT_PCT * 0.5 and price_gap > 0.03:
            direction = (
                "poly_yes_underpriced" if poly_yes < kalshi_yes
                else "kalshi_yes_underpriced"
            )
            results.append((
                directional_pct,
                direction,
                poly_yes, kalshi_yes,
                f"Price gap: Poly YES={poly_yes:.3f} vs Kalshi YES={kalshi_yes:.3f} "
                f"({price_gap:.3f} = {directional_pct:.1f}%). "
                f"Directional play — not risk-free but high EV.",
            ))

        if not results:
            return None

        # Return best opportunity
        best = max(results, key=lambda x: x[0])
        profit_pct, direction, poly_p, kalshi_p, notes = best

        if profit_pct < MIN_ARB_PROFIT_PCT:
            return None

        return {
            "polymarket_market_id": pm["id"],
            "kalshi_ticker": km["ticker"],
            "description": f"Poly: {pm['question'][:80]} | Kalshi: {km['title'][:80]}",
            "similarity": round(similarity, 3),
            "poly_yes_price": poly_yes,
            "kalshi_yes_price": kalshi_yes,
            "price_gap": round(abs(poly_yes - kalshi_yes), 4),
            "profit_pct": round(profit_pct, 2),
            "arb_type": "cross_platform",
            "direction": direction,
            "notes": notes,
        }

    def _persist_opportunities(self, opportunities: list[dict]):
        """Save detected opportunities to DB, marking old ones as resolved."""
        with get_db() as db:
            # Mark existing active opportunities as resolved
            db.query(ArbitrageOpportunity).filter_by(is_active=True).update(
                {"is_active": False, "resolved_at": datetime.utcnow()}
            )

            for opp in opportunities:
                record = ArbitrageOpportunity(
                    polymarket_market_id=opp["polymarket_market_id"],
                    kalshi_ticker=opp["kalshi_ticker"],
                    description=opp["description"],
                    poly_yes_price=opp["poly_yes_price"],
                    kalshi_yes_price=opp["kalshi_yes_price"],
                    price_gap=opp["price_gap"],
                    profit_pct=opp["profit_pct"],
                    arb_type=opp["arb_type"],
                    direction=opp["direction"],
                    is_active=True,
                )
                db.add(record)

        if opportunities:
            logger.info(
                "Detected %d arbitrage opportunities (best: %.2f%%)",
                len(opportunities),
                max(o["profit_pct"] for o in opportunities),
            )

    # ─── Mechanical Arb (same platform) ──────────────────────

    def detect_mechanical_arb(self) -> list[dict]:
        """
        Find Polymarket markets where YES + NO prices sum to < $1.00.
        SQL-filtered to avoid loading all 258k markets into Python.
        """
        opportunities = []
        max_total = 1.0 - (MIN_ARB_PROFIT_PCT / (100.0 + MIN_ARB_PROFIT_PCT))

        with get_db() as db:
            from sqlalchemy import text as _text
            rows = db.execute(_text(
                "SELECT id, question, yes_price, no_price, (yes_price + no_price) AS total "
                "FROM markets "
                "WHERE is_active = 1 AND yes_price IS NOT NULL AND no_price IS NOT NULL "
                "  AND yes_price > 0 AND no_price > 0 "
                "  AND (yes_price + no_price) < :max_total "
                "LIMIT 500"
            ), {"max_total": max_total}).fetchall()

        for row in rows:
            mid, question, yes_price, no_price, total = row
            profit_pct = ((1.0 - total) / total) * 100
            opportunities.append({
                "market_id": mid,
                "question": question,
                "yes_price": yes_price,
                "no_price": no_price,
                "total_cost": round(total, 4),
                "profit_pct": round(profit_pct, 2),
                "arb_type": "mechanical",
                "notes": (
                    f"YES + NO = {total:.4f} < $1.00 on same market. "
                    f"Buy both → guaranteed profit of {profit_pct:.2f}%"
                ),
            })

        if opportunities:
            logger.info("Found %d mechanical arb opportunities", len(opportunities))

        return opportunities
