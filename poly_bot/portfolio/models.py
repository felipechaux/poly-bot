"""Portfolio models — multi-currency (COP / USD)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from poly_bot.execution.models import utcnow
from poly_bot.market_data.models import Currency


def convert(amount: float, from_ccy: str, to_ccy: str, usdcop: float) -> float:
    """Convert at mid rate (valuation only — trades use the executor's spread)."""
    if from_ccy == to_ccy:
        return amount
    if from_ccy == "USD" and to_ccy == "COP":
        return amount * usdcop
    if from_ccy == "COP" and to_ccy == "USD":
        return amount / usdcop
    raise ValueError(f"Unsupported conversion {from_ccy}->{to_ccy}")


class Position(BaseModel):
    ticker: str
    currency: Currency
    quantity: float = 0.0
    avg_cost: float = 0.0  # per share incl. buy commissions, asset currency
    realized_pnl: float = 0.0  # asset currency
    dividends: float = 0.0  # net dividends received, asset currency
    strategy: str = ""
    opened_at: datetime = Field(default_factory=utcnow)
    last_price: float | None = None

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def market_value(self) -> float:
        price = self.last_price if self.last_price is not None else self.avg_cost
        return self.quantity * price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pct(self) -> float:
        return self.unrealized_pnl / self.cost_basis if self.cost_basis > 0 else 0.0


class PortfolioSnapshot(BaseModel):
    cash: dict[str, float]
    positions: list[Position] = Field(default_factory=list)
    usdcop: float
    base_currency: Currency = "COP"
    realized_pnl: dict[str, float] = Field(default_factory=dict)  # by currency
    fees: dict[str, float] = Field(default_factory=dict)
    dividends: dict[str, float] = Field(default_factory=dict)
    deposits: dict[str, float] = Field(default_factory=dict)
    trade_count: int = 0
    taken_at: datetime = Field(default_factory=utcnow)

    def to(self, amount: float, from_ccy: str, to_ccy: str | None = None) -> float:
        return convert(amount, from_ccy, to_ccy or self.base_currency, self.usdcop)

    def _sum(self, by_ccy: dict[str, float], ccy: str | None) -> float:
        return sum(self.to(v, k, ccy) for k, v in by_ccy.items())

    def cash_value(self, ccy: str | None = None) -> float:
        return self._sum(self.cash, ccy)

    def positions_value(self, ccy: str | None = None) -> float:
        return sum(self.to(p.market_value, p.currency, ccy) for p in self.positions)

    def total_value(self, ccy: str | None = None) -> float:
        return self.cash_value(ccy) + self.positions_value(ccy)

    def unrealized_pnl(self, ccy: str | None = None) -> float:
        return sum(self.to(p.unrealized_pnl, p.currency, ccy) for p in self.positions)

    def realized(self, ccy: str | None = None) -> float:
        return self._sum(self.realized_pnl, ccy)

    def total_fees(self, ccy: str | None = None) -> float:
        return self._sum(self.fees, ccy)

    def total_dividends(self, ccy: str | None = None) -> float:
        return self._sum(self.dividends, ccy)

    def invested(self, ccy: str | None = None) -> float:
        """Deposits valued at today's rate (so FX moves count as return)."""
        return self._sum(self.deposits, ccy)

    def value_of(self, ticker: str, ccy: str | None = None) -> float:
        p = self.get_position(ticker)
        return self.to(p.market_value, p.currency, ccy) if p else 0.0

    def weight(self, ticker: str) -> float:
        total = self.total_value()
        return self.value_of(ticker) / total if total > 0 else 0.0

    def get_position(self, ticker: str) -> Position | None:
        for p in self.positions:
            if p.ticker == ticker:
                return p
        return None
