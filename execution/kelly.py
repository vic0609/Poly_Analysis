"""
Kelly Criterion position sizing for binary prediction markets.

For a YES bet:
  - You pay price c per share, each share pays $1.00 if YES resolves
  - Net odds b = (1 - c) / c
  - Kelly fraction f* = (p - c) / (1 - c)
  where p = estimated true probability, c = current market price

For a NO bet (buying NO shares at price 1-c):
  - f* = ((1-p) - (1-c)) / (1 - (1-c)) = (c - p) / c

Both cases: edge must be positive (we have an informational advantage) else f* = 0.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class KellyResult:
    side: str                   # "YES" or "NO"
    market_price: float         # current price of the chosen side
    true_prob: float            # our estimated true probability
    raw_fraction: float         # full Kelly fraction (0–1)
    scaled_fraction: float      # after half-Kelly / cap applied
    usdc_size: float            # dollar amount to bet
    expected_value: float       # EV per dollar risked
    edge_pct: float             # raw edge as percentage


def kelly_fraction(true_prob: float, market_price: float, side: str = "YES") -> float:
    """
    Compute raw Kelly fraction for one side of a binary prediction market.

    Returns 0.0 if there is no edge (true_prob ≤ market_price for YES,
    or (1-true_prob) ≤ (1-market_price) for NO).
    """
    if side == "YES":
        edge = true_prob - market_price
        if edge <= 0 or market_price >= 1.0:
            return 0.0
        return edge / (1.0 - market_price)
    else:
        true_prob_no = 1.0 - true_prob
        price_no = 1.0 - market_price
        edge = true_prob_no - price_no
        if edge <= 0 or price_no >= 1.0:
            return 0.0
        return edge / (1.0 - price_no)


def expected_value(true_prob: float, market_price: float, side: str = "YES") -> float:
    """
    Expected value per $1 risked.

    EV = p * (win_payout) - (1-p) * stake
    For YES: win_payout = (1 - price) / price, stake = 1
    """
    if side == "YES":
        if market_price <= 0 or market_price >= 1:
            return 0.0
        win_payout = (1.0 - market_price) / market_price
        return true_prob * win_payout - (1.0 - true_prob)
    else:
        price_no = 1.0 - market_price
        true_prob_no = 1.0 - true_prob
        if price_no <= 0 or price_no >= 1:
            return 0.0
        win_payout = (1.0 - price_no) / price_no
        return true_prob_no * win_payout - (1.0 - true_prob_no)


def size_position(
    true_prob: float,
    market_price: float,
    direction: str,
    bankroll_usdc: float,
    half_kelly: bool = True,
    max_fraction: float = 0.25,
    max_usdc: float = 500.0,
    min_usdc: float = 10.0,
) -> Optional[KellyResult]:
    """
    Compute full Kelly position sizing for a prediction market bet.

    Args:
        true_prob:      Our estimated true probability (from EdgeDetector fair price)
        market_price:   Current YES price (0–1)
        direction:      "YES" or "NO" — which side we're betting
        bankroll_usdc:  Total capital available
        half_kelly:     Use half Kelly to reduce variance (recommended)
        max_fraction:   Cap fraction at this value (e.g. 0.25 = 25% max)
        max_usdc:       Hard dollar cap per trade
        min_usdc:       Minimum trade size (skip if smaller)

    Returns:
        KellyResult or None if there's no edge / size too small.
    """
    if true_prob is None or market_price is None:
        return None
    if not (0 < market_price < 1) or not (0 < true_prob < 1):
        return None

    raw_f = kelly_fraction(true_prob, market_price, side=direction)
    if raw_f <= 0:
        return None

    # Apply half-Kelly and cap
    scaled_f = raw_f * (0.5 if half_kelly else 1.0)
    scaled_f = min(scaled_f, max_fraction)

    usdc_size = bankroll_usdc * scaled_f
    usdc_size = min(usdc_size, max_usdc)
    usdc_size = round(usdc_size, 2)

    if usdc_size < min_usdc:
        return None

    ev = expected_value(true_prob, market_price, side=direction)
    side_price = market_price if direction == "YES" else (1.0 - market_price)
    edge_pct = ((true_prob if direction == "YES" else 1 - true_prob) - side_price) / side_price * 100

    return KellyResult(
        side=direction,
        market_price=side_price,
        true_prob=true_prob if direction == "YES" else 1.0 - true_prob,
        raw_fraction=round(raw_f, 4),
        scaled_fraction=round(scaled_f, 4),
        usdc_size=usdc_size,
        expected_value=round(ev, 4),
        edge_pct=round(edge_pct, 2),
    )
