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
        """Fetch a diverse pool of markets from Gamma API, balanced by category."""
        log.info("feed.refreshing_markets", min_liquidity=self._min_liquidity, max_markets=self._max_markets)
        try:
            # Fetch a large pool so we have enough variety to pick from
            fetch_limit = max(self._max_markets * 4, 200)
            markets = await self._gamma.get_markets(
                limit=fetch_limit,
                active=True,
                closed=False,
            )
            log.info("feed.gamma_returned", count=len(markets))

            # Filter by liquidity and accepting orders
            filtered = [
                m for m in markets
                if m.liquidity >= self._min_liquidity and m.accepting_orders
            ]
            log.info("feed.after_liquidity_filter", count=len(filtered),
                     dropped=len(markets) - len(filtered))

            # Group by category and take top markets per category (diversity)
            from collections import defaultdict
            by_category: dict[str, list[Market]] = defaultdict(list)
            for m in filtered:
                cat = (m.category or "Other").strip() or "Other"
                by_category[cat].append(m)

            # Sort within each category by liquidity
            for cat in by_category:
                by_category[cat].sort(key=lambda m: m.liquidity, reverse=True)

            # Interleave: round-robin across categories to fill max_markets slots
            selected: list[Market] = []
            slots_per_cat = max(2, self._max_markets // max(len(by_category), 1))
            categories_sorted = sorted(by_category.keys(), key=lambda c: by_category[c][0].liquidity, reverse=True)

            # First pass: up to slots_per_cat per category
            for cat in categories_sorted:
                selected.extend(by_category[cat][:slots_per_cat])
                if len(selected) >= self._max_markets:
                    break

            # Fill remaining slots with highest-liquidity leftovers
            if len(selected) < self._max_markets:
                seen = {m.condition_id for m in selected}
                remaining = sorted(
                    [m for m in filtered if m.condition_id not in seen],
                    key=lambda m: m.liquidity,
                    reverse=True,
                )
                selected.extend(remaining[: self._max_markets - len(selected)])

            selected = selected[: self._max_markets]

            log.info(
                "feed.diversity_selection",
                total_candidates=len(filtered),
                categories=len(by_category),
                selected=len(selected),
                category_breakdown={cat: len(by_category[cat]) for cat in categories_sorted[:8]},
            )

            # Enrich markets that have no token IDs
            enriched = await asyncio.gather(
                *[self._enrich_tokens(m) for m in selected],
                return_exceptions=True,
            )
            errors = [r for r in enriched if isinstance(r, Exception)]
            with_tokens = [m for m in enriched if isinstance(m, Market) and m.yes_token]
            without_tokens = [m for m in enriched if isinstance(m, Market) and not m.yes_token]

            if errors:
                log.error("feed.enrichment_errors", count=len(errors), first_error=str(errors[0]))
            if without_tokens:
                log.warning("feed.markets_dropped_no_tokens", count=len(without_tokens))

            self._markets = {m.condition_id: m for m in with_tokens}
            self._last_market_refresh = datetime.utcnow()
            log.info(
                "feed.markets_updated",
                count=len(self._markets),
                sample=[{"q": m.question[:50], "cat": m.category, "price": m.yes_token.price}
                        for m in list(with_tokens)[:5]],
            )
        except Exception as exc:
            log.error("feed.market_refresh_failed", error=str(exc), error_type=type(exc).__name__,
                      exc_info=True)

    async def _poll_all(self) -> None:
        """Poll order books for all tracked markets concurrently."""
        tasks = [
            self._poll_market(market)
            for market in self._markets.values()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _enrich_tokens(self, market: Market) -> Market:
        """If market has no token IDs, fetch them from the CLOB API."""
        if market.yes_token:
            log.debug("feed.token_skip_enrich", condition_id=market.condition_id,
                      yes_price=market.yes_token.price)
            return market
        log.info("feed.enriching_via_clob", condition_id=market.condition_id,
                 question=market.question[:50])
        tokens = await self._clob.get_market_tokens(market.condition_id)
        if not tokens:
            log.warning("feed.clob_returned_no_tokens", condition_id=market.condition_id)
        return market.model_copy(update={"tokens": tokens})

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
