"""Strategy registry — maps names to classes and instantiates from config."""

from __future__ import annotations

from typing import Any

from poly_bot.strategies.base import Strategy
from poly_bot.strategies.core_allocation import CoreAllocationStrategy
from poly_bot.strategies.llm_analyst import LLMAnalystStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "core_allocation": CoreAllocationStrategy,
    "llm_analyst": LLMAnalystStrategy,
}


def load_strategies(strategy_configs: dict[str, Any]) -> list[Strategy]:
    """Instantiate enabled strategies. Bad config raises — better than trading wrong."""
    strategies: list[Strategy] = []
    for name, cfg in strategy_configs.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        kind = str(cfg.get("class", name))  # several instances may share a class
        cls = STRATEGY_REGISTRY.get(kind)
        if cls is None:
            raise ValueError(f"Unknown strategy: '{kind}'. Available: {list(STRATEGY_REGISTRY)}")
        strategies.append(cls({**cfg, "name": name}))
    # Satellite strategies must not trade the core's tickers
    core_targets: set[str] = set()
    for s in strategies:
        if isinstance(s, CoreAllocationStrategy):
            core_targets |= set(s.targets)
    for s in strategies:
        if isinstance(s, LLMAnalystStrategy):
            s.excluded |= core_targets
    names = [s.name for s in strategies]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate strategy names: {names}")
    return strategies
