"""Polymarket API scraper — Gamma, CLOB, and Data APIs."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiohttp

from config import (
    POLYMARKET_GAMMA_API,
    POLYMARKET_CLOB_API,
    POLYMARKET_DATA_API,
    WHALE_MIN_SIZE_USDC,
)

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "polymarket-edge-monitor/1.0", "Accept": "application/json"}


class PolymarketScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    # ─── Gamma API ────────────────────────────────────────────

    async def get_all_markets(
        self,
        active_only: bool = True,
        limit: int = 100,
        order: str = "volume24hr",
    ) -> list[dict]:
        """Fetch all markets, auto-paginated. Returns list of market dicts."""
        markets = []
        offset = 0

        while True:
            params = {
                "limit": limit,
                "offset": offset,
                "order": order,
                "ascending": "false",
            }
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"

            try:
                async with self.session.get(
                    f"{POLYMARKET_GAMMA_API}/markets",
                    params=params,
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error("Gamma API /markets error: %s", resp.status)
                        break
                    data = await resp.json()

            except Exception as exc:
                logger.error("Gamma API fetch error: %s", exc)
                break

            if not data:
                break

            markets.extend(data)
            if len(data) < limit:
                break
            offset += limit
            await asyncio.sleep(0.2)   # be polite

        logger.info("Fetched %d markets from Polymarket", len(markets))
        return markets

    async def get_events(self, active_only: bool = True, limit: int = 100) -> list[dict]:
        """Fetch events (grouped market sets)."""
        params = {"limit": limit, "order": "volume24hr", "ascending": "false"}
        if active_only:
            params["active"] = "true"

        try:
            async with self.session.get(
                f"{POLYMARKET_GAMMA_API}/events",
                params=params,
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                return await resp.json()
        except Exception as exc:
            logger.error("Gamma API /events error: %s", exc)
            return []

    async def get_market(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by ID."""
        try:
            async with self.session.get(
                f"{POLYMARKET_GAMMA_API}/markets/{market_id}",
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as exc:
            logger.error("Gamma API /markets/%s error: %s", market_id, exc)
            return None

    # ─── CLOB API ─────────────────────────────────────────────

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch live order book for a token (YES or NO outcome token)."""
        try:
            async with self.session.get(
                f"{POLYMARKET_CLOB_API}/book",
                params={"token_id": token_id},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as exc:
            logger.error("CLOB orderbook error for %s: %s", token_id, exc)
            return None

    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get best price for a token. side = 'BUY' | 'SELL'."""
        try:
            async with self.session.get(
                f"{POLYMARKET_CLOB_API}/price",
                params={"token_id": token_id, "side": side},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data.get("price", 0))
        except Exception as exc:
            logger.error("CLOB price error for %s: %s", token_id, exc)
            return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        try:
            async with self.session.get(
                f"{POLYMARKET_CLOB_API}/midpoint",
                params={"token_id": token_id},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data.get("mid", 0))
        except Exception as exc:
            logger.error("CLOB midpoint error for %s: %s", token_id, exc)
            return None

    async def get_price_history(
        self, token_id: str, interval: str = "1h", fidelity: int = 60
    ) -> list[dict]:
        """Get historical price data for a token.

        interval: "1m" | "1h" | "1d" | "1w" | "1mo" | "all"
        fidelity: number of data points
        """
        try:
            async with self.session.get(
                f"{POLYMARKET_CLOB_API}/prices-history",
                params={"token_id": token_id, "interval": interval, "fidelity": fidelity},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("history", [])
        except Exception as exc:
            logger.error("CLOB price history error for %s: %s", token_id, exc)
            return []

    async def get_recent_trades(self, token_id: str, limit: int = 100) -> list[dict]:
        """Get recent trades for a specific token."""
        try:
            async with self.session.get(
                f"{POLYMARKET_CLOB_API}/trades",
                params={"token_id": token_id, "limit": limit},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("CLOB trades error for %s: %s", token_id, exc)
            return []

    # ─── Data API ─────────────────────────────────────────────

    async def get_wallet_positions(self, address: str) -> list[dict]:
        """Get current open positions for a wallet address."""
        try:
            async with self.session.get(
                f"{POLYMARKET_DATA_API}/positions",
                params={"user": address},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Data API positions error for %s: %s", address, exc)
            return []

    async def get_global_trades(self, limit: int = 200) -> list[dict]:
        """Fetch recent global trades across all wallets via /trades endpoint."""
        try:
            async with self.session.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"limit": limit},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Global trades fetch error: %s", exc)
            return []

    async def get_wallet_trades(
        self, address: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Get trade history for a wallet address."""
        try:
            async with self.session.get(
                f"{POLYMARKET_DATA_API}/activity",
                params={"user": address, "limit": limit, "offset": offset},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Data API activity error for %s: %s", address, exc)
            return []

    async def get_leaderboard(self, limit: int = 100) -> list[dict]:
        """Fetch the Polymarket profit leaderboard."""
        try:
            async with self.session.get(
                f"{POLYMARKET_DATA_API}/leaderboard",
                params={"limit": limit},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    # Fall back to gamma leaderboard endpoint
                    return await self._get_leaderboard_gamma(limit)
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Leaderboard fetch error: %s", exc)
            return await self._get_leaderboard_gamma(limit)

    async def _get_leaderboard_gamma(self, limit: int = 100) -> list[dict]:
        """Fallback: fetch leaderboard from Gamma API."""
        try:
            async with self.session.get(
                f"{POLYMARKET_GAMMA_API}/leaderboard",
                params={"limit": limit},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Gamma leaderboard fallback error: %s", exc)
            return []

    # ─── Market Parsing Helpers ───────────────────────────────

    @staticmethod
    def parse_market(raw: dict) -> dict:
        """Normalize raw Gamma API market dict into a clean schema.

        Real API response shape (verified 2026-04):
          id            : "540816"            (numeric string, internal ID)
          conditionId   : "0x9c1a..."         (EVM condition ID — use as primary key)
          outcomePrices : "[\"0.545\",\"0.455\"]"  (JSON-encoded string!)
          tokens        : may be absent
          volume24hr    : float
          liquidity     : string float
        """
        import json as _json

        # ── Primary key: prefer conditionId (stable EVM hash), fall back to id ──
        market_id = raw.get("conditionId") or raw.get("id", "")

        # ── Prices: outcomePrices is a JSON-encoded string ──────────────────────
        yes_price = None
        no_price = None
        outcome_prices_raw = raw.get("outcomePrices")
        if outcome_prices_raw:
            try:
                if isinstance(outcome_prices_raw, str):
                    prices = _json.loads(outcome_prices_raw)
                else:
                    prices = list(outcome_prices_raw)
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
                elif len(prices) == 1:
                    yes_price = float(prices[0])
                    no_price = 1.0 - yes_price
            except Exception:
                pass

        # ── Token IDs (optional, used for CLOB lookups) ─────────────────────────
        tokens = raw.get("tokens") or []
        yes_token = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), {})
        no_token  = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), {})
        # Fall back to clobTokenIds if present
        clob_ids = raw.get("clobTokenIds") or []
        yes_token_id = (
            yes_token.get("token_id") or yes_token.get("tokenId")
            or (clob_ids[0] if len(clob_ids) > 0 else None)
        )
        no_token_id = (
            no_token.get("token_id") or no_token.get("tokenId")
            or (clob_ids[1] if len(clob_ids) > 1 else None)
        )

        # ── Spread (if both prices known) ────────────────────────────────────────
        spread = None
        if yes_price is not None and no_price is not None:
            spread = round(abs(1.0 - yes_price - no_price), 4)

        # ── End date ─────────────────────────────────────────────────────────────
        end_date = None
        for field in ("endDate", "endDateIso", "end_date"):
            if raw.get(field):
                try:
                    raw_date = str(raw[field])
                    if "T" not in raw_date:
                        raw_date += "T00:00:00Z"
                    end_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    break
                except Exception:
                    pass

        # ── Category: Polymarket sometimes puts it in tags ───────────────────────
        category = raw.get("category") or ""
        if not category:
            tags = raw.get("tags") or []
            if tags and isinstance(tags[0], dict):
                category = tags[0].get("label", "")
            elif tags and isinstance(tags[0], str):
                category = tags[0]

        return {
            "id": market_id,
            "slug": raw.get("slug", ""),
            "question": raw.get("question", ""),
            "category": category,
            "end_date": end_date,
            "is_active": bool(raw.get("active", True)) and not bool(raw.get("closed", False)),
            "yes_price": yes_price,
            "no_price": no_price,
            "spread": spread,
            "volume_24h": float(raw.get("volume24hr") or raw.get("volume24h") or 0),
            "volume_total": float(raw.get("volume") or raw.get("volumeNum") or 0),
            "liquidity": float(raw.get("liquidity") or raw.get("liquidityNum") or 0),
            "open_interest": float(raw.get("openInterest") or 0),
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
        }
