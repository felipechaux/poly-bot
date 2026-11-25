"""
Price provider — daily closes, dividends and USD/COP from Yahoo Finance (yfinance).

yfinance is synchronous, so calls run in a worker thread. Data is cleaned before use:
BVC series contain stale duplicate rows (same close + volume as the previous day) and
holiday rows with zero volume and an unchanged close; both are dropped.

Data licence: Yahoo Finance data is for personal, non-commercial use.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import pandas as pd

from poly_bot.market_data.indicators import compute_indicators
from poly_bot.market_data.models import Asset, Dividend, Quote
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)


@dataclass
class MarketData:
    """Everything a daily cycle needs from the market."""

    quotes: dict[str, Quote]
    dividends: dict[str, list[Dividend]]
    usdcop: float
    usdcop_as_of: date
    closes: dict[str, pd.Series]
    opens: dict[str, pd.Series] = field(default_factory=dict)  # daily opens, for next-open fills


class PriceProvider(Protocol):
    async def fetch(self, assets: list[Asset]) -> MarketData: ...


def clean_history(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """Drop NaN closes and, for BVC, stale duplicate / holiday rows."""
    if df.empty or "Close" not in df:
        return df
    df = df[df["Close"].notna() & (df["Close"] > 0)].copy()
    if market != "BVC" or df.empty:
        return df
    vol = df["Volume"].fillna(0) if "Volume" in df else pd.Series(0, index=df.index)
    same_close = df["Close"].diff() == 0
    stale = same_close & ((vol == 0) | (vol.diff() == 0))
    # Never drop rows that carry a dividend
    if "Dividends" in df:
        stale &= df["Dividends"].fillna(0) == 0
    return df[~stale]


def _periods_per_year(asset: Asset) -> int:
    return 365 if asset.market == "CRYPTO" else 252


class YFinanceProvider:
    def __init__(
        self,
        fx_ticker: str = "COP=X",
        history_period: str = "2y",
        cache_ttl_minutes: float = 30.0,
    ) -> None:
        self._fx_ticker = fx_ticker
        self._period = history_period
        self._ttl = cache_ttl_minutes * 60
        self._cache: tuple[float, frozenset[str], MarketData] | None = None
        self._lock = asyncio.Lock()

    async def fetch(self, assets: list[Asset]) -> MarketData:
        key = frozenset(a.ticker for a in assets)
        async with self._lock:
            if self._cache and self._cache[1] == key and time.time() - self._cache[0] < self._ttl:
                return self._cache[2]
            data = await asyncio.to_thread(self._fetch_sync, assets)
            self._cache = (time.time(), key, data)
            return data

    def invalidate(self) -> None:
        self._cache = None

    # ------------------------------------------------------------------

    def _download(self, tickers: list[str]) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        raw = yf.download(
            tickers,
            period=self._period,
            interval="1d",
            auto_adjust=False,
            actions=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        out: dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.dropna(how="all")
            if not df.empty and df["Close"].notna().any():
                out[t] = df
        return out

    def _fetch_sync(self, assets: list[Asset]) -> MarketData:
        tickers = [a.ticker for a in assets] + [self._fx_ticker]
        frames = self._download(tickers)
        missing = [t for t in tickers if t not in frames]
        if missing:  # yfinance occasionally times out on a few tickers — retry once
            log.warning("prices.retry_missing", tickers=missing)
            frames.update(self._download(missing))
            still = [t for t in tickers if t not in frames]
            if still:
                log.warning("prices.missing", tickers=still)

        fx_df = frames.get(self._fx_ticker)
        if fx_df is None or fx_df["Close"].dropna().empty:
            raise RuntimeError(f"No FX data for {self._fx_ticker}; cannot value portfolio")
        fx_close = fx_df["Close"].dropna()

        quotes: dict[str, Quote] = {}
        dividends: dict[str, list[Dividend]] = {}
        closes: dict[str, pd.Series] = {}
        opens: dict[str, pd.Series] = {}
        for asset in assets:
            df = frames.get(asset.ticker)
            if df is None:
                continue
            df = clean_history(df, asset.market)
            if df.empty:
                continue
            close = df["Close"].astype(float)
            closes[asset.ticker] = close
            if "Open" in df:
                op = df["Open"].astype(float)
                opens[asset.ticker] = op[op > 0].dropna()
            quotes[asset.ticker] = Quote(
                ticker=asset.ticker,
                price=float(close.iloc[-1]),
                currency=asset.currency,
                as_of=close.index[-1].date(),
                indicators=compute_indicators(close, _periods_per_year(asset)),
            )
            if "Dividends" in df:
                divs = df["Dividends"].fillna(0)
                dividends[asset.ticker] = [
                    Dividend(
                        ticker=asset.ticker,
                        ex_date=idx.date(),
                        amount_per_share=float(v),
                        currency=asset.currency,
                    )
                    for idx, v in divs[divs > 0].items()
                ]

        log.info("prices.fetched", quotes=len(quotes), requested=len(assets))
        return MarketData(
            quotes=quotes,
            dividends=dividends,
            usdcop=float(fx_close.iloc[-1]),
            usdcop_as_of=fx_close.index[-1].date(),
            closes=closes,
            opens=opens,
        )
