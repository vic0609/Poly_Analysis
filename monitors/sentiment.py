"""Sentiment monitor — aggregates Twitter + Reddit per active market."""

import logging
from datetime import datetime

from db.database import get_db
from db.models import Market, SentimentRecord, SocialPost
from scrapers.social import TwitterScraper, RedditScraper, NewsScraper, aggregate_sentiment

logger = logging.getLogger(__name__)

# Number of top markets (by volume) to run sentiment on each cycle
MAX_MARKETS_PER_CYCLE = 30


class SentimentMonitor:
    def __init__(self, aiohttp_session=None):
        self.twitter = TwitterScraper(aiohttp_session) if aiohttp_session else None
        self.reddit = RedditScraper()
        self.news = NewsScraper(aiohttp_session)
        self.session = aiohttp_session

    async def run_cycle(self):
        """Run one sentiment collection cycle across top active markets."""
        markets = self._get_top_markets()
        if not markets:
            logger.debug("No active markets found for sentiment analysis")
            return

        logger.info("Running sentiment cycle for %d markets", len(markets))

        for market in markets:
            posts = []
            market_id = market["id"]
            question = market["question"]

            # Twitter
            if self.twitter and self.twitter.enabled:
                try:
                    tw_posts = await self.twitter.search_recent(
                        "Polymarket", market_keyword=question[:60]
                    )
                    posts.extend(tw_posts)
                except Exception as exc:
                    logger.error("Twitter sentiment error for %s: %s", market_id, exc)

            # Reddit (sync — praw is sync)
            if self.reddit.enabled:
                try:
                    rd_posts = self.reddit.search_market(
                        question[:60], limit=30
                    )
                    posts.extend(rd_posts)
                    # Also pull r/Polymarket new posts
                    rd_new = self.reddit.get_subreddit_new("Polymarket", limit=25)
                    posts.extend(rd_new)
                except Exception as exc:
                    logger.error("Reddit sentiment error for %s: %s", market_id, exc)

            # Google News RSS — always runs, no API key needed
            if self.news.enabled:
                try:
                    news_posts = await self.news.search_market(
                        question, limit=20
                    )
                    posts.extend(news_posts)
                except Exception as exc:
                    logger.debug("News sentiment error for %s: %s", market_id, exc)

            if not posts:
                continue

            # Deduplicate by post_id
            seen = set()
            unique_posts = []
            for p in posts:
                if p["post_id"] not in seen:
                    seen.add(p["post_id"])
                    unique_posts.append(p)

            agg = aggregate_sentiment(unique_posts)
            self._persist_sentiment(market_id, unique_posts, agg)

    def _get_top_markets(self) -> list:
        with get_db() as db:
            rows = (
                db.query(Market.id, Market.question)
                .filter(Market.is_active == True)
                .order_by(Market.volume_24h.desc())
                .limit(MAX_MARKETS_PER_CYCLE)
                .all()
            )
            return [{"id": r.id, "question": r.question} for r in rows]

    def _persist_sentiment(
        self, market_id: str, posts: list[dict], agg: dict
    ):
        """Save individual posts and aggregate sentiment record to DB."""
        with get_db() as db:
            # Save individual posts (skip duplicates)
            for p in posts:
                existing = db.query(SocialPost).filter_by(post_id=p["post_id"]).first()
                if not existing:
                    sp = SocialPost(
                        source=p.get("source", "unknown"),
                        post_id=p["post_id"],
                        market_id=market_id,
                        url=p.get("url"),
                        author=p.get("author"),
                        content=p.get("content", "")[:2000],
                        posted_at=p.get("posted_at"),
                        compound_score=p.get("compound_score"),
                        engagement=p.get("engagement", 0),
                    )
                    db.add(sp)

            # Save aggregate record
            record = SentimentRecord(
                market_id=market_id,
                source="aggregate",
                compound_score=agg["compound_score"],
                positive_ratio=agg["positive_ratio"],
                negative_ratio=agg["negative_ratio"],
                neutral_ratio=agg["neutral_ratio"],
                mention_count=agg["mention_count"],
                sample_size=agg["sample_size"],
            )
            db.add(record)

        logger.debug(
            "Sentiment for market %s: compound=%.3f, n=%d",
            market_id,
            agg["compound_score"],
            agg["sample_size"],
        )

    def get_latest_sentiment(self, market_id: str) -> dict:
        """Retrieve the most recent sentiment aggregate for a market."""
        with get_db() as db:
            record = (
                db.query(SentimentRecord)
                .filter(
                    SentimentRecord.market_id == market_id,
                    SentimentRecord.source == "aggregate",
                )
                .order_by(SentimentRecord.timestamp.desc())
                .first()
            )

        if not record:
            return {}

        return {
            "compound_score": record.compound_score,
            "positive_ratio": record.positive_ratio,
            "negative_ratio": record.negative_ratio,
            "neutral_ratio": record.neutral_ratio,
            "mention_count": record.mention_count,
            "timestamp": record.timestamp,
        }
