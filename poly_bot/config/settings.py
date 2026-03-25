"""
Settings — single source of truth for all runtime configuration.
Merges .env secrets with config/default.yaml defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-config models
# ---------------------------------------------------------------------------


class FeedConfig:
    poll_interval_seconds: float = 2.0
    market_refresh_seconds: float = 60.0
    max_markets: int = 50


class RiskConfig:
    max_position_size_usdc: float = 500.0
    max_total_exposure_usdc: float = 3000.0
    max_position_count: int = 10
    min_market_liquidity_usdc: float = 1000.0
    min_price: float = 0.02
    max_price: float = 0.98
    order_debounce_seconds: float = 30.0


class PaperConfig:
    initial_balance_usdc: float = 10_000.0
    fill_mode: str = "order_book"
    simulated_latency_ms: int = 200


# ---------------------------------------------------------------------------
# Main settings class
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Trading mode
    mode: Literal["paper", "live"] = Field(default="paper", alias="POLY_MODE")
    enable_live_trading: bool = Field(default=False)

    # Wallet (required for live mode)
    private_key: SecretStr | None = Field(default=None)
    chain_id: int = Field(default=137)

    # API endpoints
    clob_host: str = Field(default="https://clob.polymarket.com")
    gamma_host: str = Field(default="https://gamma-api.polymarket.com")

    # Paper trading
    paper_initial_balance: float = Field(default=10_000.0)

    # Logging
    log_format: Literal["console", "json"] = Field(
        default="console", validation_alias="LOG_FORMAT"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Loaded from YAML (not env)
    feed: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    paper: dict[str, Any] = Field(default_factory=dict)
    strategies: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _load_yaml_defaults(self) -> "Settings":
        """Merge YAML config file under env vars."""
        yaml_path = Path(__file__).parent.parent.parent / "config" / "default.yaml"
        if yaml_path.exists():
            with open(yaml_path) as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}
            for key in ("feed", "risk", "paper", "strategies"):
                if not getattr(self, key) and key in data:
                    object.__setattr__(self, key, data[key])
        return self

    @model_validator(mode="after")
    def _validate_live_mode(self) -> "Settings":
        if self.mode == "live":
            if not self.enable_live_trading:
                raise ValueError(
                    "Set ENABLE_LIVE_TRADING=true to enable live trading. "
                    "Use POLY_MODE=paper for safe paper trading."
                )
            if not self.private_key:
                raise ValueError(
                    "POLY_PRIVATE_KEY is required for live trading mode."
                )
        return self

    def get_risk(self, key: str, default: Any = None) -> Any:
        return self.risk.get(key, default)

    def get_strategy_config(self, name: str) -> dict[str, Any]:
        return self.strategies.get(name, {})

    def active_strategies(self) -> list[str]:
        return [
            name
            for name, cfg in self.strategies.items()
            if isinstance(cfg, dict) and cfg.get("enabled", False)
        ]


# Singleton — import this everywhere
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
