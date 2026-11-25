"""
Risk manager — pre-trade checks run before every order.

Limits are expressed as weights of total portfolio value (base currency):
per asset, per asset class, per region. Also: whitelist (universe), stale prices,
minimum order size and no short selling.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from poly_bot.execution.models import OrderRequest
from poly_bot.market_data.models import Asset, Quote
from poly_bot.market_data.universe import Universe
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot

log = get_logger(__name__)


class RiskManager:
    def __init__(
        self,
        universe: Universe,
        max_weight_per_asset: float = 0.5,
        max_weight_per_class: dict[str, float] | None = None,
        max_weight_per_region: dict[str, float] | None = None,
        min_order_value_usd: float = 25.0,
        max_stale_days: int = 5,
        allow_short: bool = False,
    ) -> None:
        self._universe = universe
        self._max_asset = max_weight_per_asset
        self._max_class = max_weight_per_class or {}
        self._max_region = max_weight_per_region or {}
        self._min_order_usd = min_order_value_usd
        self._max_stale_days = max_stale_days
        self._allow_short = allow_short

    @classmethod
    def from_config(cls, universe: Universe, risk: dict[str, Any], market_data: dict[str, Any]) -> "RiskManager":
        return cls(
            universe=universe,
            max_weight_per_asset=float(risk.get("max_weight_per_asset", 0.5)),
            max_weight_per_class=risk.get("max_weight_per_class") or {},
            max_weight_per_region=risk.get("max_weight_per_region") or {},
            min_order_value_usd=float(risk.get("min_order_value_usd", 25.0)),
            max_stale_days=int(market_data.get("max_stale_days", 5)),
            allow_short=bool(risk.get("allow_short", False)),
        )

    def _group_weight(self, snap: PortfolioSnapshot, attr: str, value: str) -> float:
        total = snap.total_value()
        if total <= 0:
            return 0.0
        acc = 0.0
        for p in snap.positions:
            a = self._universe.get(p.ticker)
            if a and getattr(a, attr) == value:
                acc += snap.to(p.market_value, p.currency)
        return acc / total

    def is_stale(self, quote: Quote, today: date) -> bool:
        return (today - quote.as_of).days > self._max_stale_days

    def check(
        self,
        request: OrderRequest,
        quote: Quote | None,
        snap: PortfolioSnapshot,
        today: date,
    ) -> tuple[bool, str]:
        asset: Asset | None = self._universe.get(request.ticker)
        if asset is None:
            return False, f"{request.ticker} no está en el universo permitido"
        if quote is None:
            return False, f"Sin precio para {request.ticker}"
        if self.is_stale(quote, today):
            return False, f"Precio de {request.ticker} desactualizado ({quote.as_of})"

        if request.value is not None:
            value_ccy = request.value
        elif request.quantity is not None:
            value_ccy = request.quantity * quote.price
        else:
            return False, "Orden sin valor ni cantidad"
        value_usd = snap.to(value_ccy, asset.currency, "USD")
        value_base = snap.to(value_ccy, asset.currency)

        if request.side == "SELL":
            held = snap.get_position(request.ticker)
            if held is None and not self._allow_short:
                return False, f"Sin posición en {request.ticker} (no se permiten cortos)"
            closing = held is not None and request.quantity is not None and (
                request.quantity >= held.quantity - 1e-9
            )
            if not closing and value_usd < self._min_order_usd:
                return False, f"Orden muy pequeña (US$ {value_usd:.2f} < {self._min_order_usd})"
            return True, ""

        if value_usd < self._min_order_usd:
            return False, f"Orden muy pequeña (US$ {value_usd:.2f} < {self._min_order_usd})"

        total = snap.total_value()
        if total <= 0:
            return False, "Portafolio sin valor"
        add = value_base / total

        w = snap.weight(request.ticker) + add
        if w > self._max_asset + 1e-9:
            return False, f"Peso de {request.ticker} sería {w:.1%} > máx {self._max_asset:.0%}"

        cls_limit = self._max_class.get(asset.asset_class)
        if cls_limit is not None:
            w = self._group_weight(snap, "asset_class", asset.asset_class) + add
            if w > cls_limit + 1e-9:
                return False, f"Clase {asset.asset_class} sería {w:.1%} > máx {cls_limit:.0%}"

        reg_limit = self._max_region.get(asset.region)
        if reg_limit is not None:
            w = self._group_weight(snap, "region", asset.region) + add
            if w > reg_limit + 1e-9:
                return False, f"Región {asset.region} sería {w:.1%} > máx {reg_limit:.0%}"

        return True, ""
