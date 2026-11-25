"""Market data models: assets in the universe, quotes and trend indicators."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Market = Literal["BVC", "US", "CRYPTO"]
Currency = Literal["COP", "USD"]

MARKET_CURRENCY: dict[str, Currency] = {"BVC": "COP", "US": "USD", "CRYPTO": "USD"}


class Asset(BaseModel):
    ticker: str
    name: str = ""
    market: Market = "US"
    asset_class: str = "equity"  # etf | equity | bond | commodity | crypto
    region: str = "US"
    benchmark: bool = False

    @property
    def currency(self) -> Currency:
        return MARKET_CURRENCY[self.market]


class Indicators(BaseModel):
    """Trend/volatility indicators computed from daily closes."""

    ret_1d: float | None = None
    ret_1m: float | None = None
    ret_3m: float | None = None
    ret_1y: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    vol_annual: float | None = None


class Quote(BaseModel):
    ticker: str
    price: float  # last daily close, in the asset's currency
    currency: Currency
    as_of: date
    indicators: Indicators = Indicators()

    @property
    def trend(self) -> Literal["alcista", "bajista", "mixta", "sin datos"]:
        i = self.indicators
        if i.sma50 is None or i.sma200 is None:
            return "sin datos"
        above = self.price > i.sma200
        golden = i.sma50 > i.sma200
        if above and golden:
            return "alcista"
        if not above and not golden:
            return "bajista"
        return "mixta"


class Dividend(BaseModel):
    ticker: str
    ex_date: date
    amount_per_share: float
    currency: Currency
