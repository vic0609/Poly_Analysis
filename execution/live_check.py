"""
Live-trading readiness check — run with: python main.py --live-check

Evaluates the last EVAL_DAYS of paper trading against a set of hard
pass/fail criteria. All criteria must pass before going live is recommended.
"""

import os
from collections import defaultdict
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table
from rich import box

from db.database import get_db
from db.models import ExecutedTrade, Position

console = Console(highlight=False, emoji=False)

# ── Evaluation window ────────────────────────────────────────────
EVAL_DAYS = 14          # look at the most recent N days of paper trades
MIN_TRADES = 30         # minimum closed trades to have statistical confidence
MIN_ACTIVE_DAYS = 5     # minimum distinct days with trades in window

# ── Pass thresholds ──────────────────────────────────────────────
MIN_ROI_PCT          = 2.0    # ROI on capital wagered (%)
MIN_WIN_RATE         = 0.44   # 44%
MIN_WIN_LOSS_RATIO   = 1.10   # avg win / avg loss
MAX_DAILY_LOSS_PCT   = 0.10   # worst single day <= 10% of bankroll
MAX_CONSEC_LOSS_DAYS = 3      # no more than N back-to-back losing days
MIN_PRIMARY_ROI      = 0.0    # volume_spike ROI must be positive
BANKROLL             = 1000.0


def _check(label: str, passed: bool, detail: str) -> dict:
    return {"label": label, "passed": passed, "detail": detail}


