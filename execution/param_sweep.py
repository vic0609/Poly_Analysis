"""
Parameter sweep — grid search over entry parameters using historical
EdgeSignal + MarketSnapshot data to find optimal Kelly entry conditions.

Scoring metrics per param combo:
  - Sharpe ratio       (primary sort)
  - Total return %
  - Win rate
  - Max drawdown
  - N trades (must be > MIN_TRADES to be considered)

Usage:
    python main.py --sweep
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import product
from typing import Optional

import numpy as np
from rich.console import Console
from rich.table import Table
from rich import box

from db.database import get_db
from db.models import EdgeSignal, MarketSnapshot, Market
from execution.kelly import kelly_fraction

BASE_RATES = {
    "politics": 0.50, "crypto": 0.50, "sports": 0.50,
    "economics": 0.50, "geopolitics": 0.35,
    "entertainment": 0.50, "science": 0.40,
}


def _derive_fair_price(yes_price: float, category: str, liquidity: float) -> float:
    """
    Re-derive fair price using base rate gravity — same logic as the live
    EdgeDetector so sweep and live system are consistent.
    """
    base = BASE_RATES.get((category or "").lower(), 0.50)
    deviation = abs(yes_price - base)
    if deviation <= 0.10:
        return yes_price  # no meaningful base-rate edge

    liq = max(0.0, liquidity or 0)
    liq_factor = max(0.05, 1.0 - min(1.0, liq / 100_000))
    base_weight = min(0.35, deviation * liq_factor * 0.8)

    fair = yes_price + (base - yes_price) * base_weight
    return round(float(np.clip(fair, 0.01, 0.99)), 4)

logger = logging.getLogger(__name__)
console = Console(highlight=False, emoji=False)

# ─── Parameter Grid ───────────────────────────────────────────
GRID = {
    "min_edge_score":  [40, 50, 60],
    "min_price_gap":   [0.02, 0.04, 0.06],
    "kelly_scale":     [0.25, 0.50, 1.00],
    "max_fraction":    [0.10, 0.20],
    "min_yes_price":   [0.20, 0.25],
    "stop_loss_pct":   [0.10, 0.15, 0.20, 0.25, 0.35],   # fraction of stake to risk
    "take_profit_pct": [0.50, 0.75, 1.00],                # fraction of stake as profit target
}

MIN_TRADES    = 10      # skip combos with too few trades to be statistically meaningful
BANKROLL      = 1000.0  # starting capital
MAX_USDC      = 500.0   # hard position cap
MIN_USDC      = 10.0    # minimum trade size
LOOKAHEAD_H   = 4       # hours to look ahead for exit price


# ─── Data Loading ─────────────────────────────────────────────

SIGNAL_MAX_ROWS = 50_000  # use most recent N signals — keeps memory manageable


def load_signals() -> list[dict]:
    """Load recent historical edge signals with both entry and exit prices."""
    console.print("[cyan]Loading historical signals...[/cyan]")

    with get_db() as db:
        rows = db.query(
            EdgeSignal.market_id,
            EdgeSignal.detected_at,
            EdgeSignal.direction,
            EdgeSignal.edge_score,
            EdgeSignal.current_price,
            EdgeSignal.signal_type,
        ).filter(
            EdgeSignal.direction.in_(["YES", "NO"]),
            EdgeSignal.current_price != None,
            EdgeSignal.edge_score != None,
        ).order_by(EdgeSignal.detected_at.desc()).limit(SIGNAL_MAX_ROWS).all()

        # Load market metadata for fair price derivation
        market_meta = {
            m.id: (m.category or "", m.liquidity or 0)
            for m in db.query(Market.id, Market.category, Market.liquidity).all()
        }

    console.print(f"  {len(rows):,} raw signals loaded")

    # ── Build snapshot index: market_id -> sorted list of (timestamp, yes_price) ──
    # Only load snapshots for markets that appear in the signals list, and only
    # within the relevant time window — avoids OOM on large databases.
    console.print("[cyan]Loading price snapshots...[/cyan]")
    snap_index: dict[str, list] = defaultdict(list)

    signal_market_ids = list({r.market_id for r in rows})
    # Earliest signal time — no need for snapshots before it
    if rows:
        earliest = min(r.detected_at for r in rows)
        snap_cutoff = earliest - timedelta(minutes=30)
    else:
        snap_cutoff = datetime.utcnow() - timedelta(days=30)

    SNAP_CHUNK = 500
    total_snaps = 0
    for i in range(0, len(signal_market_ids), SNAP_CHUNK):
        chunk_ids = signal_market_ids[i:i + SNAP_CHUNK]
        with get_db() as db:
            for s in (
                db.query(
                    MarketSnapshot.market_id,
                    MarketSnapshot.timestamp,
                    MarketSnapshot.yes_price,
                )
                .filter(
                    MarketSnapshot.market_id.in_(chunk_ids),
                    MarketSnapshot.timestamp >= snap_cutoff,
                    MarketSnapshot.yes_price != None,
                )
                .yield_per(2000)
            ):
                snap_index[s.market_id].append((s.timestamp, s.yes_price))
                total_snaps += 1

    # Sort by timestamp
    for mid in snap_index:
        snap_index[mid].sort(key=lambda x: x[0])

    console.print(f"  {len(snap_index):,} markets with snapshot history ({total_snaps:,} rows)")

    # ── Pair each signal with an exit price ────────────────────
    signals = []
    skipped = 0
    for row in rows:
        snaps_for_market = snap_index.get(row.market_id, [])
        exit_price = _find_exit_price(snaps_for_market, row.detected_at)
        if exit_price is None:
            skipped += 1
            continue

        # Re-derive fair price from base rate (stored values are stale)
        cat, liq = market_meta.get(row.market_id, ("", 0))
        fair = _derive_fair_price(row.current_price, cat, liq)

        signals.append({
            "market_id":      row.market_id,
            "detected_at":    row.detected_at,
            "direction":      row.direction,
            "edge_score":     row.edge_score,
            "current_price":  row.current_price,
            "fair_price":     fair,
            "signal_type":    row.signal_type,
            "exit_yes_price": exit_price,
        })

    console.print(
        f"  {len(signals):,} signals matched to exit prices "
        f"({skipped:,} skipped — no future snapshot)"
    )
    return signals


def _find_exit_price(
    snapshots: list[tuple],
    entry_time: datetime,
) -> Optional[float]:
    """
    Find the first snapshot at least 30 min and at most LOOKAHEAD_H hours
    after entry_time. Returns yes_price or None.
    """
    min_dt = entry_time + timedelta(minutes=30)
    max_dt = entry_time + timedelta(hours=LOOKAHEAD_H)

    for ts, price in snapshots:
        if min_dt <= ts <= max_dt:
            return price
    return None


# ─── Simulation ───────────────────────────────────────────────

def simulate(
    signals: list[dict],
    min_edge_score: float,
    min_price_gap: float,
    kelly_scale: float,
    max_fraction: float,
    min_yes_price: float = 0.05,
    stop_loss_pct: float = 0.25,
    take_profit_pct: float = 0.75,
) -> dict:
    """
    Simulate paper trading under a given parameter set.
    Returns performance metrics dict.
    """
    balance = BANKROLL
    peak = BANKROLL
    max_drawdown = 0.0
    trade_returns = []

    for sig in signals:
        if sig["edge_score"] < min_edge_score:
            continue
        if sig["direction"] not in ("YES", "NO"):
            continue

        direction   = sig["direction"]
        yes_price   = sig["current_price"]
        fair        = sig["fair_price"]
        exit_yes    = sig["exit_yes_price"]

        # Entry / exit prices for chosen side
        if direction == "YES":
            entry = yes_price
            fair_side = fair
            exit_p = exit_yes
        else:
            entry = 1.0 - yes_price
            fair_side = 1.0 - fair
            exit_p = 1.0 - exit_yes

        if not (0 < entry < 1) or not (0 < fair_side < 1):
            continue

        # Skip near-zero / near-one markets (illiquid, hard to execute)
        if yes_price < min_yes_price or yes_price > (1 - min_yes_price):
            continue

        # Minimum price gap between fair and market
        if abs(fair_side - entry) < min_price_gap:
            continue

        # Kelly fraction
        raw_k = kelly_fraction(fair_side, entry, side="YES")  # normalized to chosen side
        if raw_k <= 0:
            continue

        scaled_k = min(raw_k * kelly_scale, max_fraction)
        stake = min(balance * scaled_k, MAX_USDC)
        if stake < MIN_USDC:
            continue

        # Apply stop-loss / take-profit caps to the exit price
        # (approximation: if 4h price crossed the threshold, assume it triggered)
        raw_return = (exit_p - entry) / entry
        if raw_return <= -stop_loss_pct:
            exit_p = entry * (1.0 - stop_loss_pct)   # stopped out
        elif raw_return >= take_profit_pct:
            exit_p = entry * (1.0 + take_profit_pct)  # took profit

        # P&L: enter at entry, exit at exit_p
        shares = stake / entry
        pnl = (exit_p - entry) * shares

        balance += pnl
        balance = max(0.01, balance)   # no negative balance

        # Track return per trade and drawdown
        ret = pnl / stake
        trade_returns.append(ret)

        if balance > peak:
            peak = balance
        drawdown = (peak - balance) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    n = len(trade_returns)
    if n < MIN_TRADES:
        return None

    arr = np.array(trade_returns)
    mean_r = float(np.mean(arr))
    std_r = float(np.std(arr)) + 1e-9
    sharpe = mean_r / std_r * np.sqrt(n)   # trade-count normalized Sharpe
    win_rate = float(np.mean(arr > 0))
    total_return = (balance - BANKROLL) / BANKROLL * 100

    return {
        "total_return":  round(total_return, 2),
        "sharpe":        round(sharpe, 3),
        "win_rate":      round(win_rate, 3),
        "max_drawdown":  round(max_drawdown * 100, 2),
        "n_trades":      n,
        "final_balance": round(balance, 2),
        "avg_return":    round(mean_r * 100, 3),
    }


# ─── Grid Search ──────────────────────────────────────────────

def run_sweep() -> list[dict]:
    """Run full grid search and return results sorted by Sharpe."""
    signals = load_signals()
    if not signals:
        console.print("[red]No historical signals found. Run the monitor for at least a few cycles first.[/red]")
        return []

    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(product(*values))

    console.print(
        f"\n[cyan]Sweeping {len(combos)} parameter combinations "
        f"across {len(signals):,} signals...[/cyan]"
    )

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        metrics = simulate(signals, **params)
        if metrics is None:
            continue
        results.append({**params, **metrics})

    results.sort(key=lambda r: r["sharpe"], reverse=True)
    return results


# ─── Display ──────────────────────────────────────────────────

def print_sweep_results(results: list[dict]):
    if not results:
        console.print("[yellow]No valid parameter combinations found.[/yellow]")
        return

    best = results[0]

    console.print(f"\n[bold green]═══ Parameter Sweep Results ({len(results)} valid combos) ═══[/bold green]")
    console.print(f"  Lookahead window:  {LOOKAHEAD_H}h")
    console.print(f"  Starting bankroll: ${BANKROLL:,.0f}")
    console.print(f"  Min trades filter: {MIN_TRADES}")

    table = Table(
        title="Top 15 Parameter Combinations (sorted by Sharpe)",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("#",        width=3)
    table.add_column("Score≥",   width=6)
    table.add_column("Gap≥",     width=6)
    table.add_column("Kelly×",   width=7)
    table.add_column("MaxF",     width=5)
    table.add_column("MinP",     width=5)
    table.add_column("SL%",      width=5)
    table.add_column("TP%",      width=5)
    table.add_column("Sharpe",   width=7, style="bold cyan")
    table.add_column("Return%",  width=9)
    table.add_column("WinRate",  width=8)
    table.add_column("MaxDD%",   width=7)
    table.add_column("Trades",   width=7)
    table.add_column("Balance",  width=9)

    for i, r in enumerate(results[:15], 1):
        ret_color = "green" if r["total_return"] > 0 else "red"
        table.add_row(
            str(i),
            str(r["min_edge_score"]),
            f"{r['min_price_gap']:.2f}",
            f"{r['kelly_scale']:.2f}",
            f"{r['max_fraction']:.2f}",
            f"{r['min_yes_price']:.2f}",
            f"{r['stop_loss_pct']:.0%}",
            f"{r['take_profit_pct']:.0%}",
            f"{r['sharpe']:+.3f}",
            f"[{ret_color}]{r['total_return']:+.1f}%[/{ret_color}]",
            f"{r['win_rate']:.0%}",
            f"{r['max_drawdown']:.1f}%",
            str(r["n_trades"]),
            f"${r['final_balance']:,.0f}",
        )

    console.print(table)

    console.print(f"\n[bold green]Optimal parameters (best Sharpe):[/bold green]")
    console.print(f"  MIN_EDGE_SCORE_TO_TRADE = {best['min_edge_score']}")
    console.print(f"  min_price_gap           = {best['min_price_gap']}")
    console.print(f"  HALF_KELLY / kelly_scale = {best['kelly_scale']}")
    console.print(f"  MAX_KELLY_FRACTION       = {best['max_fraction']}")
    console.print(f"  STOP_LOSS_PCT            = {best['stop_loss_pct']}")
    console.print(f"  TAKE_PROFIT_PCT          = {best['take_profit_pct']}")
    console.print(f"\n  Expected: Sharpe {best['sharpe']:+.3f} | "
                  f"Return {best['total_return']:+.1f}% | "
                  f"Win rate {best['win_rate']:.0%} | "
                  f"Max drawdown {best['max_drawdown']:.1f}%\n")

    return best
