"""
Live execution engine — places real orders on Polymarket via py-clob-client.
ONLY used when POLY_MODE=live AND ENABLE_LIVE_TRADING=true.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from poly_bot.execution.models import ExecutionReport, Fill, OrderRequest, OrderStatus
from poly_bot.market_data.clob_client import AsyncClobClient
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)


class LiveExecutor:
    """Real order execution via Polymarket CLOB API."""

    is_paper: bool = False

    def __init__(self, clob_client: AsyncClobClient) -> None:
        self._clob = clob_client
        self._fill_callbacks: list[Callable[[Fill], Any]] = []

    async def submit_order(self, request: OrderRequest) -> ExecutionReport:
        """Place a real order on Polymarket."""
        order_id = str(uuid.uuid4())

        log.warning(
            "live.order_submitted",
            order_id=order_id,
            token_id=request.token_id,
            side=request.side,
            price=request.price,
            size_usdc=request.size_usdc,
        )

        client = self._clob._get_client()  # type: ignore[attr-defined]

        try:
            # Build order via py-clob-client
            from py_clob_client.clob_types import (  # type: ignore[import-untyped]
                LimitOrderArgs, MarketOrderArgs, OrderType,
            )

            # Use GTC limit order for predictable fills
            args = LimitOrderArgs(
                token_id=request.token_id,
                price=request.price,
                size=request.size_usdc / request.price,  # Convert to shares
                side=request.side,
            )

            result = await self._clob._run(client.create_and_post_order, args)
            real_order_id = str(getattr(result, "orderID", order_id))

            log.info("live.order_placed", order_id=real_order_id)
            return ExecutionReport(
                order_id=real_order_id,
                request=request,
                status="open",
            )

        except Exception as exc:
            log.error("live.order_failed", error=str(exc), order_id=order_id)
            return ExecutionReport(
                order_id=order_id,
                request=request,
                status="rejected",
                rejection_reason=str(exc),
            )

    async def cancel_order(self, order_id: str) -> bool:
        client = self._clob._get_client()  # type: ignore[attr-defined]
        try:
            await self._clob._run(client.cancel, order_id)
            log.info("live.order_cancelled", order_id=order_id)
            return True
        except Exception as exc:
            log.error("live.cancel_failed", order_id=order_id, error=str(exc))
            return False

    async def get_open_orders(self) -> list[OrderRequest]:
        # Live open orders are fetched from the API; not tracked locally
        return []

    def on_fill(self, callback: Callable[[Fill], Any]) -> None:
        self._fill_callbacks.append(callback)
