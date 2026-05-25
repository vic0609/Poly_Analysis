"""
Polymarket Edge — Telegram Bot
Bidirectional: sends alerts TO you + accepts commands FROM you.

Commands:
  /start    — welcome message + help
  /signals  — top active edge signals
  /arb      — best arbitrage opportunities
  /whales   — recent whale trades (last 6h)
  /status   — monitor health + DB stats
  /top [n]  — top N markets by 24h volume
  /market <query> — search for a specific market
  /help     — list all commands

Run alongside the monitor:
    .venv\\Scripts\\python.exe telegram_bot.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

# Force UTF-8 on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db.database import init_db, get_db
from db.models import Market, EdgeSignal, ArbitrageOpportunity, WhaleTrade, WhaleWallet
from analysis.signals import get_top_signals, get_top_arbitrage, get_recent_whale_activity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Formatting helpers ───────────────────────────────────────

def fmt_price(p) -> str:
    return f"{float(p):.3f}" if p is not None else "?"

def fmt_usd(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def fmt_pct(v) -> str:
    return f"{float(v):.1f}%" if v is not None else "?"

def direction_emoji(d: str) -> str:
    return {"YES": "🟢", "NO": "🔴", "NEUTRAL": "⚪"}.get(d or "", "⚪")

def signal_type_label(t: str) -> str:
    return {
        "price_sentiment_divergence": "Sentiment Divergence",
        "whale_accumulation":         "Whale Accumulation",
        "volume_spike":               "Volume Spike",
        "arbitrage":                  "Arb Signal",
        "base_rate_mismatch":         "Base Rate Mismatch",
        "composite":                  "Composite",
    }.get(t or "", t or "?")


# ─── Command handlers ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Polymarket Edge Monitor Bot*\n\n"
        "I monitor 50,000+ markets 24/7 and alert you to:\n"
        "- Edge signals (mispriced markets)\n"
        "- Arbitrage opportunities\n"
        "- Whale wallet activity\n\n"
        "*Commands:*\n"
        "/signals — top edge signals\n"
        "/arb — arbitrage opportunities\n"
        "/whales — recent whale activity\n"
        "/top — top markets by volume\n"
        "/market <query> — search markets\n"
        "/status — system health\n"
        "/help — this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching top edge signals...")
    signals = get_top_signals(limit=10, min_score=20)

    if not signals:
        await update.message.reply_text(
            "No active edge signals yet. Monitor needs more data — check back in a few minutes."
        )
        return

    lines = ["*Top Edge Signals*\n"]
    for i, s in enumerate(signals[:8], 1):
        de = direction_emoji(s.get("direction"))
        lines.append(
            f"{i}. {de} *Score: {s['edge_score']:.0f}* | {signal_type_label(s['signal_type'])}\n"
            f"   {s['question'][:65]}\n"
            f"   Price: {fmt_price(s['current_price'])} -> Fair: {fmt_price(s['implied_fair_price'])} | "
            f"Vol: {fmt_usd(s['volume_24h'])} | Conf: {fmt_pct(s['confidence'])}\n"
            f"   _{s.get('notes', '')[:80]}_\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_arb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scanning arbitrage opportunities...")
    opps = get_top_arbitrage(limit=8)

    if not opps:
        await update.message.reply_text(
            "No active arbitrage opportunities detected. Market is efficient right now — keep monitoring."
        )
        return

    lines = ["*Arbitrage Opportunities*\n"]
    for i, o in enumerate(opps[:6], 1):
        lines.append(
            f"{i}. *{o['profit_pct']:.2f}% profit* | {o['arb_type']}\n"
            f"   {o['description'][:70]}\n"
            f"   Poly YES: {fmt_price(o['poly_yes_price'])} | Kalshi YES: {fmt_price(o['kalshi_yes_price'])}\n"
            f"   Gap: {fmt_price(o['price_gap'])} | Direction: {o['direction']}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_whales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pulling recent whale activity...")
    trades = get_recent_whale_activity(hours=6, limit=20)

    if not trades:
        await update.message.reply_text(
            "No whale trades recorded in the last 6 hours. Run the monitor longer to accumulate data."
        )
        return

    lines = ["*Whale Activity (last 6h)*\n"]
    total_buy = sum(t["size_usdc"] for t in trades if t["action"] == "BUY")
    total_sell = sum(t["size_usdc"] for t in trades if t["action"] == "SELL")
    lines.append(f"Net pressure: Buy {fmt_usd(total_buy)} vs Sell {fmt_usd(total_sell)}\n")

    for t in trades[:8]:
        action_e = "🟢 BUY" if t["action"] == "BUY" else "🔴 SELL"
        ts = t["timestamp"].strftime("%H:%M") if t.get("timestamp") else "?"
        wallet = t["wallet"]
        wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet or "") > 12 else wallet
        lines.append(
            f"{action_e} {t['side']} | {fmt_usd(t['size_usdc'])} @ {fmt_price(t['price'])}\n"
            f"   {wallet_short} | {ts} UTC\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 10
    if ctx.args:
        try:
            n = min(int(ctx.args[0]), 20)
        except ValueError:
            pass

    with get_db() as db:
        markets = (
            db.query(Market)
            .filter(Market.is_active == True, Market.yes_price != None)
            .order_by(Market.volume_24h.desc())
            .limit(n)
            .all()
        )
        market_data = [
            {
                "question": m.question,
                "yes_price": m.yes_price,
                "volume_24h": m.volume_24h,
                "liquidity": m.liquidity,
                "category": m.category,
            }
            for m in markets
        ]

    if not market_data:
        await update.message.reply_text("No market data yet. Monitor is still loading.")
        return

    lines = [f"*Top {n} Markets by 24h Volume*\n"]
    for i, m in enumerate(market_data, 1):
        lines.append(
            f"{i}. *{fmt_usd(m['volume_24h'])}* vol | YES: {fmt_price(m['yes_price'])}\n"
            f"   {m['question'][:65]}\n"
            f"   Liq: {fmt_usd(m['liquidity'])} | {m['category'] or 'N/A'}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_market(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /market <search query>\nExample: /market trump election")
        return

    query = " ".join(ctx.args).lower()

    with get_db() as db:
        markets = (
            db.query(Market)
            .filter(
                Market.is_active == True,
                Market.question.ilike(f"%{query}%"),
            )
            .order_by(Market.volume_24h.desc())
            .limit(5)
            .all()
        )
        results = [
            {
                "question": m.question,
                "yes_price": m.yes_price,
                "no_price": m.no_price,
                "volume_24h": m.volume_24h,
                "liquidity": m.liquidity,
                "category": m.category,
                "end_date": m.end_date,
            }
            for m in markets
        ]

    if not results:
        await update.message.reply_text(f"No active markets found matching '{query}'")
        return

    lines = [f"*Markets matching '{query}'*\n"]
    for m in results:
        end = m["end_date"].strftime("%Y-%m-%d") if m.get("end_date") else "?"
        lines.append(
            f"*{m['question'][:70]}*\n"
            f"   YES: {fmt_price(m['yes_price'])} | NO: {fmt_price(m['no_price'])}\n"
            f"   Vol 24h: {fmt_usd(m['volume_24h'])} | Liq: {fmt_usd(m['liquidity'])}\n"
            f"   Category: {m['category'] or '?'} | Ends: {end}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as db:
        total_markets = db.query(Market).count()
        active_markets = db.query(Market).filter(Market.is_active == True).count()
        priced_markets = db.query(Market).filter(Market.yes_price != None).count()
        kalshi_count = db.query(__import__("db.models", fromlist=["KalshiMarket"]).KalshiMarket).count()
        active_signals = db.query(EdgeSignal).filter(EdgeSignal.is_active == True).count()
        active_arbs = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.is_active == True).count()
        whale_wallets = db.query(WhaleWallet).count()
        recent_whale_trades = db.query(WhaleTrade).filter(
            WhaleTrade.timestamp >= datetime.utcnow() - timedelta(hours=24)
        ).count()

    text = (
        f"*Monitor Status* — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"*Markets*\n"
        f"  Polymarket total: {total_markets:,}\n"
        f"  Active w/ prices: {priced_markets:,}\n"
        f"  Kalshi markets:   {kalshi_count:,}\n\n"
        f"*Signals*\n"
        f"  Active edge signals: {active_signals}\n"
        f"  Active arb opps:     {active_arbs}\n\n"
        f"*Whales*\n"
        f"  Tracked wallets: {whale_wallets}\n"
        f"  Trades (24h):    {recent_whale_trades}\n\n"
        f"*Monitor:* Running 24/7"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Unknown command. Type /help for the list of commands."
    )


# ─── Proactive alert sender ───────────────────────────────────

async def send_alert(bot, text: str):
    """Push an alert to the configured chat ID."""
    if not TELEGRAM_CHAT_ID:
        return
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("Telegram send error: %s", exc)


async def alert_loop(app):
    """Background task: periodically push top signals and arbs as alerts."""
    await asyncio.sleep(30)   # let the monitor collect data first
    last_signal_ids = set()
    last_arb_ids = set()

    while True:
        try:
            # Alert new high-score signals
            signals = get_top_signals(limit=5, min_score=50)
            for s in signals:
                sid = s["id"]
                if sid not in last_signal_ids:
                    de = direction_emoji(s.get("direction"))
                    msg = (
                        f"{de} *NEW EDGE SIGNAL* — Score {s['edge_score']:.0f}/100\n"
                        f"*{s['question'][:80]}*\n"
                        f"Type: {signal_type_label(s['signal_type'])}\n"
                        f"Direction: {s.get('direction', '?')} | Confidence: {fmt_pct(s['confidence'])}\n"
                        f"Price: {fmt_price(s['current_price'])} -> Fair: {fmt_price(s['implied_fair_price'])}\n"
                        f"Vol 24h: {fmt_usd(s['volume_24h'])} | Liq: {fmt_usd(s['liquidity'])}\n"
                        f"_{s.get('notes', '')[:100]}_"
                    )
                    await send_alert(app.bot, msg)
                    last_signal_ids.add(sid)

            # Alert new arb opportunities
            arbs = get_top_arbitrage(limit=3)
            for o in arbs:
                oid = o["id"]
                if oid not in last_arb_ids and o["profit_pct"] >= 2.0:
                    msg = (
                        f"*ARB OPPORTUNITY* — {o['profit_pct']:.2f}% profit\n"
                        f"{o['description'][:100]}\n"
                        f"Poly YES: {fmt_price(o['poly_yes_price'])} | Kalshi YES: {fmt_price(o['kalshi_yes_price'])}\n"
                        f"Direction: {o['direction']}"
                    )
                    await send_alert(app.bot, msg)
                    last_arb_ids.add(oid)

        except Exception as exc:
            logger.error("Alert loop error: %s", exc)

        await asyncio.sleep(120)   # check every 2 minutes


# ─── Main ─────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        print("1. Message @BotFather on Telegram -> /newbot -> copy the token")
        print("2. Message @userinfobot on Telegram -> copy your chat ID")
        print("3. Add both to .env and re-run")
        sys.exit(1)

    init_db()
    logger.info("Starting Polymarket Edge Telegram Bot...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("arb",     cmd_arb))
    app.add_handler(CommandHandler("whales",  cmd_whales))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("market",  cmd_market))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown))

    logger.info("Bot running. Message your bot to get started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
