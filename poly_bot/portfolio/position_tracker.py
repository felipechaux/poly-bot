"""
Position tracker — maintains in-memory state of all open positions.
Updated on every fill event. Thread-safe via asyncio.
"""

from __future__ import annotations

from datetime import datetime

from poly_bot.execution.models import Fill
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot, Position

log = get_logger(__name__)


class PositionTracker:
    """
    Maintains open positions and P&L accounting.
    Call on_fill() for every Fill event.
    """

    def __init__(self, initial_balance_usdc: float = 10_000.0) -> None:
        self.cash_usdc = initial_balance_usdc
        self._positions: dict[str, Position] = {}  # token_id -> Position
        self._total_realized_pnl = 0.0
        self._total_fees = 0.0
        self._win_count = 0
        self._loss_count = 0
        self._trade_count = 0
        self._current_prices: dict[str, float] = {}  # token_id -> current mid price

    def on_fill(self, fill: Fill) -> None:
        """Process a fill event and update positions."""
        if fill.side == "BUY":
            self._on_buy(fill)
        else:
            self._on_sell(fill)

        self._total_fees += fill.fee_usdc
        log.info(
            "portfolio.fill_processed",
            token_id=fill.token_id,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            cost_usdc=fill.cost_usdc,
            cash_remaining=self.cash_usdc,
        )

    def update_price(self, token_id: str, price: float) -> None:
        """Update current market price for unrealized P&L calculation."""
        self._current_prices[token_id] = price

    def snapshot(self) -> PortfolioSnapshot:
        """Return current portfolio state with unrealized P&L."""
        positions = list(self._positions.values())

        unrealized = 0.0
        for pos in positions:
            current = self._current_prices.get(pos.token_id, pos.avg_cost_basis)
            unrealized += (current - pos.avg_cost_basis) * pos.shares

        return PortfolioSnapshot(
            cash_usdc=self.cash_usdc,
            positions=positions,
            total_realized_pnl=self._total_realized_pnl,
            total_unrealized_pnl=unrealized,
            total_fees_paid=self._total_fees,
            win_count=self._win_count,
            loss_count=self._loss_count,
            trade_count=self._trade_count,
        )

    def get_position(self, token_id: str) -> Position | None:
        return self._positions.get(token_id)

    def open_position_count(self) -> int:
        return len(self._positions)

    def total_exposure_usdc(self) -> float:
        return sum(p.total_cost_usdc for p in self._positions.values())

    # ------------------------------------------------------------------
    # Internal accounting
    # ------------------------------------------------------------------

    def _on_buy(self, fill: Fill) -> None:
        pos = self._positions.get(fill.token_id)
        if pos is None:
            self._positions[fill.token_id] = Position(
                token_id=fill.token_id,
                side="BUY",
                shares=fill.size,
                avg_cost_basis=fill.price,
                total_cost_usdc=fill.cost_usdc,
                strategy=fill.strategy,
            )
        else:
            # Update average cost basis (FIFO blend)
            total_shares = pos.shares + fill.size
            total_cost = pos.total_cost_usdc + fill.cost_usdc
            pos.shares = total_shares
            pos.total_cost_usdc = total_cost
            pos.avg_cost_basis = total_cost / total_shares if total_shares > 0 else fill.price
            pos.last_updated = datetime.utcnow()

        # Deduct from cash (already tracked by executor, but tracker is independent)
        self.cash_usdc -= fill.cost_usdc + fill.fee_usdc

    def _on_sell(self, fill: Fill) -> None:
        pos = self._positions.get(fill.token_id)
        if pos is None:
            log.warning("portfolio.sell_without_position", token_id=fill.token_id)
            return

        # Realize P&L
        realized = (fill.price - pos.avg_cost_basis) * fill.size - fill.fee_usdc
        self._total_realized_pnl += realized
        self._trade_count += 1

        if realized > 0:
            self._win_count += 1
        else:
            self._loss_count += 1

        self.cash_usdc += fill.cost_usdc - fill.fee_usdc
        pos.realized_pnl += realized
        pos.shares -= fill.size
        pos.total_cost_usdc = pos.shares * pos.avg_cost_basis
        pos.last_updated = datetime.utcnow()

        if pos.shares <= 0.001:
            log.info(
                "portfolio.position_closed",
                token_id=fill.token_id,
                realized_pnl=realized,
                strategy=fill.strategy,
            )
            del self._positions[fill.token_id]
        else:
            log.info(
                "portfolio.position_reduced",
                token_id=fill.token_id,
                remaining_shares=pos.shares,
                realized_pnl=realized,
            )
