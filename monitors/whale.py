"""Whale wallet monitor — on-chain via Polygon + Polymarket Data API."""

import logging
from datetime import datetime, timezone
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from config import (
    POLYGON_RPC_URL,
    CTF_EXCHANGE_ADDRESS,
    NEG_RISK_EXCHANGE_ADDRESS,
    WHALE_MIN_SIZE_USDC,
)
from db.database import get_db
from db.models import WhaleWallet, WhaleTrade, Market

logger = logging.getLogger(__name__)

# ABI fragment — we only need the OrderFilled event
CTF_EXCHANGE_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "orderHash", "type": "bytes32"},
            {"indexed": True, "name": "maker", "type": "address"},
            {"indexed": False, "name": "taker", "type": "address"},
            {"indexed": False, "name": "makerAssetId", "type": "uint256"},
            {"indexed": False, "name": "takerAssetId", "type": "uint256"},
            {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "fee", "type": "uint256"},
        ],
        "name": "OrderFilled",
        "type": "event",
    }
]


class WhaleMonitor:
    def __init__(self, poly_scraper=None):
        self.poly_scraper = poly_scraper
        self._w3: Optional[Web3] = None
        self._ctf_contract = None
        self._neg_risk_contract = None
        self._last_block: int = 0
        self._init_web3()

    def _init_web3(self):
        """Initialize Web3 connection to Polygon."""
        try:
            self._w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL, request_kwargs={"timeout": 30}))
            if self._w3.is_connected():
                self._ctf_contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(CTF_EXCHANGE_ADDRESS),
                    abi=CTF_EXCHANGE_ABI,
                )
                self._neg_risk_contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(NEG_RISK_EXCHANGE_ADDRESS),
                    abi=CTF_EXCHANGE_ABI,
                )
                self._last_block = self._w3.eth.block_number - 200  # start recent
                logger.info(
                    "Web3 connected to Polygon. Current block: %d",
                    self._w3.eth.block_number,
                )
            else:
                logger.warning("Web3 connection failed — on-chain monitor disabled")
                self._w3 = None
        except Exception as exc:
            logger.warning("Web3 init error: %s — on-chain disabled", exc)
            self._w3 = None

    # ─── Leaderboard Seeding ──────────────────────────────────

    async def refresh_whale_list_from_leaderboard(self):
        """Pull top 100 traders from Polymarket leaderboard and upsert to DB."""
        if not self.poly_scraper:
            return

        entries = await self.poly_scraper.get_leaderboard(limit=100)
        if not entries:
            logger.warning("Leaderboard empty or unavailable")
            return

        with get_db() as db:
            for i, entry in enumerate(entries):
                address = (
                    entry.get("proxyWallet")
                    or entry.get("address")
                    or entry.get("wallet")
                    or ""
                ).lower()
                if not address or len(address) < 10:
                    continue

                wallet = db.query(WhaleWallet).filter_by(address=address).first()
                if not wallet:
                    wallet = WhaleWallet(address=address, source="leaderboard")
                    db.add(wallet)

                wallet.label = (
                    entry.get("name")
                    or entry.get("username")
                    or entry.get("pseudonym")
                    or wallet.label
                )
                wallet.total_profit_usdc = float(
                    entry.get("profit") or entry.get("pnl") or 0
                )
                wallet.win_rate = (
                    float(entry.get("winRate") or entry.get("win_rate") or 0)
                    if entry.get("winRate") or entry.get("win_rate")
                    else None
                )
                wallet.total_trades = int(entry.get("numTrades") or entry.get("trades") or 0)

        logger.info("Refreshed %d whale wallets from leaderboard", len(entries))

    # ─── On-chain Trade Scanning ──────────────────────────────

    def scan_new_blocks(self) -> list[dict]:
        """Scan latest Polygon blocks for large CTF Exchange trades.

        Returns list of raw trade event dicts.
        Disabled when Alchemy returns 400 on eth_getLogs for high-traffic contracts.
        """
        if not self._w3:
            return []

        try:
            current_block = self._w3.eth.block_number
        except Exception as exc:
            logger.debug("Failed to get block number: %s", exc)
            return []

        if current_block <= self._last_block:
            return []

        # Cap to avoid Alchemy log-size limits (Polygon ~2s/block, 10 blocks = ~20s)
        from_block = self._last_block + 1
        to_block = min(current_block, from_block + 10)

        trades = []
        for contract in [self._ctf_contract, self._neg_risk_contract]:
            if not contract:
                continue
            try:
                events = contract.events.OrderFilled.get_logs(
                    from_block=from_block, to_block=to_block
                )
                for evt in events:
                    args = evt["args"]
                    # makerAmountFilled is in USDC wei (6 decimals)
                    size_usdc = args.get("makerAmountFilled", 0) / 1e6
                    if size_usdc < WHALE_MIN_SIZE_USDC:
                        continue

                    trades.append({
                        "tx_hash": evt["transactionHash"].hex(),
                        "block_number": evt["blockNumber"],
                        "maker": args.get("maker", "").lower(),
                        "taker": args.get("taker", "").lower(),
                        "maker_asset_id": str(args.get("makerAssetId", 0)),
                        "taker_asset_id": str(args.get("takerAssetId", 0)),
                        "size_usdc": size_usdc,
                        "price": self._compute_price(args),
                        "timestamp": self._block_to_datetime(evt["blockNumber"]),
                    })
            except ContractLogicError as exc:
                logger.debug("Contract logic error: %s", exc)
            except Exception as exc:
                logger.debug("Block scan error (blocks %d-%d): %s", from_block, to_block, exc)
                self._last_block = to_block  # advance even on error to avoid re-scanning same blocks

        self._last_block = to_block
        if trades:
            logger.info(
                "Found %d whale trades in blocks %d-%d",
                len(trades), from_block, to_block,
            )
        return trades

    def _compute_price(self, args: dict) -> float:
        """Estimate trade price from filled amounts."""
        maker = args.get("makerAmountFilled", 0)
        taker = args.get("takerAmountFilled", 0)
        if taker == 0:
            return 0.0
        return round(maker / (maker + taker), 4)

    def _block_to_datetime(self, block_number: int) -> datetime:
        """Convert a block number to an approximate UTC datetime."""
        try:
            block = self._w3.eth.get_block(block_number)
            return datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)
        except Exception:
            return datetime.now(tz=timezone.utc)

    # ─── DB Persistence ───────────────────────────────────────

    def persist_trades(self, raw_trades: list[dict]):
        """Save on-chain trades to database. Records all large trades regardless of
        whether the maker is a known whale — and auto-registers new large traders."""
        if not raw_trades:
            return

        with get_db() as db:
            known_whales = {w.address for w in db.query(WhaleWallet).all()}

            # Build token_id → (market_id, side) lookup from markets table
            token_map = self._build_token_map(db)

            for trade in raw_trades:
                maker = trade["maker"]

                # Auto-register any large trader as a tracked whale wallet
                if maker not in known_whales:
                    new_wallet = WhaleWallet(
                        address=maker,
                        source="detected",
                        last_active=trade["timestamp"],
                    )
                    db.add(new_wallet)
                    known_whales.add(maker)
                    logger.info("New whale detected: %s ($%.0f trade)", maker, trade["size_usdc"])

                # Skip duplicate tx hashes
                existing = db.query(WhaleTrade).filter_by(tx_hash=trade["tx_hash"]).first()
                if existing:
                    continue

                # Determine BUY vs SELL from CTF Exchange asset IDs:
                # makerAssetId == "0" means maker is paying USDC → buying outcome tokens (BUY)
                # makerAssetId != "0" means maker is selling outcome tokens for USDC (SELL)
                maker_asset = trade["maker_asset_id"]
                if maker_asset == "0":
                    action = "BUY"
                    token_id = trade["taker_asset_id"]  # token being bought
                else:
                    action = "SELL"
                    token_id = maker_asset              # token being sold

                market_id, side = token_map.get(token_id, (None, "YES"))

                whale_trade = WhaleTrade(
                    tx_hash=trade["tx_hash"],
                    wallet_address=maker,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    action=action,
                    size_usdc=trade["size_usdc"],
                    price=trade["price"],
                    block_number=trade["block_number"],
                    timestamp=trade["timestamp"],
                )
                db.add(whale_trade)

                # Update whale last_active
                wallet = db.query(WhaleWallet).filter_by(address=maker).first()
                if wallet:
                    wallet.last_active = trade["timestamp"]
                    wallet.total_trades = (wallet.total_trades or 0) + 1

    # ─── Whale Signal Computation ─────────────────────────────

    def compute_whale_signal(self, market_id: str, lookback_hours: int = 24) -> float:
        """
        Compute net whale buy pressure for a market over the last N hours.

        Returns a value from -1 (heavy selling) to +1 (heavy buying),
        weighted by trade size.
        """
        from datetime import timedelta
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)

        with get_db() as db:
            trades = (
                db.query(WhaleTrade)
                .filter(
                    WhaleTrade.market_id == market_id,
                    WhaleTrade.timestamp >= cutoff,
                )
                .all()
            )

        if not trades:
            return 0.0

        buy_volume = sum(t.size_usdc for t in trades if t.action == "BUY")
        sell_volume = sum(t.size_usdc for t in trades if t.action == "SELL")
        total = buy_volume + sell_volume

        if total == 0:
            return 0.0

        return round((buy_volume - sell_volume) / total, 4)

    def _build_token_map(self, db) -> dict:
        """Return dict mapping token_id → (market_id, side) from markets table."""
        token_map = {}
        try:
            from db.models import Market as _Market
            rows = db.query(_Market.id, _Market.yes_token_id, _Market.no_token_id).filter(
                _Market.yes_token_id != None
            ).limit(10000).all()
            for market_id, yes_tok, no_tok in rows:
                if yes_tok:
                    token_map[str(yes_tok)] = (market_id, "YES")
                if no_tok:
                    token_map[str(no_tok)] = (market_id, "NO")
        except Exception as exc:
            logger.debug("token_map build error: %s", exc)
        return token_map

    # ─── API-based Activity (no on-chain needed) ──────────────

    async def poll_global_whale_activity(self):
        """Pull recent large trades from Polymarket /trades feed.
        No whale address list needed — catches any wallet trading at whale scale."""
        if not self.poly_scraper:
            return

        try:
            trades = await self.poly_scraper.get_global_trades(limit=200)
        except Exception as exc:
            logger.warning("Global trades fetch failed: %s", exc)
            return

        if not trades:
            return

        saved = 0
        for trade in trades:
            size = float(trade.get("size") or 0)
            if size < WHALE_MIN_SIZE_USDC:
                continue

            tx_hash = trade.get("transactionHash") or ""
            if not tx_hash:
                continue

            maker = (trade.get("proxyWallet") or "").lower()
            if not maker:
                continue

            # Timestamp is a Unix int in this API
            ts_raw = trade.get("timestamp")
            try:
                timestamp = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc) if ts_raw else datetime.now(tz=timezone.utc)
            except Exception:
                timestamp = datetime.now(tz=timezone.utc)

            with get_db() as db:
                if db.query(WhaleTrade).filter_by(tx_hash=tx_hash).first():
                    continue

                if not db.query(WhaleWallet).filter_by(address=maker).first():
                    db.add(WhaleWallet(
                        address=maker,
                        label=trade.get("pseudonym") or trade.get("name"),
                        source="detected",
                        last_active=timestamp,
                    ))
                    logger.info("New whale detected: %s ($%.0f — %s)", maker[:16], size, trade.get("pseudonym", ""))

                db.add(WhaleTrade(
                    tx_hash=tx_hash,
                    wallet_address=maker,
                    market_id=trade.get("conditionId"),
                    token_id=trade.get("asset"),
                    side=(trade.get("outcome") or "YES").upper()[:3],
                    action=(trade.get("side") or "BUY").upper(),
                    size_usdc=size,
                    price=float(trade.get("price") or 0),
                    block_number=0,
                    timestamp=timestamp,
                ))
                saved += 1

        if saved:
            logger.info("Global trades: saved %d new whale-size trades (>=$%.0f)", saved, WHALE_MIN_SIZE_USDC)

    async def poll_whale_activity_via_api(self):
        """Use Polymarket Data API to track whale wallet activity.
        Auto-seeds the whale list from the leaderboard when empty."""
        if not self.poly_scraper:
            return

        with get_db() as db:
            whales = db.query(WhaleWallet).limit(100).all()
            addresses = [w.address for w in whales]

        # Seed from leaderboard if whale list is still empty
        if not addresses:
            await self.refresh_whale_list_from_leaderboard()
            with get_db() as db:
                whales = db.query(WhaleWallet).limit(100).all()
                addresses = [w.address for w in whales]

        for address in addresses:
            trades = await self.poly_scraper.get_wallet_trades(address, limit=20)
            for trade in trades:
                size = float(trade.get("size") or trade.get("usdcSize") or 0)
                if size < WHALE_MIN_SIZE_USDC:
                    continue

                tx_hash = trade.get("transactionHash") or trade.get("id") or ""
                if not tx_hash:
                    continue

                timestamp_raw = trade.get("timestamp") or trade.get("createdAt")
                timestamp = datetime.utcnow()
                if timestamp_raw:
                    try:
                        timestamp = datetime.fromisoformat(
                            str(timestamp_raw).replace("Z", "+00:00")
                        )
                    except Exception:
                        pass

                with get_db() as db:
                    existing = db.query(WhaleTrade).filter_by(tx_hash=tx_hash).first()
                    if existing:
                        continue

                    wt = WhaleTrade(
                        tx_hash=tx_hash,
                        wallet_address=address,
                        market_id=trade.get("conditionId") or trade.get("marketId"),
                        token_id=trade.get("tokenId"),
                        side=trade.get("outcome", "YES").upper(),
                        action=trade.get("side", "BUY").upper(),
                        size_usdc=size,
                        price=float(trade.get("price") or 0),
                        block_number=0,
                        timestamp=timestamp,
                    )
                    db.add(wt)
