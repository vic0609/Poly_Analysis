"""
Polymarket Edge Monitor — Main Orchestrator
Runs all monitors concurrently 24/7 and writes everything to the DB.

Usage:
    python main.py               # full monitor mode
    python main.py --signals     # one-shot: print top signals
    python main.py --arb         # one-shot: print arbitrage opportunities
    python main.py --whales      # one-shot: print recent whale activity
    python main.py --init        # initialize DB only
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

# Force UTF-8 output on Windows to avoid cp1252 encoding errors
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from rich.console import Console
from rich.logging import RichHandler

import config
from db.database import init_db, get_db, prune_old_data, full_vacuum
from db.models import Market, MarketSnapshot, KalshiMarket, LeaderboardEntry

from scrapers.polymarket import PolymarketScraper
from scrapers.kalshi import KalshiScraper

from monitors.whale import WhaleMonitor
from monitors.arbitrage import ArbitrageMonitor
from monitors.sentiment import SentimentMonitor

from analysis.edge_detector import EdgeDetector
from analysis.signals import get_top_signals, get_top_arbitrage, get_recent_whale_activity

from alerts.notifier import (
    print_signals_table, print_arb_table, print_whale_table,
    alert_signal as _alert_signal,  # noqa: F401 — kept for manual use via CLI
)

from execution.executor import Executor
from execution.settlement import SettlementEngine

# ─── Logging Setup ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
console = Console(highlight=False, emoji=False, stderr=False)


# ═══════════════════════════════════════════════════════════════
# MARKET DATA CYCLE
# ═══════════════════════════════════════════════════════════════

async def market_cycle(poly: PolymarketScraper):
    """Fetch all markets, bulk-upsert to DB, record snapshots."""
    logger.info("⏱  Market data cycle started")

    try:
        raw_markets = await poly.get_all_markets(active_only=True)
    except Exception as exc:
        logger.error("Market fetch failed: %s", exc)
        return

    # Parse all markets first (CPU-only, no DB)
    parsed_markets = []
    for raw in raw_markets:
        try:
            parsed = PolymarketScraper.parse_market(raw)
            if parsed["id"]:
                parsed_markets.append(parsed)
        except Exception as exc:
            logger.debug("parse error: %s", exc)

    logger.info("Parsed %d valid markets, starting bulk upsert...", len(parsed_markets))

    # Bulk upsert using SQLite INSERT OR REPLACE in chunks
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    CHUNK = 1000
    upserted = 0
    snapshot_rows = []

    for i in range(0, len(parsed_markets), CHUNK):
        chunk = parsed_markets[i:i + CHUNK]
        with get_db() as db:
            # Build rows for bulk insert/replace
            rows = []
            for p in chunk:
                rows.append({
                    "id":           p["id"],
                    "slug":         p["slug"],
                    "question":     p["question"],
                    "category":     p["category"],
                    "end_date":     p["end_date"],
                    "is_active":    p["is_active"],
                    "yes_price":    p["yes_price"],
                    "no_price":     p["no_price"],
                    "spread":       p["spread"],
                    "volume_24h":   p["volume_24h"],
                    "volume_total": p["volume_total"],
                    "liquidity":    p["liquidity"],
                    "open_interest":p["open_interest"],
                    "yes_token_id": p["yes_token_id"],
                    "no_token_id":  p["no_token_id"],
                    "updated_at":   datetime.utcnow(),
                })
                # Queue snapshot for active priced markets
                if p["yes_price"] is not None and float(p["yes_price"]) > 0 and p.get("is_active"):
                    snapshot_rows.append({
                        "market_id":    p["id"],
                        "yes_price":    p["yes_price"],
                        "no_price":     p["no_price"],
                        "spread":       p["spread"],
                        "volume_24h":   p["volume_24h"],
                        "liquidity":    p["liquidity"],
                        "open_interest":p["open_interest"],
                    })

            if rows:
                stmt = sqlite_insert(Market).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "yes_price":    stmt.excluded.yes_price,
                        "no_price":     stmt.excluded.no_price,
                        "spread":       stmt.excluded.spread,
                        "volume_24h":   stmt.excluded.volume_24h,
                        "volume_total": stmt.excluded.volume_total,
                        "liquidity":    stmt.excluded.liquidity,
                        "open_interest":stmt.excluded.open_interest,
                        "is_active":    stmt.excluded.is_active,
                        "updated_at":   stmt.excluded.updated_at,
                    },
                )
                db.execute(stmt)
                upserted += len(rows)

        await asyncio.sleep(0)   # yield to event loop between chunks

    # Bulk insert snapshots (append-only, no conflict check needed)
    if snapshot_rows:
        with get_db() as db:
            db.execute(
                MarketSnapshot.__table__.insert(),
                [{**r, "timestamp": datetime.utcnow()} for r in snapshot_rows[:5000]]
            )

    logger.info(
        "Market cycle complete — %d upserted, %d snapshots recorded",
        upserted, len(snapshot_rows),
    )


# ═══════════════════════════════════════════════════════════════
# KALSHI DATA CYCLE
# ═══════════════════════════════════════════════════════════════

async def kalshi_cycle(kalshi: KalshiScraper):
    """Fetch Kalshi markets and update DB."""
    logger.info("⏱  Kalshi data cycle started")

    try:
        raw_markets = await kalshi.get_all_open_markets()
    except Exception as exc:
        logger.error("Kalshi fetch failed: %s", exc)
        return

    with get_db() as db:
        for raw in raw_markets:
            try:
                parsed = KalshiScraper.parse_market(raw)
                if not parsed["ticker"]:
                    continue

                km = db.query(KalshiMarket).filter_by(ticker=parsed["ticker"]).first()
                if not km:
                    km = KalshiMarket(ticker=parsed["ticker"])
                    db.add(km)

                for field, value in parsed.items():
                    if field != "ticker":
                        setattr(km, field, value)

            except Exception as exc:
                logger.debug("Error parsing Kalshi market: %s", exc)

    logger.info("Kalshi cycle complete — %d markets", len(raw_markets))


# ═══════════════════════════════════════════════════════════════
# WHALE CYCLE
# ═══════════════════════════════════════════════════════════════

async def whale_cycle(whale_monitor: WhaleMonitor):
    """Scan on-chain blocks for whale trades + refresh leaderboard periodically."""
    logger.info("⏱  Whale monitor cycle started")

    # Refresh leaderboard every ~10 whale cycles
    if not hasattr(whale_cycle, "_counter"):
        whale_cycle._counter = 0
    whale_cycle._counter += 1

    if whale_cycle._counter % 10 == 1:
        try:
            await whale_monitor.refresh_whale_list_from_leaderboard()
        except Exception as exc:
            logger.warning("Leaderboard refresh failed: %s", exc)

    # On-chain scan
    try:
        raw_trades = whale_monitor.scan_new_blocks()
        if raw_trades:
            whale_monitor.persist_trades(raw_trades)
    except Exception as exc:
        logger.error("On-chain scan error: %s", exc)

    # Global activity feed — catches ALL large trades without needing known addresses
    try:
        await whale_monitor.poll_global_whale_activity()
    except Exception as exc:
        logger.warning("Global whale poll error: %s", exc)

    # Per-wallet fallback for any known tracked whales
    try:
        await whale_monitor.poll_whale_activity_via_api()
    except Exception as exc:
        logger.warning("API whale poll error: %s", exc)


# ═══════════════════════════════════════════════════════════════
# SENTIMENT CYCLE
# ═══════════════════════════════════════════════════════════════

async def sentiment_cycle(sentiment_monitor: SentimentMonitor):
    """Collect social sentiment for top markets."""
    import traceback
    logger.info("⏱  Sentiment cycle started")
    try:
        await sentiment_monitor.run_cycle()
    except Exception as exc:
        logger.error("Sentiment cycle error: %s\n%s", exc, traceback.format_exc())


# ═══════════════════════════════════════════════════════════════
# ARBITRAGE CYCLE
# ═══════════════════════════════════════════════════════════════

def arb_cycle(arb_monitor: ArbitrageMonitor):
    """Detect cross-platform and mechanical arbitrage."""
    logger.info("⏱  Arbitrage cycle started")
    try:
        cross = arb_monitor.detect_opportunities()
        mechanical = arb_monitor.detect_mechanical_arb()

        pass  # arb opps saved to DB; no push alert (only executed trades notify)

        total = len(cross) + len(mechanical)
        if total:
            logger.info(
                "Arb cycle: %d cross-platform, %d mechanical",
                len(cross), len(mechanical),
            )
    except Exception as exc:
        logger.error("Arb cycle error: %s", exc)


# ═══════════════════════════════════════════════════════════════
# EDGE DETECTION CYCLE
# ═══════════════════════════════════════════════════════════════

def edge_cycle(detector: EdgeDetector, executor: Executor):
    """Run edge detection, alert on top signals, and execute via Kelly sizing."""
    import traceback
    logger.info("⏱  Edge detection cycle started")
    try:
        signals = detector.run()
        top = [s for s in signals if s["edge_score"] >= 40]
        in_range = [s for s in top if 0.20 <= (s.get("current_price") or 0) <= 0.75]
        logger.info(
            "edge_cycle: %d total signals, %d score>=40, %d in price range 0.20-0.75",
            len(signals), len(top), len(in_range),
        )
        for sig in in_range[:50]:
            executor.evaluate_and_execute(sig)
        if top and sys.stdout.isatty():
            try:
                print_signals_table(top[:20])
            except Exception:
                pass
    except Exception as exc:
        logger.error("Edge detection cycle error: %s\n%s", exc, traceback.format_exc())


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

async def run_monitor():
    """Main 24/7 monitoring loop."""
    console.print("=" * 60)
    console.print("[bold cyan]  Polymarket Edge Monitor Starting[/bold cyan]")
    console.print("=" * 60)
    init_db()
    logger.info("Database initialized at %s", config.DATABASE_URL)

    async with aiohttp.ClientSession() as session:
        poly = PolymarketScraper(session)
        kalshi = KalshiScraper(session)
        whale_monitor = WhaleMonitor(poly_scraper=poly)
        sentiment_monitor = SentimentMonitor(aiohttp_session=session)
        arb_monitor = ArbitrageMonitor()
        detector = EdgeDetector()
        executor = Executor()
        settlement = SettlementEngine(poly_scraper=poly)

        # Track last-run times
        last_run = {
            "market":     0.0,
            "kalshi":     0.0,
            "whale":      0.0,
            "sentiment":  0.0,
            "arb":        0.0,
            "edge":       0.0,
            "settlement": 0.0,
            "prune":      0.0,   # daily DB cleanup
        }
        PRUNE_INTERVAL  = 86_400   # 24h in seconds
        VACUUM_INTERVAL = 604_800  # 7 days in seconds

        console.print("[green]All monitors initialized. Starting main loop...[/green]")
        console.print(f"  Market poll:    every {config.MARKET_POLL_INTERVAL}s")
        console.print(f"  Whale poll:     every {config.WHALE_POLL_INTERVAL}s")
        console.print(f"  Sentiment poll: every {config.SENTIMENT_POLL_INTERVAL}s")
        console.print(f"  Arb poll:       every {config.ARBITRAGE_POLL_INTERVAL}s")
        console.print(f"  Edge detect:    every {config.EDGE_DETECTION_INTERVAL}s")
        console.print("=" * 60)

        loop_count = 0
        while True:
            now = asyncio.get_event_loop().time()
            loop_count += 1

            # ── Heavy writers run sequentially to avoid SQLite write contention ──
            # market_cycle and kalshi_cycle each bulk-upsert thousands of rows;
            # running them concurrently causes WAL lock collisions.
            if now - last_run["market"] >= config.MARKET_POLL_INTERVAL:
                await market_cycle(poly)
                last_run["market"] = asyncio.get_event_loop().time()

            if now - last_run["kalshi"] >= config.MARKET_POLL_INTERVAL:
                await kalshi_cycle(kalshi)
                last_run["kalshi"] = asyncio.get_event_loop().time()

            # ── Light writers can run concurrently (small row counts) ──
            light_tasks = []
            if now - last_run["whale"] >= config.WHALE_POLL_INTERVAL:
                light_tasks.append(whale_cycle(whale_monitor))
                last_run["whale"] = now
            if now - last_run["sentiment"] >= config.SENTIMENT_POLL_INTERVAL:
                light_tasks.append(sentiment_cycle(sentiment_monitor))
                last_run["sentiment"] = now
            if light_tasks:
                await asyncio.gather(*light_tasks, return_exceptions=True)

            # Sync tasks (run after async to have fresh data)
            if now - last_run["arb"] >= config.ARBITRAGE_POLL_INTERVAL:
                arb_cycle(arb_monitor)
                last_run["arb"] = now

            if now - last_run["edge"] >= config.EDGE_DETECTION_INTERVAL:
                edge_cycle(detector, executor)
                last_run["edge"] = now

            if now - last_run["settlement"] >= config.SETTLEMENT_INTERVAL:
                await settlement.run_cycle()
                last_run["settlement"] = now

            # ── Daily DB prune (weekly VACUUM) ────────────────
            if now - last_run["prune"] >= PRUNE_INTERVAL:
                do_vacuum = (now - last_run["prune"]) >= VACUUM_INTERVAL
                try:
                    prune_old_data(vacuum=do_vacuum)
                except Exception as exc:
                    logger.error("DB prune error: %s", exc)
                last_run["prune"] = now

            # Brief status log every 10 loops
            if loop_count % 10 == 0:
                with get_db() as db:
                    market_count = db.query(Market).filter(Market.is_active == True).count()
                logger.info(
                    "Loop #%d | Active markets: %d | %s UTC",
                    loop_count, market_count,
                    datetime.utcnow().strftime("%H:%M:%S"),
                )

            await asyncio.sleep(5)   # poll every 5s, run tasks on schedule


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINTS
# ═══════════════════════════════════════════════════════════════

def cmd_signals():
    init_db()
    signals = get_top_signals(limit=30, min_score=20)
    print_signals_table(signals)
    if not signals:
        console.print("[yellow]No signals. Run 'python main.py' first to collect data.[/yellow]")


def cmd_arb():
    init_db()
    opps = get_top_arbitrage(limit=20)
    print_arb_table(opps)
    if not opps:
        console.print("[yellow]No arb opportunities. Run the monitor first.[/yellow]")


def cmd_whales():
    init_db()
    trades = get_recent_whale_activity(hours=24, limit=50)
    print_whale_table(trades)
    if not trades:
        console.print("[yellow]No whale activity. Run the monitor first.[/yellow]")


def cmd_sim():
    """Print paper trading simulation performance report."""
    init_db()
    from rich.table import Table
    from rich import box
    from db.models import ExecutedTrade, Position

    with get_db() as db:
        trades = [
            {
                "executed_at":   t.executed_at,
                "side":          t.side,
                "size_usdc":     t.size_usdc,
                "price":         t.price,
                "kelly_fraction":t.kelly_fraction,
                "edge_score":    t.edge_score,
                "signal_type":   t.signal_type,
                "market_id":     t.market_id,
                "realized_pnl":  t.realized_pnl,
            }
            for t in db.query(ExecutedTrade).filter_by(is_paper=True)
            .order_by(ExecutedTrade.executed_at.desc()).all()
        ]
        open_pos_count = db.query(Position).filter_by(status="open", is_paper=True).count()

    if not trades:
        console.print("[yellow]No paper trades yet. Run the monitor first.[/yellow]")
        return

    total_deployed = sum(t["size_usdc"] or 0 for t in trades)
    realized_pnl = sum(t["realized_pnl"] or 0 for t in trades)
    wins = [t for t in trades if (t["realized_pnl"] or 0) > 0]
    losses = [t for t in trades if (t["realized_pnl"] or 0) < 0]

    console.print("\n[bold cyan]═══ Paper Trading Simulation Report ═══[/bold cyan]")
    console.print(f"  Total trades:      {len(trades)}")
    console.print(f"  Open positions:    {open_pos_count}")
    console.print(f"  Total deployed:    ${total_deployed:,.2f}")
    console.print(f"  Realized P&L:      ${realized_pnl:+,.2f}")
    console.print(f"  Wins / Losses:     {len(wins)} / {len(losses)}")
    win_rate = len(wins) / len(trades) if trades else 0
    console.print(f"  Win rate:          {win_rate:.0%}")
    avg_kelly = sum(t["kelly_fraction"] or 0 for t in trades) / len(trades)
    console.print(f"  Avg Kelly fraction:{avg_kelly:.1%}")

    table = Table(title="Recent Paper Trades", box=box.ROUNDED, show_lines=True)
    table.add_column("Time",   min_width=10)
    table.add_column("Side",   min_width=5)
    table.add_column("Size",   min_width=9)
    table.add_column("Price",  min_width=7)
    table.add_column("Kelly",  min_width=7)
    table.add_column("Score",  min_width=6)
    table.add_column("Signal", min_width=22)
    table.add_column("Market", min_width=20)

    for t in trades[:20]:
        color = "green" if t["side"] == "YES" else "red"
        table.add_row(
            t["executed_at"].strftime("%H:%M:%S"),
            f"[{color}]{t['side']}[/{color}]",
            f"${t['size_usdc']:.2f}",
            f"{t['price']:.3f}",
            f"{t['kelly_fraction']:.1%}",
            f"{t['edge_score']:.1f}",
            t["signal_type"] or "?",
            (t["market_id"] or "")[:45],
        )
    console.print(table)


def _apply_best_params(best: dict):
    """Write optimal sweep params back to .env."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "r") as f:
        content = f.read()

    replacements = {
        "MIN_EDGE_SCORE_TO_TRADE": str(int(best["min_edge_score"])),
        "MAX_KELLY_FRACTION":      str(best["max_fraction"]),
    }
    # kelly_scale 0.5 = half kelly, 1.0 = full kelly, 0.25 = quarter
    half_kelly = "true" if best["kelly_scale"] <= 0.5 else "false"
    replacements["HALF_KELLY"] = half_kelly

    import re
    for key, val in replacements.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)

    with open(env_path, "w") as f:
        f.write(content)

    console.print(f"\n[green]Optimal params written to .env:[/green]")
    for k, v in replacements.items():
        console.print(f"  {k} = {v}")
    console.print("[yellow]Restart the monitor to apply.[/yellow]\n")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Edge Monitor")
    parser.add_argument("--signals", action="store_true", help="Print top edge signals")
    parser.add_argument("--arb", action="store_true", help="Print arbitrage opportunities")
    parser.add_argument("--whales", action="store_true", help="Print recent whale activity")
    parser.add_argument("--sim", action="store_true", help="Print paper trading simulation report")
    parser.add_argument("--sweep", action="store_true", help="Grid search for optimal entry parameters")
    parser.add_argument("--apply", action="store_true", help="Auto-apply best params to .env after --sweep")
    parser.add_argument("--init", action="store_true", help="Initialize database only")
    parser.add_argument("--live-check", action="store_true", help="Check live-trading readiness criteria")
    parser.add_argument("--prune", action="store_true", help="Prune old DB rows (snapshots >7d, signals >7d, kalshi >3d)")
    parser.add_argument("--vacuum", action="store_true", help="Full VACUUM to reclaim disk space (stop monitor first)")
    args = parser.parse_args()

    if args.live_check:
        from execution.live_check import run_live_check
        init_db()
        run_live_check()
    elif args.signals:
        cmd_signals()
    elif args.arb:
        cmd_arb()
    elif args.whales:
        cmd_whales()
    elif args.sim:
        cmd_sim()
    elif args.sweep:
        from execution.param_sweep import run_sweep, print_sweep_results
        init_db()
        results = run_sweep()
        best = print_sweep_results(results)
        if best and args.apply:
            _apply_best_params(best)
    elif args.prune:
        init_db()
        result = prune_old_data()
        total = sum(result.values())
        console.print(f"[green]Pruned {total:,} rows: {result}[/green]")
    elif args.vacuum:
        console.print("[yellow]Running full VACUUM (ensure monitor is stopped)...[/yellow]")
        full_vacuum()
        console.print("[green]VACUUM complete.[/green]")
    elif args.init:
        init_db()
        console.print("[green]Database initialized successfully.[/green]")
    else:
        try:
            asyncio.run(run_monitor())
        except KeyboardInterrupt:
            console.print("\n[yellow]Monitor stopped by user.[/yellow]")


if __name__ == "__main__":
    main()