def run_live_check() -> bool:
    """
    Run all readiness criteria. Prints a formatted table and returns
    True if every criterion passes, False otherwise.
    """
    cutoff = datetime.utcnow() - timedelta(days=EVAL_DAYS)

    with get_db() as db:
        all_closed = [
            {
                "executed_at": t.executed_at,
                "realized_pnl": t.realized_pnl,
                "size_usdc": t.size_usdc or 0,
                "signal_type": t.signal_type or "unknown",
                "side": t.side or "?",
                "edge_score": t.edge_score or 0,
            }
            for t in db.query(ExecutedTrade)
            .filter(
                ExecutedTrade.is_paper == True,
                ExecutedTrade.realized_pnl != None,
            )
            .all()
        ]

    recent = [t for t in all_closed if t["executed_at"] >= cutoff]
    wins   = [t for t in recent if t["realized_pnl"] > 0]
    losses = [t for t in recent if t["realized_pnl"] <= 0]

    total_wagered = sum(t["size_usdc"] for t in recent)
    total_pnl     = sum(t["realized_pnl"] for t in recent)
    roi_pct       = total_pnl / total_wagered * 100 if total_wagered else 0
    win_rate      = len(wins) / len(recent) if recent else 0
    avg_win       = sum(t["realized_pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss      = sum(t["realized_pnl"] for t in losses) / len(losses) if losses else 0
    wl_ratio      = abs(avg_win / avg_loss) if avg_loss else 0

    # Daily P&L
    daily: dict[str, float] = defaultdict(float)
    for t in recent:
        day = t["executed_at"].strftime("%Y-%m-%d")
        daily[day] += t["realized_pnl"]

    active_days   = len(daily)
    worst_day_pnl = min(daily.values()) if daily else 0
    worst_day_pct = abs(worst_day_pnl) / BANKROLL * 100 if worst_day_pnl < 0 else 0

    # Consecutive losing days
    sorted_days = sorted(daily.items())
    max_consec_loss = 0
    cur_consec = 0
    for _, pnl in sorted_days:
        if pnl < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Primary signal ROI
    vs_type: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "wagered": 0.0})
    for t in recent:
        vs_type[t["signal_type"]]["pnl"]     += t["realized_pnl"]
        vs_type[t["signal_type"]]["wagered"] += t["size_usdc"]
    vol_roi = (
        vs_type["volume_spike"]["pnl"] / vs_type["volume_spike"]["wagered"] * 100
        if vs_type["volume_spike"]["wagered"] > 0 else 0
    )

    # Any active signal type with negative ROI
    bad_signals = [
        k for k, v in vs_type.items()
        if v["wagered"] > 0 and v["pnl"] / v["wagered"] < -0.01 and v["wagered"] > 100
    ]

    # Config / credentials
    pk_set     = bool(os.getenv("POLYMARKET_PRIVATE_KEY", "").strip())
    api_set    = bool(os.getenv("POLYMARKET_API_KEY", "").strip())
    clob_avail = False
    try:
        import py_clob_client  # noqa: F401
        clob_avail = True
    except ImportError:
        pass

    # ── Build criteria list ──────────────────────────────────────
    criteria = [
        _check(
            f"Sufficient data  (>={MIN_TRADES} trades in {EVAL_DAYS}d)",
            len(recent) >= MIN_TRADES,
            f"{len(recent)} closed trades in last {EVAL_DAYS} days",
        ),
        _check(
            f"Active days  (>={MIN_ACTIVE_DAYS} days with trades)",
            active_days >= MIN_ACTIVE_DAYS,
            f"{active_days} active trading days",
        ),
        _check(
            f"ROI on wagered  (>={MIN_ROI_PCT:.1f}%)",
            roi_pct >= MIN_ROI_PCT,
            f"{roi_pct:+.2f}%  (${total_pnl:+.2f} on ${total_wagered:,.0f} wagered)",
        ),
        _check(
            f"Win rate  (>={MIN_WIN_RATE:.0%})",
            win_rate >= MIN_WIN_RATE,
            f"{win_rate:.1%}  ({len(wins)}W / {len(losses)}L)",
        ),
        _check(
            f"Win/loss ratio  (>={MIN_WIN_LOSS_RATIO:.2f}x)",
            wl_ratio >= MIN_WIN_LOSS_RATIO,
            f"{wl_ratio:.2f}x  (avg win ${avg_win:+.2f} / avg loss ${avg_loss:+.2f})",
        ),
        _check(
            f"Worst single day  (<={MAX_DAILY_LOSS_PCT:.0%} of bankroll)",
            worst_day_pct <= MAX_DAILY_LOSS_PCT * 100,
            f"Worst day: ${worst_day_pnl:+.2f}  ({worst_day_pct:.1f}% of bankroll)"
            if daily else "No daily data",
        ),
        _check(
            f"Max consecutive losing days  (<={MAX_CONSEC_LOSS_DAYS})",
            max_consec_loss <= MAX_CONSEC_LOSS_DAYS,
            f"{max_consec_loss} consecutive losing day(s)",
        ),
        _check(
            f"Primary signal profitable  (volume_spike ROI > 0%)",
            vol_roi > MIN_PRIMARY_ROI,
            f"volume_spike ROI: {vol_roi:+.1f}%",
        ),
        _check(
            "No active signal type losing money  (ROI > -1%)",
            len(bad_signals) == 0,
            f"Losing signals: {', '.join(bad_signals)}" if bad_signals else "All signal types profitable",
        ),
        _check(
            "POLYMARKET_PRIVATE_KEY set",
            pk_set,
            "Set in .env" if pk_set else "Missing — required for live order signing",
        ),
        _check(
            "POLYMARKET_API_KEY set",
            api_set,
            "Set in .env" if api_set else "Missing — get from Polymarket CLOB dashboard",
        ),
        _check(
            "py-clob-client installed",
            clob_avail,
            "Installed" if clob_avail else "Run: pip install py-clob-client",
        ),
    ]

    passed = sum(1 for c in criteria if c["passed"])
    total  = len(criteria)
    all_pass = passed == total

    # ── Print ────────────────────────────────────────────────────
    console.print()
    console.print(f"[bold]Live-Trading Readiness Check[/bold]  "
                  f"(last {EVAL_DAYS} days — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})")
    console.print()

    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    table.add_column("",        width=4,  no_wrap=True)
    table.add_column("Criterion", width=46, no_wrap=False)
    table.add_column("Detail",    width=52, no_wrap=False)

    for c in criteria:
        icon   = "[bold green]PASS[/bold green]" if c["passed"] else "[bold red]FAIL[/bold red]"
        label  = c["label"]
        detail = c["detail"]
        if not c["passed"]:
            label  = f"[red]{label}[/red]"
            detail = f"[red]{detail}[/red]"
        else:
            detail = f"[dim]{detail}[/dim]"
        table.add_row(icon, label, detail)

    console.print(table)

    # Summary bar
    bar_filled = int(passed / total * 30)
    bar = "[green]" + "#" * bar_filled + "[/green]" + "[dim]" + "-" * (30 - bar_filled) + "[/dim]"
    console.print(f"  Score: {bar}  {passed}/{total} criteria met")
    console.print()

    if all_pass:
        console.print("[bold green]RECOMMENDATION: READY TO GO LIVE[/bold green]")
        console.print()
        console.print("  To enable live trading:")
        console.print("  1. Set PAPER_TRADING=false in .env")
        console.print("  2. Set BANKROLL_USDC to your actual USDC balance")
        console.print("  3. Ensure your Polygon wallet has enough USDC")
        console.print("  4. Restart the monitor")
        console.print()
        console.print("  [yellow]Start small — reduce BANKROLL_USDC and MAX_POSITION_USDC[/yellow]")
        console.print("  [yellow]first week. Monitor logs closely.[/yellow]")
    else:
        failing = [c["label"].replace("[red]", "").replace("[/red]", "") for c in criteria if not c["passed"]]
        console.print("[bold red]RECOMMENDATION: NOT READY — keep paper trading[/bold red]")
        console.print()
        console.print(f"  {total - passed} criterion/criteria failing:")
        for f in failing:
            console.print(f"  [red]-[/red] {f}")
        console.print()

        # Days remaining estimate
        if len(recent) < MIN_TRADES:
            trades_per_day = len(recent) / max(active_days, 1)
            days_needed = (MIN_TRADES - len(recent)) / trades_per_day if trades_per_day else 99
            console.print(f"  Estimated days to reach min trades at current rate: "
                          f"~{days_needed:.0f} day(s)")

    console.print()
    return all_pass
