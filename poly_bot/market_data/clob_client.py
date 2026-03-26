"""
Async wrapper around py-clob-client (which is synchronous).
Runs blocking calls in a thread pool to preserve the async architecture.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from poly_bot.market_data.models import OrderBookSummary, PriceLevel, Token
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

# Lazy import — py-clob-client is only needed when actually trading
try:
    from py_clob_client.client import ClobClient as _SyncClobClient  # type: ignore[import-untyped]
    from py_clob_client.clob_types import ApiCreds  # type: ignore[import-untyped]

    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    log.warning("clob.sdk_not_installed", msg="py-clob-client not installed; live trading disabled")


class AsyncClobClient:
    """
    Async wrapper around the synchronous py-clob-client.
    For read-only operations (order books) no auth is needed.
    For trading, provide private_key.
    """

    def __init__(
        self,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        private_key: str | None = None,
    ) -> None:
        self._host = host
        self._chain_id = chain_id
        self._private_key = private_key
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clob")
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_client(self) -> Any:
        """Lazily initialize the sync client (thread-safe lazy init)."""
        if self._client is not None:
            return self._client
        if not _CLOB_AVAILABLE:
            raise RuntimeError("py-clob-client is not installed. Run: uv add py-clob-client")
        self._client = _SyncClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._private_key,
        )
        log.info("clob.client_initialized", host=self._host, chain_id=self._chain_id)
        return self._client

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking call in the thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def get_order_book(self, token_id: str) -> OrderBookSummary | None:
        """Fetch the current order book for a token (YES or NO)."""
        client = self._get_client()
        try:
            raw = await self._run(client.get_order_book, token_id)
            return self._parse_order_book(raw, token_id)
        except Exception as exc:
            log.error("clob.order_book_failed", token_id=token_id, error=str(exc))
            return None

    async def get_midpoint(self, token_id: str) -> float | None:
        """Fetch the current midpoint price for a token."""
        client = self._get_client()
        try:
            raw = await self._run(client.get_midpoint, token_id)
            if isinstance(raw, dict):
                return float(raw.get("mid", 0.0))
            return float(raw)
        except Exception as exc:
            log.error("clob.midpoint_failed", token_id=token_id, error=str(exc))
            return None

    async def get_last_trade_price(self, token_id: str) -> float | None:
        """Fetch the last trade price for a token."""
        client = self._get_client()
        try:
            raw = await self._run(client.get_last_trade_price, token_id)
            if isinstance(raw, dict):
                return float(raw.get("price", 0.0))
            return float(raw)
        except Exception as exc:
            log.error("clob.last_price_failed", token_id=token_id, error=str(exc))
            return None

    def _parse_order_book(self, raw: Any, token_id: str) -> OrderBookSummary:
        """Normalize py-clob-client order book response to our Pydantic model."""
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []

        if hasattr(raw, "bids"):
            for b in raw.bids or []:
                try:
                    bids.append(PriceLevel(price=float(b.price), size=float(b.size)))
                except Exception:
                    pass
        if hasattr(raw, "asks"):
            for a in raw.asks or []:
                try:
                    asks.append(PriceLevel(price=float(a.price), size=float(a.size)))
                except Exception:
                    pass

        market_id = ""
        if hasattr(raw, "market"):
            market_id = str(raw.market or "")

        return OrderBookSummary(
            market=market_id,
            asset_id=token_id,
            bids=bids,
            asks=asks,
        )

    async def get_market_tokens(self, condition_id: str) -> list[Token]:
        """Fetch YES/NO token IDs for a market from the CLOB API."""
        url = f"{self._host}/markets/{condition_id}"
        log.info("clob.fetching_tokens", condition_id=condition_id, url=url)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                log.info("clob.token_response", condition_id=condition_id, status=resp.status_code)
                resp.raise_for_status()
                data = resp.json()
            tokens = []
            for t in data.get("tokens", []):
                token_id = str(t.get("token_id", ""))
                outcome = str(t.get("outcome", ""))
                price = float(t.get("price", 0.0))
                if token_id:
                    tokens.append(Token(token_id=token_id, outcome=outcome, price=price))
            log.info("clob.tokens_fetched", condition_id=condition_id, count=len(tokens),
                     tokens=[{"outcome": t.outcome, "price": t.price} for t in tokens])
            return tokens
        except httpx.HTTPStatusError as exc:
            log.error("clob.market_tokens_http_error", condition_id=condition_id,
                      status=exc.response.status_code, body=exc.response.text[:200])
            return []
        except Exception as exc:
            log.error("clob.market_tokens_failed", condition_id=condition_id,
                      error=str(exc), error_type=type(exc).__name__)
            return []

    async def close(self) -> None:
        self._executor.shutdown(wait=False)
