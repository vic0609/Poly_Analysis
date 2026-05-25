"""Kalshi API scraper for cross-platform arbitrage detection."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiohttp

from config import KALSHI_API_BASE

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "polymarket-edge-monitor/1.0",
    "Accept": "application/json",
}


class KalshiScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._token: Optional[str] = None
        self._auth_disabled: bool = False

    async def _auth_headers(self) -> dict:
        """Return headers with Bearer token, logging in if needed."""
        from config import KALSHI_EMAIL, KALSHI_PASSWORD
        if not self._token and not self._auth_disabled and KALSHI_EMAIL and KALSHI_PASSWORD:
            await self._login(KALSHI_EMAIL, KALSHI_PASSWORD)
        h = dict(HEADERS)
        if self._token and self._token != "__no_auth__":
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _login(self, email: str, password: str):
        """Authenticate with Kalshi and store the session token."""
        try:
            async with self.session.post(
                f"{KALSHI_API_BASE}/login",
                json={"email": email, "password": password},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False,
            ) as resp:
                if resp.status == 404:
                    # New API base no longer has a login endpoint — public market data works without auth
                    logger.info("Kalshi login endpoint not found — proceeding without auth (public markets only)")
                    self._auth_disabled = True
                    return
                if resp.status != 200:
                    logger.warning("Kalshi login failed: %s", resp.status)
                    self._auth_disabled = True
                    return
                data = await resp.json()
                self._token = data.get("token")
                if self._token:
                    logger.info("Kalshi authenticated successfully")
                else:
                    logger.warning("Kalshi login returned no token: %s", data)
                    self._auth_disabled = True
        except Exception as exc:
            logger.error("Kalshi login error: %s", exc)
            self._auth_disabled = True

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """
        Fetch Kalshi markets. Returns (markets_list, next_cursor).
        status: "open" | "closed" | "settled"
        """
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        try:
            headers = await self._auth_headers()
            async with self.session.get(
                f"{KALSHI_API_BASE}/markets",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    # Token may have expired — reset and retry once
                    self._token = None
                    self._auth_disabled = False
                    headers = await self._auth_headers()
                    async with self.session.get(
                        f"{KALSHI_API_BASE}/markets",
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp2:
                        if resp2.status != 200:
                            logger.warning("Kalshi /markets returned %s after re-auth", resp2.status)
                            return [], None
                        data = await resp2.json()
                elif resp.status != 200:
                    logger.warning("Kalshi /markets returned %s", resp.status)
                    return [], None
                else:
                    data = await resp.json()
                markets = data.get("markets", [])
                next_cursor = data.get("cursor")
                return markets, next_cursor

        except Exception as exc:
            logger.error("Kalshi markets fetch error: %s", exc)
            return [], None

    async def get_all_open_markets(self, max_markets: int = 1000) -> list[dict]:
        """Fetch open Kalshi binary markets (paginated, capped at max_markets).
        Skips multi-outcome parlay markets that have no binary YES/NO price."""
        all_markets = []
        cursor = None

        while len(all_markets) < max_markets:
            markets, cursor = await self.get_markets(cursor=cursor)
            if not markets:
                break
            # Only keep single-binary markets that have a priceable outcome
            for m in markets:
                # Parlay/multi-game markets have no binary price fields
                if m.get("market_type", "binary") not in ("binary", ""):
                    continue
                parsed = self.parse_market(m)
                if parsed["yes_price"] is not None and parsed["yes_price"] > 0:
                    all_markets.append(m)
                    if len(all_markets) >= max_markets:
                        break
            if not cursor:
                break
            await asyncio.sleep(0.5)   # respect rate limit

        logger.info("Fetched %d open Kalshi binary markets", len(all_markets))
        return all_markets

    async def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single Kalshi market by ticker."""
        try:
            headers = await self._auth_headers()
            async with self.session.get(
                f"{KALSHI_API_BASE}/markets/{ticker}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("market")
        except Exception as exc:
            logger.error("Kalshi /markets/%s error: %s", ticker, exc)
            return None

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[dict]:
        """Fetch order book for a Kalshi market."""
        try:
            headers = await self._auth_headers()
            async with self.session.get(
                f"{KALSHI_API_BASE}/markets/{ticker}/orderbook",
                params={"depth": depth},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as exc:
            logger.error("Kalshi orderbook error for %s: %s", ticker, exc)
            return None

    async def get_trades(self, ticker: str, limit: int = 100) -> list[dict]:
        """Fetch recent trades for a Kalshi market."""
        try:
            headers = await self._auth_headers()
            async with self.session.get(
                f"{KALSHI_API_BASE}/markets/{ticker}/trades",
                params={"limit": limit},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("trades", [])
        except Exception as exc:
            logger.error("Kalshi trades error for %s: %s", ticker, exc)
            return []

    @staticmethod
    def parse_market(raw: dict) -> dict:
        """Normalize Kalshi market dict.

        Kalshi v2 API returns prices in cents (0–99 integer) for binary markets.
        Multi-outcome parlay markets have no yes_bid/no_bid and are skipped upstream.
        """
        def _to_decimal(val) -> Optional[float]:
            if val is None:
                return None
            f = float(val)
            # Prices > 1 are in cents (Kalshi convention); <= 1 already decimal
            return round(f / 100.0 if f > 1 else f, 4)

        # Try all known price field names in priority order (new API uses _dollars suffix)
        yes_raw = (
            raw.get("yes_bid_dollars") or raw.get("yes_bid")
            or raw.get("yes_ask_dollars") or raw.get("yes_ask")
            or raw.get("last_price_dollars") or raw.get("last_price")
            or raw.get("floor_strike")
        )
        no_raw = (
            raw.get("no_bid_dollars") or raw.get("no_bid")
            or raw.get("no_ask_dollars") or raw.get("no_ask")
        )

        yes_price = _to_decimal(yes_raw)
        no_price = _to_decimal(no_raw)

        # Derive missing side from complement
        if yes_price is not None and no_price is None:
            no_price = round(1.0 - yes_price, 4)
        elif no_price is not None and yes_price is None:
            yes_price = round(1.0 - no_price, 4)

        # Sanity check — discard implausible prices
        if yes_price is not None and not (0.01 <= yes_price <= 0.99):
            yes_price = None
            no_price = None

        close_time = None
        for field in ("close_time", "expiration_time", "end_date"):
            if raw.get(field):
                try:
                    close_time = datetime.fromisoformat(
                        str(raw[field]).replace("Z", "+00:00")
                    )
                    break
                except Exception:
                    pass

        return {
            "ticker": raw.get("ticker", ""),
            "title": raw.get("title", raw.get("question", "")),
            "category": raw.get("category", raw.get("event_category", "")),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": float(raw.get("volume_fp") or raw.get("volume", 0) or 0),
            "open_interest": float(raw.get("open_interest_fp") or raw.get("open_interest", 0) or 0),
            "close_time": close_time,
        }
