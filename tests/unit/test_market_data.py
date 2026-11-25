"""Price cleaning and indicators."""

from __future__ import annotations

import pandas as pd
import pytest

from poly_bot.market_data.indicators import compute_indicators
from poly_bot.market_data.provider import clean_history


def test_clean_drops_bvc_stale_and_holiday_rows():
    idx = pd.date_range("2026-11-10", periods=5)
    df = pd.DataFrame({
        "Close":     [100.0, 101.0, 101.0, 101.0, 102.0],
        "Volume":    [10,    20,    20,    0,     30],
        "Dividends": [0.0,   0.0,   0.0,   0.0,   0.0],
    }, index=idx)
    out = clean_history(df, "BVC")
    # row 3 = duplicate (same close+volume), row 4 = holiday (same close, 0 volume)
    assert list(out["Close"]) == [100.0, 101.0, 102.0]
    # US data is left alone
    assert len(clean_history(df, "US")) == 5


def test_clean_keeps_flat_row_with_dividend():
    idx = pd.date_range("2026-11-10", periods=2)
    df = pd.DataFrame({"Close": [100.0, 100.0], "Volume": [0, 0], "Dividends": [0.0, 5.0]}, index=idx)
    assert len(clean_history(df, "BVC")) == 2


def test_indicators_uptrend():
    close = pd.Series([100 * 1.001 ** i for i in range(300)],
                      index=pd.date_range("2025-01-01", periods=300))
    i = compute_indicators(close)
    assert i.ret_1d == pytest.approx(0.001)
    assert i.sma50 is not None and i.sma200 is not None and i.sma50 > i.sma200
    assert i.ret_1y == pytest.approx(1.001 ** 252 - 1)


def test_indicators_short_history():
    i = compute_indicators(pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2)))
    assert i.sma200 is None and i.ret_1m is None and i.ret_1d == pytest.approx(1.0)
