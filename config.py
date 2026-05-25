"""Central configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Polygon / On-chain ───────────────────────────────────────
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")

# Contract addresses on Polygon mainnet (chain ID 137)
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACE5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ─── Polymarket API ───────────────────────────────────────────
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ─── Kalshi API ───────────────────────────────────────────────
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_EMAIL: str = os.getenv("KALSHI_EMAIL", "")
KALSHI_PASSWORD: str = os.getenv("KALSHI_PASSWORD", "")

# ─── Social Media ─────────────────────────────────────────────
TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
REDDIT_CLIENT_ID: str = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT: str = os.getenv("REDDIT_USER_AGENT", "polymarket_edge_bot/1.0")

# ─── Alerts ───────────────────────────────────────────────────
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Database ─────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/polymarket_edge.db")

# ─── Poll Intervals (seconds) ─────────────────────────────────
MARKET_POLL_INTERVAL: int = int(os.getenv("MARKET_POLL_INTERVAL", "60"))
WHALE_POLL_INTERVAL: int = int(os.getenv("WHALE_POLL_INTERVAL", "30"))
SENTIMENT_POLL_INTERVAL: int = int(os.getenv("SENTIMENT_POLL_INTERVAL", "300"))
ARBITRAGE_POLL_INTERVAL: int = int(os.getenv("ARBITRAGE_POLL_INTERVAL", "30"))
EDGE_DETECTION_INTERVAL: int = int(os.getenv("EDGE_DETECTION_INTERVAL", "120"))

# ─── Edge Detection Thresholds ────────────────────────────────
ZSCORE_THRESHOLD: float = float(os.getenv("ZSCORE_THRESHOLD", "2.0"))
MIN_ARB_PROFIT_PCT: float = float(os.getenv("MIN_ARB_PROFIT_PCT", "1.5"))
WHALE_MIN_SIZE_USDC: float = float(os.getenv("WHALE_MIN_SIZE_USDC", "5000"))

# ─── Execution / Kelly ───────────────────────────────────────
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() != "false"
BANKROLL_USDC: float = float(os.getenv("BANKROLL_USDC", "1000"))
HALF_KELLY: bool = os.getenv("HALF_KELLY", "true").lower() != "false"
MAX_KELLY_FRACTION: float = float(os.getenv("MAX_KELLY_FRACTION", "0.25"))
MAX_POSITION_USDC: float = float(os.getenv("MAX_POSITION_USDC", "500"))
MIN_POSITION_USDC: float = float(os.getenv("MIN_POSITION_USDC", "10"))
MIN_EDGE_SCORE_TO_TRADE: float = float(os.getenv("MIN_EDGE_SCORE_TO_TRADE", "60"))
DISABLED_SIGNAL_TYPES: set = set(s.strip() for s in os.getenv("DISABLED_SIGNAL_TYPES", "").split(",") if s.strip())
MIN_CONFIDENCE_TO_TRADE: float = float(os.getenv("MIN_CONFIDENCE_TO_TRADE", "0.5"))
MIN_PRICE_GAP_TO_TRADE: float = float(os.getenv("MIN_PRICE_GAP_TO_TRADE", "0.05"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.10"))
POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.75"))   # close when 75% of edge captured
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.40"))       # close if down 40% of stake
SETTLEMENT_INTERVAL: int = int(os.getenv("SETTLEMENT_INTERVAL", "60")) # seconds

# ─── Known Top Trader Wallets (seed list, auto-updated from leaderboard) ──
SEED_WHALE_WALLETS = [
    # Populated at runtime from Polymarket leaderboard API
]

# Reddit subreddits to monitor
REDDIT_SUBREDDITS = [
    "Polymarket",
    "PredictionMarkets",
    "politics",
    "worldnews",
    "CryptoCurrency",
    "wallstreetbets",
]

# Twitter search terms
TWITTER_SEARCH_TERMS = [
    "Polymarket",
    "prediction market",
    "#Polymarket",
    "polymarket whale",
]

# Market categories to track (Polymarket tag slugs)
TRACKED_CATEGORIES = [
    "politics",
    "crypto",
    "sports",
    "economics",
    "geopolitics",
    "entertainment",
    "science",
]
