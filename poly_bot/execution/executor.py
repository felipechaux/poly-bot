"""
ExecutionEngine Protocol — the interface both PaperExecutor and LiveExecutor implement.
The bot only depends on this protocol, never on a concrete implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from poly_bot.execution.models import ExecutionReport, Fill, OrderRequest


@runtime_checkable
class ExecutionEngine(Protocol):
    """Abstract execution interface."""

    is_paper: bool

    async def submit_order(self, request: OrderRequest) -> ExecutionReport:
        """Submit an order. Returns an ExecutionReport with fill details."""
        ...

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        ...

    async def get_open_orders(self) -> list[OrderRequest]:
        """Return all currently open/resting orders."""
        ...

    def on_fill(self, callback: Any) -> None:
        """Register a callback to be called on each Fill event."""
        ...


from typing import Any  # noqa: E402
