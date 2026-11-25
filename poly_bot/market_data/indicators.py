"""Pure functions over daily close series (pandas)."""

from __future__ import annotations

import math

import pandas as pd

from poly_bot.market_data.models import Indicators

TRADING_DAYS = {"1m": 21, "3m": 63, "1y": 252}


def _ret(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    prev = float(close.iloc[-n - 1])
    return float(close.iloc[-1]) / prev - 1.0 if prev > 0 else None


def compute_indicators(close: pd.Series, periods_per_year: int = 252) -> Indicators:
    close = close.dropna()
    if close.empty:
        return Indicators()
    daily = close.pct_change().dropna().tail(60)
    vol = float(daily.std() * math.sqrt(periods_per_year)) if len(daily) >= 20 else None
    return Indicators(
        ret_1d=_ret(close, 1),
        ret_1m=_ret(close, TRADING_DAYS["1m"]),
        ret_3m=_ret(close, TRADING_DAYS["3m"]),
        ret_1y=_ret(close, TRADING_DAYS["1y"]),
        sma50=float(close.tail(50).mean()) if len(close) >= 50 else None,
        sma200=float(close.tail(200).mean()) if len(close) >= 200 else None,
        vol_annual=vol,
    )
