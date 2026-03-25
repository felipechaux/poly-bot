"""
Paper trading executor — simulates fills against real live order books.

Fill simulation levels:
  "instant"     — fill immediately at requested price (unrealistic, fast testing)
  "order_book"  — walk the live book to determine realistic fill price + slippage

Resting paper orders are checked on every call to on_market_update().
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable, Literal

from poly_bot.execution.models import (
    ExecutionReport,
    Fill,
    OrderRequest,
    OrderSide,
    OrderStatus,
)
from poly_bot.market_data.models import OrderBookSummary, PriceLevel
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

# Typical Polymarket taker/maker fees
TAKER_FEE = 0.0  # Polymarket currently 0% protocol fee (builder program)
MAKER_FEE = 0.0


class RestingOrder:
    """A paper limit order waiting to be matched."""

    def __init__(self, request: OrderRequest, order_id: str, remaining_usdc: float) -> None:
        self.request = request
        self.order_id = order_id
        self.remaining_usdc = remaining_usdc
        self.created_at = datetime.utcnow()
        self.fills: list[Fill] = []


class PaperExecutor:
    """
    Simulated execution engine for paper trading.
    Thread-safe via asyncio — all methods must be called from the event loop.
    """

    is_paper: bool = True

    def __init__(
        self,
        initial_balance_usdc: float = 10_000.0,
        fill_mode: Literal["instant", "order_book"] = "order_book",
        simulated_latency_ms: int = 200,
    ) -> None:
        self.balance_usdc = initial_balance_usdc
        self._fill_mode = fill_mode
        self._latency_ms = simulated_latency_ms
        self._resting: dict[str, RestingOrder] = {}  # order_id -> RestingOrder
        self._fill_callbacks: list[Callable[[Fill], Any]] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API (implements ExecutionEngine protocol)
    # ------------------------------------------------------------------

    async def submit_order(self, request: OrderRequest) -> ExecutionReport:
        """Simulate order submission. Walks order book to determine fills."""
        async with self._lock:
            order_id = str(uuid.uuid4())
            log.info(
                "paper.order_submitted",
                order_id=order_id,
                token_id=request.token_id,
                side=request.side,
                price=request.price,
                size_usdc=request.size_usdc,
                strategy=request.strategy,
            )

            if self._fill_mode == "instant":
                return await self._instant_fill(request, order_id)
            else:
                return await self._book_fill(request, order_id, book=None)

    async def submit_order_with_book(
        self, request: OrderRequest, book: OrderBookSummary
    ) -> ExecutionReport:
        """Submit with a pre-fetched order book (used internally by strategy runner)."""
        async with self._lock:
            order_id = str(uuid.uuid4())
            if self._fill_mode == "instant":
                return await self._instant_fill(request, order_id)
            else:
                return await self._book_fill(request, order_id, book=book)

    async def cancel_order(self, order_id: str) -> bool:
        async with self._lock:
            if order_id in self._resting:
                del self._resting[order_id]
                log.info("paper.order_cancelled", order_id=order_id)
                return True
            return False

    async def get_open_orders(self) -> list[OrderRequest]:
        async with self._lock:
            return [r.request for r in self._resting.values()]

    def on_fill(self, callback: Callable[[Fill], Any]) -> None:
        self._fill_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Market update — check resting orders for matches
    # ------------------------------------------------------------------

    async def on_market_update(self, book: OrderBookSummary) -> None:
        """Called by the feed on every new order book snapshot. Matches resting orders."""
        async with self._lock:
            matched_ids: list[str] = []
            for order_id, resting in self._resting.items():
                if resting.request.token_id != book.asset_id:
                    continue
                fill = self._try_match_resting(resting, book)
                if fill:
                    resting.fills.append(fill)
                    self.balance_usdc += (
                        fill.cost_usdc if resting.request.side == "SELL"
                        else -fill.cost_usdc
                    )
                    await self._emit_fill(fill)
                    if resting.remaining_usdc <= 0.01:
                        matched_ids.append(order_id)
            for oid in matched_ids:
                del self._resting[oid]

    # ------------------------------------------------------------------
    # Internal fill simulation
    # ------------------------------------------------------------------

    async def _instant_fill(self, request: OrderRequest, order_id: str) -> ExecutionReport:
        """Fill at requested price immediately, no slippage."""
        shares = request.size_usdc / request.price
        fee = request.size_usdc * TAKER_FEE

        if not self._check_balance(request):
            return ExecutionReport(
                order_id=order_id,
                request=request,
                status="rejected",
                rejection_reason=f"Insufficient paper balance: {self.balance_usdc:.2f} USDC",
            )

        fill = Fill(
            order_id=order_id,
            token_id=request.token_id,
            side=request.side,
            price=request.price,
            size=shares,
            cost_usdc=request.size_usdc,
            fee_usdc=fee,
            strategy=request.strategy,
            is_paper=True,
        )

        self._apply_balance(request, request.size_usdc)
        await asyncio.sleep(self._latency_ms / 1000)
        await self._emit_fill(fill)

        return ExecutionReport(
            order_id=order_id,
            request=request,
            status="filled",
            fills=[fill],
        )

    async def _book_fill(
        self,
        request: OrderRequest,
        order_id: str,
        book: OrderBookSummary | None,
    ) -> ExecutionReport:
        """Walk order book to simulate realistic fills."""
        if not self._check_balance(request):
            return ExecutionReport(
                order_id=order_id,
                request=request,
                status="rejected",
                rejection_reason=f"Insufficient paper balance: {self.balance_usdc:.2f} USDC",
            )

        if book is None:
            # Fall back to instant fill if no book provided
            return await self._instant_fill(request, order_id)

        fills, remaining = self._walk_book(request, order_id, book)

        for fill in fills:
            self._apply_balance(request, fill.cost_usdc)
            await self._emit_fill(fill)

        if remaining > 0.01:
            # Place remainder as resting limit order
            resting = RestingOrder(request, order_id, remaining)
            self._resting[order_id] = resting
            log.info(
                "paper.order_resting",
                order_id=order_id,
                remaining_usdc=remaining,
                limit_price=request.price,
            )
            status: OrderStatus = "partial" if fills else "open"
        else:
            status = "filled"

        return ExecutionReport(
            order_id=order_id,
            request=request,
            status=status,
            fills=fills,
        )

    def _walk_book(
        self,
        request: OrderRequest,
        order_id: str,
        book: OrderBookSummary,
    ) -> tuple[list[Fill], float]:
        """Walk the order book to fill against available liquidity."""
        if request.side == "BUY":
            levels = sorted(book.asks, key=lambda x: x.price)
            price_check = lambda level_price: level_price <= request.price
        else:
            levels = sorted(book.bids, key=lambda x: -x.price)
            price_check = lambda level_price: level_price >= request.price

        remaining = request.size_usdc
        fills: list[Fill] = []

        for level in levels:
            if not price_check(level.price) or remaining <= 0.01:
                break

            level_capacity_usdc = level.price * level.size
            fill_usdc = min(remaining, level_capacity_usdc)
            fill_shares = fill_usdc / level.price
            fee = fill_usdc * TAKER_FEE

            fill = Fill(
                order_id=order_id,
                token_id=request.token_id,
                side=request.side,
                price=level.price,
                size=fill_shares,
                cost_usdc=fill_usdc,
                fee_usdc=fee,
                strategy=request.strategy,
                is_paper=True,
            )
            fills.append(fill)
            remaining -= fill_usdc

        return fills, max(remaining, 0.0)

    def _try_match_resting(
        self, resting: RestingOrder, book: OrderBookSummary
    ) -> Fill | None:
        """Check if a resting order can now be matched against the current book."""
        request = resting.request
        if request.side == "BUY":
            best = book.best_ask
            if best is None or best > request.price:
                return None
            fill_price = best
        else:
            best = book.best_bid
            if best is None or best < request.price:
                return None
            fill_price = best

        fill_usdc = resting.remaining_usdc
        fill_shares = fill_usdc / fill_price
        fee = fill_usdc * MAKER_FEE
        resting.remaining_usdc = 0.0

        return Fill(
            order_id=resting.order_id,
            token_id=request.token_id,
            side=request.side,
            price=fill_price,
            size=fill_shares,
            cost_usdc=fill_usdc,
            fee_usdc=fee,
            strategy=request.strategy,
            is_paper=True,
        )

    def _check_balance(self, request: OrderRequest) -> bool:
        if request.side == "BUY" and request.size_usdc > self.balance_usdc:
            return False
        return True

    def _apply_balance(self, request: OrderRequest, usdc: float) -> None:
        if request.side == "BUY":
            self.balance_usdc -= usdc
        else:
            self.balance_usdc += usdc

    async def _emit_fill(self, fill: Fill) -> None:
        for cb in self._fill_callbacks:
            try:
                result = cb(fill)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log.error("paper.fill_callback_error", error=str(exc))
