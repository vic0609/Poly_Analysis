"""Alert system — console, Discord webhook, and Telegram."""

import json
import logging
from datetime import datetime

import requests
from rich.console import Console
from rich.table import Table
from rich import box

from config import DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
console = Console(highlight=False, emoji=False)


def _format_signal(signal: dict) -> str:
    """Format a signal dict into a readable string for external alerts."""
    direction_emoji = {"YES": "🟢", "NO": "🔴", "NEUTRAL": "⚪"}.get(signal.get("direction"), "⚪")
    return (
        f"{direction_emoji} *EDGE SIGNAL* [{signal.get('signal_type', '?')}]\n"
        f"Market: {signal.get('question', 'Unknown')[:80]}\n"
        f"Category: {signal.get('category', '?')} | Edge Score: {signal.get('edge_score', 0):.1f}/100\n"
        f"Direction: {signal.get('direction', '?')} | Confidence: {signal.get('confidence', 0):.0%}\n"
        f"Price: {signal.get('current_price', 0):.3f} → Fair: {signal.get('implied_fair_price', 0):.3f}\n"
        f"Notes: {signal.get('notes', '')}\n"
        f"Vol 24h: ${signal.get('volume_24h', 0):,.0f} | Liquidity: ${signal.get('liquidity', 0):,.0f}"
    )


def _format_arb(opp: dict) -> str:
    return (
        f"⚡ *ARB OPPORTUNITY* [{opp.get('arb_type', '?')}]\n"
        f"{opp.get('description', '')[:100]}\n"
        f"Poly YES: {opp.get('poly_yes_price', 0):.3f} | Kalshi YES: {opp.get('kalshi_yes_price', 0):.3f}\n"
        f"Gap: {opp.get('price_gap', 0):.3f} | Est Profit: {opp.get('profit_pct', 0):.2f}%\n"
        f"Direction: {opp.get('direction', '?')}"
    )


# ─── Console (Rich) ───────────────────────────────────────────

def print_signals_table(signals: list[dict]):
    """Print edge signals as a rich table to the terminal."""
    if not signals:
        console.print("[yellow]No active edge signals[/yellow]")
        return

    table = Table(
        title=f"[bold cyan]Edge Signals — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Score", style="bold green", width=7)
    table.add_column("Dir", width=6)
    table.add_column("Type", width=24)
    table.add_column("Category", width=12)
    table.add_column("Price", width=7)
    table.add_column("Fair", width=7)
    table.add_column("Confidence", width=11)
    table.add_column("Question", width=50)

    for s in signals:
        dir_color = {"YES": "green", "NO": "red", "NEUTRAL": "white"}.get(s.get("direction"), "white")
        table.add_row(
            f"{s.get('edge_score', 0):.1f}",
            f"[{dir_color}]{s.get('direction', '?')}[/{dir_color}]",
            s.get("signal_type", "?"),
            s.get("category", "?"),
            f"{s.get('current_price', 0):.3f}",
            f"{s.get('implied_fair_price', 0):.3f}" if s.get("implied_fair_price") else "—",
            f"{s.get('confidence', 0):.0%}",
            s.get("question", "?")[:50],
        )

    console.print(table)


def print_arb_table(opportunities: list[dict]):
    """Print arbitrage opportunities as a rich table."""
    if not opportunities:
        return

    table = Table(
        title="[bold yellow]Arbitrage Opportunities[/bold yellow]",
        box=box.ROUNDED,
    )
    table.add_column("Profit %", style="bold yellow", width=9)
    table.add_column("Type", width=14)
    table.add_column("Poly YES", width=9)
    table.add_column("Kalshi YES", width=10)
    table.add_column("Gap", width=6)
    table.add_column("Description", width=60)

    for o in opportunities:
        table.add_row(
            f"{o.get('profit_pct', 0):.2f}%",
            o.get("arb_type", "?"),
            f"{o.get('poly_yes_price', 0):.3f}",
            f"{o.get('kalshi_yes_price', 0):.3f}",
            f"{o.get('price_gap', 0):.3f}",
            o.get("description", "?")[:60],
        )

    console.print(table)


def print_whale_table(trades: list[dict]):
    """Print recent whale trades."""
    if not trades:
        return

    table = Table(
        title="[bold magenta]Recent Whale Activity[/bold magenta]",
        box=box.ROUNDED,
    )
    table.add_column("Time", width=18)
    table.add_column("Wallet", width=12)
    table.add_column("Action", width=8)
    table.add_column("Side", width=5)
    table.add_column("Size USDC", width=12)
    table.add_column("Price", width=7)
    table.add_column("Market", width=40)

    for t in trades:
        ts = t.get("timestamp")
        ts_str = ts.strftime("%m-%d %H:%M UTC") if ts else "?"
        wallet = t.get("wallet", "?")
        wallet_short = f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 12 else wallet
        action_color = "green" if t.get("action") == "BUY" else "red"

        table.add_row(
            ts_str,
            wallet_short,
            f"[{action_color}]{t.get('action', '?')}[/{action_color}]",
            t.get("side", "?"),
            f"${t.get('size_usdc', 0):,.0f}",
            f"{t.get('price', 0):.3f}",
            str(t.get("market_id", "?"))[:40],
        )

    console.print(table)


# ─── Discord ──────────────────────────────────────────────────

def send_discord_alert(message: str, username: str = "Polymarket Edge Bot"):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"content": message, "username": username}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            logger.warning("Discord alert failed: %s", resp.status_code)
    except Exception as exc:
        logger.error("Discord alert error: %s", exc)


def alert_signal(signal: dict):
    """Log signal to console only — Telegram reserved for trade executions."""
    if signal.get("edge_score", 0) < 40:
        return
    msg = _format_signal(signal)
    console.print(f"\n[bold red]ALERT:[/bold red] {msg}\n")
    send_discord_alert(msg)


def alert_arbitrage(opp: dict):
    if opp.get("profit_pct", 0) < 2.0:
        return
    msg = _format_arb(opp)
    console.print(f"\n[bold yellow]ARB ALERT:[/bold yellow] {msg}\n")
    send_discord_alert(msg)


# ─── Telegram ─────────────────────────────────────────────────

def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # Use HTML parse_mode — handles $, brackets, and emojis safely
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        if resp.status_code == 400:
            # HTML parse error — retry as plain text
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            }, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram alert failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("Telegram alert error: %s", exc)
