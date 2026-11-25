"""Strategy registry — maps names to classes and instantiates from config."""

from __future__ import annotations

from typing import Any

from poly_bot.strategies.base import Strategy
from poly_bot.strategies.core_allocation import CoreAllocationStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "core_allocation": CoreAllocationStrategy,
}


def load_strategies(strategy_configs: dict[str, Any]) -> list[Strategy]:
    """Instantiate enabled strategies. Bad config raises — better than trading wrong."""
    strategies: list[Strategy] = []
    for name, cfg in strategy_configs.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown strategy: '{name}'. Available: {list(STRATEGY_REGISTRY)}")
        strategies.append(cls(cfg))
    return strategies
