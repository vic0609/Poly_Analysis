"""Database engine setup and session management."""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

logger = logging.getLogger(__name__)

from config import DATABASE_URL
from db.models import Base

# Ensure the data directory exists for SQLite
if DATABASE_URL.startswith("sqlite"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
)

# Enable WAL mode for SQLite (allows concurrent reads during writes)
if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=10000")
        cursor.execute("PRAGMA busy_timeout=60000")   # wait up to 60s for write lock
        cursor.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint WAL every 1000 pages
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


def prune_old_data(
    snapshots_days: int = 7,
    signals_days: int = 7,
    kalshi_days: int = 3,
    vacuum: bool = False,
) -> dict:
    """
    Delete stale rows from high-volume tables to keep the DB lean.

    Returns a dict with deleted row counts per table.
    Runs VACUUM afterward only if vacuum=True (slow on large DBs — run weekly).
    """
    now = datetime.utcnow()
    cuts = {
        "market_snapshots": now - timedelta(days=snapshots_days),
        "edge_signals":     now - timedelta(days=signals_days),
        "kalshi_markets":   now - timedelta(days=kalshi_days),
    }
    deleted = {}

    with get_db() as db:
        # market_snapshots — timestamped
        r = db.execute(
            text("DELETE FROM market_snapshots WHERE timestamp < :cut"),
            {"cut": cuts["market_snapshots"]},
        )
        deleted["market_snapshots"] = r.rowcount

        # edge_signals — detected_at column
        r = db.execute(
            text("DELETE FROM edge_signals WHERE detected_at < :cut"),
            {"cut": cuts["edge_signals"]},
        )
        deleted["edge_signals"] = r.rowcount

        # kalshi_markets — updated_at column (refreshed every cycle anyway)
        r = db.execute(
            text("DELETE FROM kalshi_markets WHERE updated_at < :cut"),
            {"cut": cuts["kalshi_markets"]},
        )
        deleted["kalshi_markets"] = r.rowcount

    total = sum(deleted.values())
    logger.info(
        "DB prune: removed %d rows (snapshots=%d, signals=%d, kalshi=%d)",
        total,
        deleted["market_snapshots"],
        deleted["edge_signals"],
        deleted["kalshi_markets"],
    )

    if vacuum:
        # incremental_vacuum reclaims free pages without needing an exclusive lock,
        # making it safe to run while the monitor is active.
        # Full VACUUM (offline) is available via `python main.py --vacuum`.
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA incremental_vacuum(2000)"))
            logger.info("DB incremental_vacuum complete")
        except Exception as exc:
            logger.warning("incremental_vacuum skipped: %s", exc)

    return deleted


def full_vacuum():
    """Run a full VACUUM to reclaim disk space. Requires no other connections."""
    import sqlite3
    if "sqlite" not in DATABASE_URL:
        logger.info("VACUUM only supported for SQLite")
        return
    db_path = DATABASE_URL.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.close()
    logger.info("Full VACUUM complete")


@contextmanager
def get_db() -> Session:
    """Context manager for DB sessions with automatic rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
