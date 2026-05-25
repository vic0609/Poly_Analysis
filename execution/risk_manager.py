"""
Risk manager — enforces bankroll limits, position caps, drawdown controls,
and duplicate-trade prevention before any order is sent.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from db.database import get_db
from db.models import ExecutedTrade, Position

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        bankroll_usdc: float,
        max_open_positions: int,
        max_position_usdc: float,
        max_daily_loss_pct: float = 0.10,   # halt trading if down >10% in a day
        min_edge_score: float = 50.0,
    ):
        self.bankroll_usdc = bankroll_usdc
        self.max_open_positions = max_open_positions
        self.max_position_usdc = max_position_usdc
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_edge_score = min_edge_score

    def check(self, signal: dict, kelly_size_usdc: float) -> tuple[bool, str]:
        """
        Run all pre-trade risk checks.

        Returns (approved: bool, reason: str).
        """
        # 1. Edge score gate
        if signal.get("edge_score", 0) < self.min_edge_score:
            return False, f"Edge score {signal['edge_score']:.1f} below threshold {self.min_edge_score}"

        # 2. Already have an open position in this market
        if self._already_positioned(signal["market_id"]):
            return False, f"Already have open position in {signal['market_id'][:16]}…"

        # 3. Too many open positions
        open_count = self._open_position_count()
        if open_count >= self.max_open_positions:
            return False, f"Max open positions reached ({open_count}/{self.max_open_positions})"

        # 4. Daily loss circuit-breaker
        daily_pnl = self._daily_pnl()
        max_loss = -self.bankroll_usdc * self.max_daily_loss_pct
        if daily_pnl < max_loss:
            return False, f"Daily loss circuit-breaker: P&L ${daily_pnl:,.2f} < limit ${max_loss:,.2f}"

        # 5. Available capital
        deployed = self._deployed_usdc()
        available = self.bankroll_usdc - deployed
        if kelly_size_usdc > available:
            return False, f"Insufficient capital: need ${kelly_size_usdc:.2f}, available ${available:.2f}"

        return True, "ok"

    # ─── DB helpers ───────────────────────────────────────────

    def _already_positioned(self, market_id: str) -> bool:
        with get_db() as db:
            return (
                db.query(Position)
                .filter_by(market_id=market_id, status="open")
                .first()
            ) is not None

    def _open_position_count(self) -> int:
        with get_db() as db:
            return db.query(Position).filter_by(status="open").count()

    def _deployed_usdc(self) -> float:
        with get_db() as db:
            return sum(
                s or 0
                for (s,) in db.query(Position.size_usdc)
                .filter_by(status="open")
                .all()
            )

    def _daily_pnl(self) -> float:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with get_db() as db:
            return sum(
                pnl or 0
                for (pnl,) in db.query(ExecutedTrade.realized_pnl)
                .filter(ExecutedTrade.executed_at >= cutoff)
                .all()
            )
