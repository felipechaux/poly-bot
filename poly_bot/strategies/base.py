"""
Strategy base class — the central interface contract.
Strategies are pure functions of context: they receive data, return signals.
They NEVER call the executor or portfolio directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from poly_bot.execution.models import Fill, OrderType
from poly_bot.market_data.models import Market, MarketUpdate, OrderBookSummary
from poly_bot.portfolio.models import PortfolioSnapshot, Position


@dataclass
class Signal:
    """A trading signal emitted by a strategy."""

    token_id: str
    side: Literal["BUY", "SELL"]
    price: float
    size_usdc: float
    order_type: OrderType = "GTC"
    rationale: str = ""


@dataclass
class StrategyContext:
    """All context a strategy needs to make a decision."""

    update: MarketUpdate
    position: Position | None
    portfolio: PortfolioSnapshot
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def market(self) -> Market:
        return self.update.market

    @property
    def order_book(self) -> OrderBookSummary:
        return self.update.order_book

    @property
    def mid_price(self) -> float | None:
        return self.order_book.mid_price

    @property
    def best_bid(self) -> float | None:
        return self.order_book.best_bid

    @property
    def best_ask(self) -> float | None:
        return self.order_book.best_ask


class Strategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @abstractmethod
    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        """
        Called on every market data update.
        Return a list of signals (can be empty).
        Strategies should be fast — no blocking I/O here.
        """
        ...

    async def on_fill(self, fill: Fill, ctx: StrategyContext) -> list[Signal]:
        """
        Optional: react to a fill (e.g., place take-profit after entry fills).
        Default: no action.
        """
        return []

    async def on_start(self) -> None:
        """Optional: called once when the bot starts."""
        pass

    async def on_stop(self) -> None:
        """Optional: called once when the bot stops."""
        pass

    def _param(self, key: str, default: Any = None) -> Any:
        """Helper to get a strategy parameter from config."""
        params = self._config.get("params", {})
        return params.get(key, default)
