"""SQLAlchemy ORM models for the Polymarket Edge database."""

from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime,
    Text, ForeignKey, Index, UniqueConstraint, JSON
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ─── Markets ──────────────────────────────────────────────────

class Market(Base):
    """Polymarket market snapshot (updated each poll cycle)."""
    __tablename__ = "markets"

    id = Column(String, primary_key=True)           # Polymarket market condition ID
    slug = Column(String, index=True)
    question = Column(Text)
    category = Column(String, index=True)
    end_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    # Prices (0–1 scale = 0–100%)
    yes_price = Column(Float, nullable=True)
    no_price = Column(Float, nullable=True)
    spread = Column(Float, nullable=True)           # ask_yes - bid_yes

    # Volume / liquidity
    volume_24h = Column(Float, default=0.0)
    volume_total = Column(Float, default=0.0)
    liquidity = Column(Float, default=0.0)
    open_interest = Column(Float, default=0.0)

    # Token IDs for CLOB lookups
    yes_token_id = Column(String, nullable=True)
    no_token_id = Column(String, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    snapshots = relationship("MarketSnapshot", back_populates="market", lazy="dynamic")
    arb_opportunities = relationship("ArbitrageOpportunity", back_populates="polymarket_market", lazy="dynamic")
    edge_signals = relationship("EdgeSignal", back_populates="market", lazy="dynamic")


class MarketSnapshot(Base):
    """Time-series price/volume history per market."""
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, ForeignKey("markets.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    yes_price = Column(Float)
    no_price = Column(Float)
    spread = Column(Float)
    volume_24h = Column(Float)
    liquidity = Column(Float)
    open_interest = Column(Float)

    market = relationship("Market", back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshot_market_time", "market_id", "timestamp"),
    )


# ─── Whale Wallets ────────────────────────────────────────────

class WhaleWallet(Base):
    """Known high-value Polymarket traders."""
    __tablename__ = "whale_wallets"

    address = Column(String, primary_key=True)      # Polygon wallet address
    label = Column(String, nullable=True)           # e.g. "Theo4", "Fredi9999"
    total_profit_usdc = Column(Float, default=0.0)
    win_rate = Column(Float, nullable=True)         # 0–1
    total_trades = Column(Integer, default=0)
    is_bot = Column(Boolean, default=False)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, nullable=True)
    source = Column(String, default="leaderboard")  # leaderboard | manual | detected

    trades = relationship("WhaleTrade", back_populates="wallet", lazy="dynamic")


class WhaleTrade(Base):
    """Individual on-chain trade by a tracked whale wallet."""
    __tablename__ = "whale_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tx_hash = Column(String, unique=True, index=True)
    wallet_address = Column(String, ForeignKey("whale_wallets.address"), index=True)
    market_id = Column(String, ForeignKey("markets.id"), nullable=True, index=True)
    token_id = Column(String, nullable=True)

    side = Column(String)                           # "YES" | "NO"
    action = Column(String)                         # "BUY" | "SELL"
    size_usdc = Column(Float)
    price = Column(Float)                           # price at time of trade (0–1)
    block_number = Column(Integer)
    timestamp = Column(DateTime, index=True)

    wallet = relationship("WhaleWallet", back_populates="trades")

    __table_args__ = (
        Index("ix_whale_trade_market_time", "market_id", "timestamp"),
    )


# ─── Social Sentiment ─────────────────────────────────────────

class SentimentRecord(Base):
    """Aggregated sentiment score per market per time window."""
    __tablename__ = "sentiment_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, ForeignKey("markets.id"), index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String)                         # "twitter" | "reddit" | "news" | "aggregate"

    compound_score = Column(Float)                  # VADER compound: -1 to +1
    positive_ratio = Column(Float)
    negative_ratio = Column(Float)
    neutral_ratio = Column(Float)
    mention_count = Column(Integer, default=0)
    sample_size = Column(Integer, default=0)

    # Raw posts stored as JSON list of {"text": ..., "score": ..., "url": ...}
    raw_posts = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_sentiment_market_time", "market_id", "timestamp"),
    )


class SocialPost(Base):
    """Individual scraped social media post."""
    __tablename__ = "social_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, index=True)             # "twitter" | "reddit"
    post_id = Column(String, unique=True)
    market_id = Column(String, ForeignKey("markets.id"), nullable=True, index=True)
    url = Column(String, nullable=True)
    author = Column(String, nullable=True)
    content = Column(Text)
    posted_at = Column(DateTime, nullable=True)
    scraped_at = Column(DateTime, default=datetime.utcnow)
    compound_score = Column(Float, nullable=True)
    engagement = Column(Integer, default=0)         # likes + retweets / upvotes


# ─── Arbitrage ────────────────────────────────────────────────

