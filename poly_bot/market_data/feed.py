"""
Market data feed — polls CLOB order books and Gamma market metadata,
publishes MarketUpdate events to an asyncio.Queue consumed by strategies.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from poly_bot.market_data.clob_client import AsyncClobClient
from poly_bot.market_data.gamma_client import GammaClient
from poly_bot.market_data.models import Market, MarketUpdate, OrderBookSummary
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)


class MarketDataFeed:
    """
    Maintains a list of tracked markets and continuously polls for updates.
    Publishes MarketUpdate objects to the provided asyncio.Queue.
    """

    def __init__(
        self,
        clob_client: AsyncClobClient,
        gamma_client: GammaClient,
        queue: asyncio.Queue[MarketUpdate],
        poll_interval: float = 2.0,
        market_refresh_interval: float = 60.0,
        max_markets: int = 50,
        min_liquidity: float = 1000.0,
    ) -> None:
        self._clob = clob_client
        self._gamma = gamma_client
        self._queue = queue
        self._poll_interval = poll_interval
        self._market_refresh_interval = market_refresh_interval
        self._max_markets = max_markets
        self._min_liquidity = min_liquidity

        self._markets: dict[str, Market] = {}  # condition_id -> Market
        self._last_market_refresh: datetime | None = None
        self._running = False

    async def run(self) -> None:
        """Main feed loop. Runs until stopped."""
        self._running = True
        log.info("feed.starting", poll_interval=self._poll_interval)

        # Initial market load
        await self._refresh_markets()

        while self._running:
            now = datetime.utcnow()
            # Periodic market metadata refresh
            if (
                self._last_market_refresh is None
                or (now - self._last_market_refresh).total_seconds() >= self._market_refresh_interval
            ):
                await self._refresh_markets()

            # Poll all tracked markets
            await self._poll_all()
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False
        log.info("feed.stopped")

    async def _refresh_markets(self) -> None:
        """Fetch active markets from Gamma API, apply filters."""
        log.debug("feed.refreshing_markets")
        try:
            markets = await self._gamma.get_markets(
                limit=self._max_markets,
                active=True,
                closed=False,
            )
            # Filter by liquidity
            filtered = [
                m for m in markets
                if m.liquidity >= self._min_liquidity and m.accepting_orders
            ]
            # Keep top markets by liquidity
            filtered.sort(key=lambda m: m.liquidity, reverse=True)
            filtered = filtered[: self._max_markets]

            self._markets = {m.condition_id: m for m in filtered}
            self._last_market_refresh = datetime.utcnow()
            log.info("feed.markets_updated", count=len(self._markets))
        except Exception as exc:
            log.error("feed.market_refresh_failed", error=str(exc))

    async def _poll_all(self) -> None:
        """Poll order books for all tracked markets concurrently."""
        tasks = [
            self._poll_market(market)
            for market in self._markets.values()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_market(self, market: Market) -> None:
        """Poll order book for the YES token of a market."""
        yes_token = market.yes_token
        if yes_token is None:
            return

        order_book = await self._clob.get_order_book(yes_token.token_id)
        if order_book is None:
            return

        update = MarketUpdate(market=market, order_book=order_book)
        try:
            self._queue.put_nowait(update)
        except asyncio.QueueFull:
            log.warning("feed.queue_full", condition_id=market.condition_id)

    @property
    def tracked_markets(self) -> list[Market]:
        return list(self._markets.values())
