"""Social media scrapers — Twitter/X, Reddit, and Google News RSS (no-auth fallback)."""

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from config import (
    TWITTER_BEARER_TOKEN,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
    REDDIT_SUBREDDITS,
    TWITTER_SEARCH_TERMS,
)

logger = logging.getLogger(__name__)
vader = SentimentIntensityAnalyzer()


def score_text(text: str) -> dict:
    """Run VADER sentiment on text. Returns compound/pos/neg/neu scores."""
    scores = vader.polarity_scores(text)
    return {
        "compound": scores["compound"],
        "positive": scores["pos"],
        "negative": scores["neg"],
        "neutral": scores["neu"],
    }


def _clean_text(text: str) -> str:
    """Strip URLs and special chars for sentiment analysis."""
    text = re.sub(r"http\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#(\w+)", r"\1", text)
    return text.strip()


# ─── Twitter / X ──────────────────────────────────────────────

class TwitterScraper:
    """Uses Twitter API v2 Bearer Token for read-only search."""

    API_BASE = "https://api.twitter.com/2"
    MAX_RESULTS = 100

    def __init__(self, session):
        self.session = session
        self.enabled = False  # Twitter Search API v2 requires paid plan (402); disabled
        if not self.enabled:
            logger.warning("Twitter scraper disabled — TWITTER_BEARER_TOKEN not set")

    async def search_recent(
        self,
        query: str,
        max_results: int = 100,
        market_keyword: Optional[str] = None,
    ) -> list[dict]:
        """Search recent tweets (last 7 days). Returns list of post dicts."""
        if not self.enabled:
            return []

        # Add Polymarket context to query if not present
        if market_keyword and market_keyword.lower() not in query.lower():
            query = f"({query}) OR (polymarket {market_keyword})"

        # Twitter API v2 search
        params = {
            "query": f"{query} lang:en -is:retweet",
            "max_results": min(max_results, self.MAX_RESULTS),
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
        }
        headers = {
            "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
            "User-Agent": "polymarket-edge-monitor/1.0",
        }

        try:
            async with self.session.get(
                f"{self.API_BASE}/tweets/search/recent",
                params=params,
                headers=headers,
                timeout=__import__("aiohttp").ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    logger.warning("Twitter rate limit hit")
                    return []
                if resp.status != 200:
                    logger.warning("Twitter API returned %s", resp.status)
                    return []
                data = await resp.json()

        except Exception as exc:
            logger.error("Twitter search error: %s", exc)
            return []

        posts = []
        for tweet in data.get("data", []):
            text = tweet.get("text", "")
            clean = _clean_text(text)
            sentiment = score_text(clean)
            metrics = tweet.get("public_metrics", {})
            engagement = (
                metrics.get("like_count", 0)
                + metrics.get("retweet_count", 0) * 2
                + metrics.get("reply_count", 0)
            )

            created_at = None
            if tweet.get("created_at"):
                try:
                    created_at = datetime.fromisoformat(
                        tweet["created_at"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            posts.append({
                "source": "twitter",
                "post_id": tweet.get("id", ""),
                "content": text,
                "posted_at": created_at,
                "compound_score": sentiment["compound"],
                "positive": sentiment["positive"],
                "negative": sentiment["negative"],
                "neutral": sentiment["neutral"],
                "engagement": engagement,
                "url": f"https://twitter.com/i/web/status/{tweet.get('id', '')}",
            })

        return posts

    async def search_all_terms(self, market_question: Optional[str] = None) -> list[dict]:
        """Search all configured Twitter terms. Optionally filter by market question."""
        all_posts = []
        for term in TWITTER_SEARCH_TERMS:
            posts = await self.search_recent(term, market_keyword=market_question)
            all_posts.extend(posts)
        return all_posts


# ─── Reddit ───────────────────────────────────────────────────

class RedditScraper:
    """Scrapes Reddit via PRAW for market-relevant sentiment."""

    def __init__(self):
        self.enabled = bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)
        self._reddit = None

        if self.enabled:
            try:
                import praw
                self._reddit = praw.Reddit(
                    client_id=REDDIT_CLIENT_ID,
                    client_secret=REDDIT_CLIENT_SECRET,
                    user_agent=REDDIT_USER_AGENT,
                )
                logger.info("Reddit scraper initialized")
            except ImportError:
                logger.warning("praw not installed — Reddit scraper disabled")
                self.enabled = False
            except Exception as exc:
                logger.warning("Reddit init error: %s", exc)
                self.enabled = False
        else:
            logger.warning("Reddit scraper disabled — credentials not set")

    def search_market(
        self,
        query: str,
        subreddits: Optional[list[str]] = None,
        limit: int = 50,
        time_filter: str = "week",
    ) -> list[dict]:
        """Search Reddit for posts related to a market question.

        time_filter: "hour" | "day" | "week" | "month" | "year" | "all"
        """
        if not self.enabled or not self._reddit:
            return []

        subreddits = subreddits or REDDIT_SUBREDDITS
        subreddit_str = "+".join(subreddits)
        posts = []

        try:
            subreddit = self._reddit.subreddit(subreddit_str)
            results = subreddit.search(
                query,
                sort="relevance",
                time_filter=time_filter,
                limit=limit,
            )

            for submission in results:
                # Score title + selftext together
                combined = f"{submission.title}. {submission.selftext[:500]}"
                clean = _clean_text(combined)
                sentiment = score_text(clean)

                posts.append({
                    "source": "reddit",
                    "post_id": submission.id,
                    "content": f"{submission.title}: {submission.selftext[:300]}",
                    "url": f"https://reddit.com{submission.permalink}",
                    "posted_at": datetime.fromtimestamp(
                        submission.created_utc, tz=timezone.utc
                    ),
                    "compound_score": sentiment["compound"],
                    "positive": sentiment["positive"],
                    "negative": sentiment["negative"],
                    "neutral": sentiment["neutral"],
                    "engagement": submission.score + submission.num_comments * 2,
                    "subreddit": submission.subreddit.display_name,
                })

        except Exception as exc:
            logger.error("Reddit search error for '%s': %s", query, exc)

        return posts

    def get_subreddit_new(
        self,
        subreddit: str = "Polymarket",
        limit: int = 50,
    ) -> list[dict]:
        """Pull newest posts from a specific subreddit."""
        if not self.enabled or not self._reddit:
            return []

        posts = []
        try:
            sub = self._reddit.subreddit(subreddit)
            for submission in sub.new(limit=limit):
                clean = _clean_text(f"{submission.title} {submission.selftext[:500]}")
                sentiment = score_text(clean)

                posts.append({
                    "source": "reddit",
                    "post_id": submission.id,
                    "content": f"{submission.title}: {submission.selftext[:300]}",
                    "url": f"https://reddit.com{submission.permalink}",
                    "posted_at": datetime.fromtimestamp(
                        submission.created_utc, tz=timezone.utc
                    ),
                    "compound_score": sentiment["compound"],
                    "positive": sentiment["positive"],
                    "negative": sentiment["negative"],
                    "neutral": sentiment["neutral"],
                    "engagement": submission.score + submission.num_comments * 2,
                    "subreddit": subreddit,
                })

        except Exception as exc:
            logger.error("Reddit r/%s new posts error: %s", subreddit, exc)

        return posts


# ─── Google News RSS (no API key required) ────────────────────

class NewsScraper:
    """Fetches Google News RSS headlines for market-relevant queries.

    Requires no API keys. Rate-limited naturally by RSS caching (~15 min).
    Returns posts in the same format as Twitter/Reddit scrapers so they
    can be fed directly into aggregate_sentiment().
    """

    RSS_URL = "https://news.google.com/rss/search"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; polymarket-edge-monitor/1.0)"
        ),
    }

    def __init__(self, session):
        self.session = session
        self.enabled = session is not None

    async def search(self, query: str, max_results: int = 30) -> list[dict]:
        """Search Google News RSS for headlines matching query."""
        if not self.enabled:
            return []

        params = urllib.parse.urlencode({
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        })
        url = f"{self.RSS_URL}?{params}"

        try:
            import aiohttp as _aiohttp
            async with self.session.get(
                url,
                headers=self.HEADERS,
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.debug("Google News RSS returned %s for query '%s'", resp.status, query[:40])
                    return []
                text = await resp.text()
        except Exception as exc:
            logger.debug("Google News RSS fetch error: %s", exc)
            return []

        return self._parse_rss(text, max_results)

    def _parse_rss(self, xml_text: str, max_results: int) -> list[dict]:
        posts = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.debug("RSS parse error: %s", exc)
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        for item in channel.findall("item")[:max_results]:
            title = (item.findtext("title") or "").strip()
            description = (item.findtext("description") or "").strip()
            # Strip HTML tags from description
            description = re.sub(r"<[^>]+>", " ", description).strip()
            pub_date_str = item.findtext("pubDate") or ""
            link = item.findtext("link") or ""

            combined = f"{title}. {description}"
            clean = _clean_text(combined)
            if not clean:
                continue

            sentiment = score_text(clean)

            pub_date = None
            if pub_date_str:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_date = parsedate_to_datetime(pub_date_str)
                except Exception:
                    pass

            # Use link as post_id (unique per article)
            post_id = f"news_{abs(hash(link or title))}"

            posts.append({
                "source": "news",
                "post_id": post_id,
                "content": combined[:500],
                "url": link,
                "author": None,
                "posted_at": pub_date,
                "compound_score": sentiment["compound"],
                "positive": sentiment["positive"],
                "negative": sentiment["negative"],
                "neutral": sentiment["neutral"],
                "engagement": 1,   # no engagement signal from RSS
            })

        return posts

    async def search_market(self, market_question: str, limit: int = 20) -> list[dict]:
        """Search news for a specific market question (truncated for RSS)."""
        # Use first ~60 chars of the question as the search query
        query = market_question[:80]
        return await self.search(query, max_results=limit)


# ─── Aggregate Sentiment ──────────────────────────────────────

def aggregate_sentiment(posts: list[dict]) -> dict:
    """Compute weighted aggregate sentiment from a list of posts.

    Weights engagement so viral posts count more.
    """
    if not posts:
        return {
            "compound_score": 0.0,
            "positive_ratio": 0.0,
            "negative_ratio": 0.0,
            "neutral_ratio": 0.0,
            "mention_count": 0,
            "sample_size": 0,
        }

    total_weight = 0.0
    weighted_compound = 0.0
    positive_count = 0
    negative_count = 0
    neutral_count = 0

    for post in posts:
        weight = max(1, post.get("engagement", 1))
        compound = post.get("compound_score", 0.0)
        weighted_compound += compound * weight
        total_weight += weight

        if compound >= 0.05:
            positive_count += 1
        elif compound <= -0.05:
            negative_count += 1
        else:
            neutral_count += 1

    n = len(posts)
    return {
        "compound_score": weighted_compound / total_weight if total_weight > 0 else 0.0,
        "positive_ratio": positive_count / n,
        "negative_ratio": negative_count / n,
        "neutral_ratio": neutral_count / n,
        "mention_count": n,
        "sample_size": n,
    }
