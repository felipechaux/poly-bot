"""
Paper executor — simulates fills at the last daily close.

Fill price  = close × (1 ± slippage)   (worse for us on both sides)
Commission  = max(min_commission, commission_pct × gross), per market (BVC / US / CRYPTO)
Quantity    = whole shares on BVC, fractional on US/crypto (configurable)
FX          = USD↔COP at the market rate with a spread

The executor is stateless: callers pass the cash/holding available; the PositionTracker
applies the resulting Transaction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from poly_bot.execution.models import ExecutionReport, OrderRequest, Transaction
from poly_bot.market_data.models import Asset, Currency, Quote
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class MarketCosts:
    commission_pct: float = 0.001
    min_commission: float = 0.0
    slippage_pct: float = 0.0005
    fractional: bool = True

    def commission(self, gross: float) -> float:
        return max(self.min_commission, gross * self.commission_pct)


DEFAULT_COSTS: dict[str, MarketCosts] = {
    "BVC": MarketCosts(0.003, 10_000.0, 0.002, False),
    "US": MarketCosts(0.001, 1.0, 0.0005, True),
    "CRYPTO": MarketCosts(0.001, 0.0, 0.001, True),
}


class PaperExecutor:
    is_paper: bool = True

    def __init__(self, costs: dict[str, Any] | None = None) -> None:
        costs = dict(costs or {})
        self.fx_spread_pct = float(costs.pop("fx_spread_pct", 0.005))
        withholding = costs.pop("dividend_withholding", None) or {}
        # Dividend withholding tax by market (US→non-resident 30% by default)
        self.dividend_withholding: dict[str, float] = {
            "US": 0.30, "BVC": 0.0, "CRYPTO": 0.0, **withholding
        }
        self._costs = dict(DEFAULT_COSTS)
        for market, cfg in costs.items():
            if isinstance(cfg, dict):
                self._costs[market] = MarketCosts(**cfg)

    def costs_for(self, asset: Asset) -> MarketCosts:
        return self._costs[asset.market]

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def fill_price(self, asset: Asset, quote: Quote, side: str) -> float:
        slip = self.costs_for(asset).slippage_pct
        return quote.price * (1 + slip) if side == "BUY" else quote.price * (1 - slip)

    def max_buy_quantity(self, asset: Asset, quote: Quote, cash: float) -> float:
        """Largest quantity whose gross + commission fits in `cash`."""
        c = self.costs_for(asset)
        price = self.fill_price(asset, quote, "BUY")
        qty = max(0.0, min(cash / (price * (1 + c.commission_pct)), (cash - c.min_commission) / price))
        return self._round_qty(qty, c)

    def execute(
        self,
        request: OrderRequest,
        asset: Asset,
        quote: Quote,
        cash_available: float,
        held_quantity: float = 0.0,
    ) -> ExecutionReport:
        def reject(reason: str) -> ExecutionReport:
            log.info("paper.order_rejected", ticker=request.ticker, side=request.side, reason=reason)
            return ExecutionReport(request=request, status="rejected", rejection_reason=reason)

        if quote.currency != asset.currency:
            return reject(f"Currency mismatch {quote.currency} != {asset.currency}")

        c = self.costs_for(asset)
        price = self.fill_price(asset, quote, request.side)

        if request.quantity is not None:
            qty = request.quantity
        elif request.value is not None:
            qty = request.value / price
        else:
            return reject("Order needs value or quantity")
        qty = self._round_qty(qty, c)

        if request.side == "SELL":
            qty = min(qty, held_quantity)
            # Selling (almost) everything → sell exactly what we hold
            if held_quantity - qty < 1e-9 or (c.fractional and held_quantity - qty < 1e-6):
                qty = held_quantity
        if qty <= 0:
            return reject("Quantity rounds to zero")

        gross = qty * price
        fee = c.commission(gross)

        if request.side == "BUY":
            if gross + fee > cash_available + 1e-9:
                # Shrink to what we can afford instead of rejecting outright
                qty = self.max_buy_quantity(asset, quote, cash_available)
                if qty <= 0:
                    return reject(
                        f"Insufficient {asset.currency} cash: {cash_available:,.2f}"
                    )
                gross = qty * price
                fee = c.commission(gross)
        elif gross <= fee:
            return reject("Sale value does not cover commission")

        tx = Transaction(
            kind=request.side,
            currency=asset.currency,
            ticker=asset.ticker,
            quantity=qty,
            price=price,
            gross=gross,
            fee=fee,
            strategy=request.strategy,
            rationale=request.rationale,
        )
        log.info(
            "paper.filled",
            ticker=asset.ticker,
            side=request.side,
            qty=round(qty, 6),
            price=round(price, 4),
            gross=round(gross, 2),
            fee=round(fee, 2),
            ccy=asset.currency,
        )
        return ExecutionReport(request=request, status="filled", transaction=tx)

    @staticmethod
    def _round_qty(qty: float, c: MarketCosts) -> float:
        if qty <= 0:
            return 0.0
        if c.fractional:
            return math.floor(qty * 1e6) / 1e6
        return float(math.floor(qty + 1e-9))

    # ------------------------------------------------------------------
    # FX
    # ------------------------------------------------------------------

    def convert(
        self,
        from_currency: Currency,
        to_currency: Currency,
        from_amount: float,
        usdcop: float,
        strategy: str = "",
        rationale: str = "",
    ) -> Transaction:
        """Convert `from_amount` of from_currency into to_currency at mid ± spread."""
        if from_currency == to_currency:
            raise ValueError("FX conversion needs two different currencies")
        if from_currency == "COP":
            rate = usdcop * (1 + self.fx_spread_pct)  # pay more COP per USD
            received = from_amount / rate
            mid = from_amount / usdcop
        else:
            rate = usdcop * (1 - self.fx_spread_pct)  # get fewer COP per USD
            received = from_amount * rate
            mid = from_amount * usdcop
        return Transaction(
            kind="FX",
            currency=to_currency,
            from_currency=from_currency,
            from_amount=from_amount,
            gross=received,
            price=rate,
            fee=mid - received,  # informational: spread cost in destination currency
            strategy=strategy,
            rationale=rationale,
        )

    def from_amount_for(
        self, from_currency: Currency, to_amount: float, usdcop: float
    ) -> float:
        """How much from_currency is needed to receive `to_amount` after spread."""
        if from_currency == "COP":
            return to_amount * usdcop * (1 + self.fx_spread_pct)
        return to_amount / (usdcop * (1 - self.fx_spread_pct))
