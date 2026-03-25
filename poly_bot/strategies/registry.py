"""Strategy registry — maps names to classes and instantiates from config."""

from __future__ import annotations

from typing import Any

from poly_bot.strategies.base import Strategy
from poly_bot.strategies.ai_research import AIResearchStrategy
from poly_bot.strategies.mean_reversion import MeanReversionStrategy
from poly_bot.strategies.momentum import MomentumStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "ai_research": AIResearchStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum": MomentumStrategy,
}


def load_strategies(strategy_configs: dict[str, Any]) -> list[Strategy]:
    """Instantiate all enabled strategies from config."""
    strategies: list[Strategy] = []
    for name, cfg in strategy_configs.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown strategy: '{name}'. Available: {list(STRATEGY_REGISTRY)}")
        strategies.append(cls(cfg))
    return strategies
