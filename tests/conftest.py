"""Shared test fixtures: a small universe, quotes and a fake price provider."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from poly_bot.market_data.models import Asset, Dividend, Indicators, Quote
from poly_bot.market_data.provider import MarketData
from poly_bot.market_data.universe import Universe

TODAY = date(2026, 11, 24)
USDCOP = 4000.0

ASSETS = [
    Asset(ticker="VT", name="World", market="US", asset_class="etf", region="GLOBAL", benchmark=True),
    Asset(ticker="ICOLCAP.CL", name="COLCAP", market="BVC", asset_class="etf", region="CO", benchmark=True),
    Asset(ticker="ECOPETROL.CL", name="Ecopetrol", market="BVC", asset_class="equity", region="CO"),
    Asset(ticker="BTC-USD", name="Bitcoin", market="CRYPTO", asset_class="crypto", region="GLOBAL"),
    Asset(ticker="NU", name="Nubank", market="US", asset_class="equity", region="LATAM"),
]
PRICES = {"VT": 100.0, "ICOLCAP.CL": 20_000.0, "ECOPETROL.CL": 2_000.0, "BTC-USD": 50_000.0, "NU": 10.0}


def make_quotes(prices: dict[str, float] | None = None, as_of: date = TODAY) -> dict[str, Quote]:
    prices = prices or PRICES
    ccy = {a.ticker: a.currency for a in ASSETS}
    return {
        t: Quote(ticker=t, price=p, currency=ccy[t], as_of=as_of,
                 indicators=Indicators(ret_1d=0.01, ret_3m=0.05, sma50=p, sma200=p * 0.9))
        for t, p in prices.items()
    }


@pytest.fixture
def universe() -> Universe:
    return Universe(ASSETS)


@pytest.fixture
def quotes() -> dict[str, Quote]:
    return make_quotes()


class FakeProvider:
    def __init__(self, prices: dict[str, float] | None = None, usdcop: float = USDCOP,
                 dividends: dict[str, list[Dividend]] | None = None, as_of: date = TODAY) -> None:
        self.prices = dict(prices or PRICES)
        self.usdcop = usdcop
        self.dividends = dividends or {}
        self.as_of = as_of

    async def fetch(self, assets: list[Asset]) -> MarketData:
        quotes = make_quotes(self.prices, self.as_of)
        idx = pd.date_range(self.as_of - timedelta(days=2), periods=3)
        return MarketData(
            quotes={t: q for t, q in quotes.items() if t in {a.ticker for a in assets}},
            dividends=self.dividends, usdcop=self.usdcop, usdcop_as_of=self.as_of,
            closes={t: pd.Series([p] * 3, index=idx) for t, p in self.prices.items()},
        )
