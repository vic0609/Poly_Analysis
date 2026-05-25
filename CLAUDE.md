# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
# One-shot setup (creates venv, installs deps, creates .env, inits DB)
python setup.py

# Activate venv (Windows)
.venv\Scripts\activate

# Initialize DB only
python main.py --init
```

## Running

```bash
# Full 24/7 monitor (all cycles)
python main.py

# One-shot queries (require monitor to have run first)
python main.py --signals    # top edge signals
python main.py --arb        # arbitrage opportunities
python main.py --whales     # recent whale activity
python main.py --sim        # paper trading simulation report

# Parameter optimization
python main.py --sweep              # grid search optimal entry params
python main.py --sweep --apply      # sweep + write best params to .env

# DB maintenance (stop monitor first)
python main.py --prune     # delete rows >7d old
python main.py --vacuum    # reclaim disk space

# Streamlit dashboard
streamlit run dashboard/app.py
```

## Architecture

The system is a 24/7 async Python monitor (`main.py`) that orchestrates six independent cycles, all writing to a single SQLite database (`data/polymarket_edge.db`).

**Data flow:**
```
Scrapers → DB → Monitors → DB → EdgeDetector → DB → Executor → DB
```

### Cycle cadence (configurable in `.env`)
| Cycle | Default interval | Heavy? |
|---|---|---|
| `market_cycle` | 60s | Yes — bulk upserts thousands of rows |
| `kalshi_cycle` | 60s | Yes — runs sequentially after market to avoid WAL lock collisions |
| `whale_cycle` | 30s | Light — runs concurrently |
| `sentiment_cycle` | 300s | Light — runs concurrently |
| `arb_cycle` | 30s | Sync — runs after async cycles for fresh data |
| `edge_cycle` | 120s | Sync — runs edge detection then executes via Kelly sizing |

The main loop ticks every 5s and dispatches cycles by elapsed time. Heavy writers (`market_cycle`, `kalshi_cycle`) are sequential to prevent SQLite WAL lock collisions; light writers gather concurrently.

### Module responsibilities

- **`scrapers/`** — fetch raw data from external APIs
  - `polymarket.py` — Gamma API (market list), CLOB API (orderbook), Data API (leaderboard)
  - `kalshi.py` — Kalshi trading API v2
  - `social.py` — Twitter v2 API, Reddit PRAW

- **`monitors/`** — domain-specific monitoring logic
  - `whale.py` — on-chain Polygon block scanning (web3) + Polymarket API fallback; refreshes whale wallet list from leaderboard every 10 cycles
  - `arbitrage.py` — cross-platform (Poly vs Kalshi) and mechanical arb detection
  - `sentiment.py` — aggregates VADER/TextBlob scores from Twitter + Reddit posts

- **`analysis/`** — signal computation (pure logic, reads DB)
  - `edge_detector.py` — 5 bulk SQL queries to prefetch all sub-signal data, then evaluates each market with a weighted composite score (0–100). Weights: volume anomaly 25%, base rate 25%, price-sentiment 20%, whale 15%, arb 15%.
  - `signals.py` — helper queries for CLI display

- **`execution/`** — trade sizing and execution
  - `kelly.py` — Kelly criterion position sizing
  - `risk_manager.py` — pre-trade checks (max positions, daily loss limit, edge threshold)
  - `executor.py` — paper mode (DB + Telegram alert) or live mode (py-clob-client CLOB order)
  - `settlement.py` — closes positions when markets resolve
  - `param_sweep.py` — grid search over Kelly/edge-score params

- **`db/`** — SQLAlchemy ORM
  - `models.py` — all tables: `markets`, `market_snapshots`, `whale_wallets`, `whale_trades`, `sentiment_records`, `social_posts`, `kalshi_markets`, `arbitrage_opportunities`, `edge_signals`, `positions`, `executed_trades`, `leaderboard`
  - `database.py` — `get_db()` context manager, `init_db()`, `prune_old_data()`, `full_vacuum()`

- **`alerts/notifier.py`** — Discord webhook + Telegram bot alerts, Rich console tables

- **`dashboard/app.py`** — Streamlit UI reading from DB

- **`telegram_bot.py`** — standalone Telegram bot for querying signals interactively

### Configuration

All tunables live in `.env` (loaded by `config.py`). Key flags:

- `PAPER_TRADING=true` — default; set to `false` + add `POLYMARKET_PRIVATE_KEY` for live orders
- `BANKROLL_USDC`, `MAX_KELLY_FRACTION`, `HALF_KELLY` — position sizing
- `MIN_EDGE_SCORE_TO_TRADE` (default 60), `MIN_CONFIDENCE_TO_TRADE` (0.5), `MIN_PRICE_GAP_TO_TRADE` (0.05) — trade entry filters
- `ZSCORE_THRESHOLD` — statistical threshold for volume/price anomaly signals

### SQLite constraints

- Markets with `yes_price < 0.05` or `> 0.95`, `volume_24h < 500`, or `liquidity < 1000` are excluded from edge detection.
- The executor skips markets where `curr_price < 0.12` or `> 0.88` (near-extreme, illiquid).
- `EdgeDetector` uses chunked queries (500 IDs/chunk) to stay under SQLite's 999-variable limit.
- `market_snapshots` is capped at 5000 rows per cycle insertion and pruned daily (>7d old).
- Run `--vacuum` only while the monitor is stopped — WAL mode doesn't release space until VACUUM.
