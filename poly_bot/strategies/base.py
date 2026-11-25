"""
Strategy base class — the central interface contract.
Strategies are pure functions of context: they receive data, return signals.
They NEVER call the executor or portfolio directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from poly_bot.market_data.models import Quote
from poly_bot.market_data.universe import Universe
from poly_bot.portfolio.models import PortfolioSnapshot


@dataclass
class Signal:
    """A trading signal: buy/sell `value` (in the asset's currency) of `ticker`."""

    ticker: str
    side: Literal["BUY", "SELL"]
    value: float | None = None
    quantity: float | None = None  # alternative to value (e.g. sell all)
    rationale: str = ""


@dataclass
class DailyContext:
    """Everything a strategy sees once per daily cycle."""

    today: date
    quotes: dict[str, Quote]
    usdcop: float
    portfolio: PortfolioSnapshot
    universe: Universe
    last_signal_date: date | None = None  # last cycle this strategy emitted signals
    extra: dict[str, Any] = field(default_factory=dict)  # dry_run, cached_analysis, track_record…
    # Outputs besides signals (persisted / shown by the bot):
    events: list[dict[str, Any]] = field(default_factory=list)  # → agent_events
    notes: list[str] = field(default_factory=list)  # → daily summary lines


class Strategy(ABC):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def on_daily(self, ctx: DailyContext) -> list[Signal]:
        """Called once per daily cycle. Return signals (SELLs are executed before BUYs)."""
        ...

    def validate(self, universe: Universe) -> None:
        """Optional: fail fast on bad config at startup."""

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    def _param(self, key: str, default: Any = None) -> Any:
        return self._config.get("params", {}).get(key, default)
