"""
Risk manager — pre-trade checks executed before every order submission.
All checks are synchronous and stateless (reads from portfolio snapshot).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from poly_bot.execution.models import OrderRequest
from poly_bot.market_data.models import Market
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot

log = get_logger(__name__)


class RiskManager:
    def __init__(
        self,
        max_position_size_usdc: float = 500.0,
        max_total_exposure_usdc: float = 3000.0,
        max_position_count: int = 10,
        min_market_liquidity_usdc: float = 1000.0,
        min_price: float = 0.02,
        max_price: float = 0.98,
        order_debounce_seconds: float = 30.0,
    ) -> None:
        self._max_position = max_position_size_usdc
        self._max_exposure = max_total_exposure_usdc
        self._max_positions = max_position_count
        self._min_liquidity = min_market_liquidity_usdc
        self._min_price = min_price
        self._max_price = max_price
        self._debounce_seconds = order_debounce_seconds
        self._recent_orders: dict[str, datetime] = {}  # debounce key -> last time

    def check(
        self,
        request: OrderRequest,
        portfolio: PortfolioSnapshot,
        market: Market | None = None,
    ) -> tuple[bool, str]:
        """
        Run all pre-trade checks.
        Returns (approved, reason). approved=True means OK to proceed.
        """
        # 1. Price sanity — reject near-resolved markets
        if request.price < self._min_price:
            return False, f"Price {request.price:.3f} below minimum {self._min_price}"
        if request.price > self._max_price:
            return False, f"Price {request.price:.3f} above maximum {self._max_price}"

        # 2. Order size
        if request.size_usdc > self._max_position:
            return False, f"Order size ${request.size_usdc:.2f} exceeds max ${self._max_position:.2f}"

        # 3. Total portfolio exposure
        new_exposure = portfolio.total_position_value + request.size_usdc
        if request.side == "BUY" and new_exposure > self._max_exposure:
            return False, (
                f"Total exposure ${new_exposure:.2f} would exceed max ${self._max_exposure:.2f}"
            )

        # 4. Position count
        existing = portfolio.get_position(request.token_id) if hasattr(portfolio, 'get_position') else None
        if request.side == "BUY" and existing is None:
            if len(portfolio.positions) >= self._max_positions:
                return False, f"Max position count {self._max_positions} reached"

        # 5. Cash check
        if request.side == "BUY" and request.size_usdc > portfolio.cash_usdc:
            return False, f"Insufficient cash: ${portfolio.cash_usdc:.2f} < ${request.size_usdc:.2f}"

        # 6. Market liquidity
        if market and market.liquidity < self._min_liquidity:
            return False, f"Market liquidity ${market.liquidity:.0f} below minimum ${self._min_liquidity:.0f}"

        # 7. Duplicate order debounce
        debounce_key = f"{request.token_id}:{request.side}:{request.price:.3f}"
        last = self._recent_orders.get(debounce_key)
        if last and (datetime.utcnow() - last).total_seconds() < self._debounce_seconds:
            return False, f"Duplicate signal within debounce window ({self._debounce_seconds}s)"

        # All checks passed
        self._recent_orders[debounce_key] = datetime.utcnow()
        self._cleanup_debounce()
        return True, ""

    def _cleanup_debounce(self) -> None:
        cutoff = datetime.utcnow() - timedelta(seconds=self._debounce_seconds * 2)
        expired = [k for k, v in self._recent_orders.items() if v < cutoff]
        for k in expired:
            del self._recent_orders[k]
