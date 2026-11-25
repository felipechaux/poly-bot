"""
Settings — single source of truth for runtime configuration.
Secrets / per-machine values come from env (.env); everything else from config/default.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "default.yaml"

YAML_SECTIONS = (
    "portfolio",
    "schedule",
    "market_data",
    "costs",
    "risk",
    "strategies",
    "universe",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Only paper mode exists in Phase 1. advisor/live come later (see docs/INVEST_BOT_PLAN.md).
    mode: Literal["paper"] = Field(default="paper", alias="POLY_MODE")

    db_path: str = Field(default="data/invest_bot.db")
    config_path: str = Field(default=str(DEFAULT_CONFIG_PATH))

    # Notifications (ntfy.sh). Empty topic = notifications disabled.
    ntfy_server: str = Field(default="https://ntfy.sh", validation_alias="NTFY_SERVER")
    ntfy_topic: str = Field(default="", validation_alias="NTFY_TOPIC")
    ntfy_token: str = Field(default="", validation_alias="NTFY_TOKEN")

    dashboard_url: str = Field(default="", validation_alias="DASHBOARD_URL")

    log_format: Literal["console", "json"] = Field(default="console", validation_alias="LOG_FORMAT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Loaded from YAML
    portfolio: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    market_data: dict[str, Any] = Field(default_factory=dict)
    costs: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    strategies: dict[str, Any] = Field(default_factory=dict)
    universe: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _load_yaml(self) -> "Settings":
        path = Path(self.config_path)
        if path.exists():
            data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
            for key in YAML_SECTIONS:
                if not getattr(self, key) and key in data:
                    object.__setattr__(self, key, data[key])
        return self

    @property
    def base_currency(self) -> str:
        return str(self.portfolio.get("base_currency", "COP"))

    @property
    def initial_cash(self) -> dict[str, float]:
        raw = self.portfolio.get("initial_cash", {"COP": 10_000_000.0, "USD": 5_000.0})
        return {str(k): float(v) for k, v in raw.items()}

    def get_strategy_config(self, name: str) -> dict[str, Any]:
        cfg: dict[str, Any] = self.strategies.get(name, {})
        return cfg

    def active_strategies(self) -> list[str]:
        return [
            name
            for name, cfg in self.strategies.items()
            if isinstance(cfg, dict) and cfg.get("enabled", False)
        ]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