class KalshiMarket(Base):
    """Kalshi market snapshot for cross-platform comparison."""
    __tablename__ = "kalshi_markets"

    ticker = Column(String, primary_key=True)
    title = Column(Text)
    category = Column(String, nullable=True)
    yes_price = Column(Float, nullable=True)
    no_price = Column(Float, nullable=True)
    volume = Column(Float, default=0.0)
    open_interest = Column(Float, default=0.0)
    close_time = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArbitrageOpportunity(Base):
    """Detected price discrepancy between Polymarket and Kalshi."""
    __tablename__ = "arbitrage_opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    polymarket_market_id = Column(String, ForeignKey("markets.id"), index=True)
    kalshi_ticker = Column(String, ForeignKey("kalshi_markets.ticker"), index=True)
    description = Column(Text)                      # human readable event

    # Prices at detection time
    poly_yes_price = Column(Float)
    kalshi_yes_price = Column(Float)
    price_gap = Column(Float)                       # abs difference
    profit_pct = Column(Float)                      # estimated profit %

    # Strategy
    arb_type = Column(String)                       # "cross_platform" | "mechanical" | "combinatorial"
    direction = Column(String)                      # "buy_poly_yes_sell_kalshi_yes" etc.
    estimated_max_size_usdc = Column(Float, nullable=True)

    polymarket_market = relationship("Market", back_populates="arb_opportunities")
    kalshi_market = relationship("KalshiMarket")


# ─── Edge Signals ─────────────────────────────────────────────

class EdgeSignal(Base):
    """Computed outlier / edge opportunity combining all data sources."""
    __tablename__ = "edge_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, ForeignKey("markets.id"), index=True)
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    # Signal type
    signal_type = Column(String, index=True)
    # Types:
    #   "price_sentiment_divergence"  - price disagrees with sentiment
    #   "whale_accumulation"          - whale buying heavily one side
    #   "volume_spike"                - abnormal volume relative to baseline
    #   "arbitrage"                   - cross-platform price gap
    #   "base_rate_mismatch"          - price deviates from historical base rate
    #   "thin_market_mispricing"      - low-volume market with clear edge

    # Scores
    edge_score = Column(Float)                      # 0–100 composite edge score
    confidence = Column(Float)                      # 0–1 model confidence
    zscore = Column(Float, nullable=True)           # statistical z-score
    direction = Column(String, nullable=True)       # "YES" | "NO" | "NEUTRAL"

    # Supporting data
    current_price = Column(Float)
    implied_fair_price = Column(Float, nullable=True)
    sentiment_score = Column(Float, nullable=True)
    whale_signal = Column(Float, nullable=True)     # net whale buy pressure -1 to +1
    arb_profit_pct = Column(Float, nullable=True)

    notes = Column(Text, nullable=True)
    raw_data = Column(JSON, nullable=True)

    market = relationship("Market", back_populates="edge_signals")

    __table_args__ = (
        Index("ix_edge_market_time", "market_id", "detected_at"),
        Index("ix_edge_type_active", "signal_type", "is_active"),
    )


# ─── Execution / Portfolio ───────────────────────────────────

class Position(Base):
    """Open or closed position taken by the execution layer."""
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, ForeignKey("markets.id"), index=True)
    token_id = Column(String, nullable=True)
    side = Column(String)                           # "YES" | "NO"
    status = Column(String, default="open", index=True)  # "open" | "closed" | "cancelled"

    # Entry
    entry_price = Column(Float)
    size_usdc = Column(Float)                       # dollars deployed
    shares = Column(Float)                          # shares purchased
    kelly_fraction = Column(Float)
    edge_score = Column(Float)

    # Exit (filled when closed)
    exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)     # "resolved" | "manual" | "stop"

    # Metadata
    clob_order_id = Column(String, nullable=True)
    is_paper = Column(Boolean, default=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_position_market_status", "market_id", "status"),
    )


class ExecutedTrade(Base):
    """Immutable log of every order sent (paper or live)."""
    __tablename__ = "executed_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    executed_at = Column(DateTime, default=datetime.utcnow, index=True)

    market_id = Column(String, ForeignKey("markets.id"), index=True)
    token_id = Column(String, nullable=True)
    side = Column(String)                           # "YES" | "NO"
    action = Column(String)                         # "BUY" | "SELL"
    price = Column(Float)
    size_usdc = Column(Float)
    shares = Column(Float)
    kelly_fraction = Column(Float)
    edge_score = Column(Float)
    signal_type = Column(String, nullable=True)

    # Fill data (populated on close)
    fill_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)

    clob_order_id = Column(String, nullable=True)
    is_paper = Column(Boolean, default=True)


# ─── Leaderboard Cache ────────────────────────────────────────

class LeaderboardEntry(Base):
    """Polymarket leaderboard snapshot."""
    __tablename__ = "leaderboard"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_at = Column(DateTime, default=datetime.utcnow, index=True)
    rank = Column(Integer)
    wallet_address = Column(String, index=True)
    username = Column(String, nullable=True)
    profit_usdc = Column(Float)
    win_rate = Column(Float, nullable=True)
    num_trades = Column(Integer, nullable=True)
    volume_usdc = Column(Float, nullable=True)
