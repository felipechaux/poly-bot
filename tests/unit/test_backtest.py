"""Backtester on synthetic history: same strategy, costs and next-open fills as live."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from poly_bot.analytics.backtest import History, load_result, metrics_from_curve, run_backtest, save_result
from tests.integration.test_bot_cycle import settings

DAYS = pd.bdate_range("2023-01-02", "2025-12-31")
PRICES = {"VT": 100.0, "ICOLCAP.CL": 20_000.0, "BTC-USD": 30_000.0}
GROWTH = {"VT": 0.10, "ICOLCAP.CL": 0.00, "BTC-USD": 0.40}  # yearly drift


def frame(t: str) -> pd.DataFrame:
    rng = np.random.default_rng(len(t))
    n = len(DAYS)
    drift = (1 + GROWTH[t]) ** (1 / 252) - 1
    close = PRICES[t] * np.cumprod(1 + drift + rng.normal(0, 0.01, n))
    opens = np.r_[close[0], close[:-1]] * 1.001  # open = previous close + a small gap
    div = np.zeros(n)
    if t == "VT":
        div[::63] = 0.5  # quarterly dividend
    return pd.DataFrame({"Open": opens, "Close": close, "Adj Close": close, "Dividends": div},
                        index=DAYS)


def history() -> History:
    return History(frames={t: frame(t) for t in PRICES},
                   usdcop=pd.Series(4000.0, index=DAYS))


def bt_settings(tmp_path):
    s = settings(tmp_path)
    s.execution = {"fill": "next_open", "crypto_fill": "orderbook"}  # live config
    return s


async def test_backtest_runs_with_live_costs_and_no_live_data(tmp_path):
    s = bt_settings(tmp_path)
    res = await run_backtest(s, years=2, history=history())
    core = res.metrics["core"]
    assert core["start"] >= "2023-12" and core["end"] == "2025-12-31"
    assert res.trades >= 3 and res.fees_base > 0  # initial allocation + rebalances, with commissions
    assert set(res.metrics) >= {"core", "core_usd", "VT", "ICOLCAP.CL"}
    assert -1 < core["max_drawdown"] <= 0 and core["vol"] > 0
    assert {r["ticker"] for r in res.contribution} >= {"VT", "ICOLCAP.CL", "BTC-USD"}
    assert res.curve and {"day", "core", "VT"} <= set(res.curve[0])
    assert [y["year"] for y in res.yearly][-1] == 2025
    # BTC grew 40%/yr vs COLCAP flat → BTC contributes more per peso invested
    by = {r["ticker"]: r["pnl_cop"] for r in res.contribution}
    assert by["BTC-USD"] / 0.1 > by["ICOLCAP.CL"] / 0.3


async def test_backtest_never_touches_the_live_order_book(tmp_path, monkeypatch):
    from poly_bot.market_data import orderbook

    async def boom(*a, **k):
        raise AssertionError("backtest used the live order book")

    monkeypatch.setattr(orderbook.OrderBook, "fill", boom)
    await run_backtest(bt_settings(tmp_path), years=1, history=history())


def test_metrics_and_save_roundtrip(tmp_path):
    v = pd.Series([100.0, 110.0, 99.0, 121.0], index=pd.to_datetime(
        ["2024-01-01", "2024-07-01", "2025-01-01", "2026-01-01"]))
    m = metrics_from_curve(v)
    assert m["total_return"] == pytest.approx(0.21) and m["max_drawdown"] == pytest.approx(-0.1)
    assert m["max_dd_peak"] == "2024-07-01" and m["max_dd_trough"] == "2025-01-01"
    assert load_result(tmp_path / "none.json") is None


async def test_result_is_saved_for_the_dashboard(tmp_path):
    res = await run_backtest(bt_settings(tmp_path), years=1, history=history())
    path = tmp_path / "bt.json"
    save_result(res, path)
    assert load_result(path)["metrics"]["core"]["cagr"] == pytest.approx(res.metrics["core"]["cagr"])


async def test_api_serves_the_saved_backtest(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import poly_bot.analytics.backtest as bt
    from poly_bot.bot import Bot
    from poly_bot.web.api import create_app
    from tests.conftest import FakeProvider

    path = tmp_path / "bt.json"
    monkeypatch.setattr(bt, "RESULT_PATH", path)
    monkeypatch.setattr(bt.load_result, "__defaults__", (path,))
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    with TestClient(create_app(bot)) as client:
        assert client.get("/api/backtest").json() == {"ready": False}
        save_result(await run_backtest(bt_settings(tmp_path), years=1, history=history()), path)
        d = client.get("/api/backtest").json()
        assert "Backtest de la core" in client.get("/").text
    assert d["ready"] and d["metrics"]["core"]["cagr"] is not None and d["curve"]
    await bot._shutdown()


def _first_day(h: History, years: int) -> pd.Timestamp:
    end = h.frames["VT"].index[-1]
    return next(d for d in DAYS if d >= end - pd.DateOffset(years=years))


async def test_no_dividend_for_shares_bought_on_the_ex_date(tmp_path):
    base, ex = history(), history()
    for h in (base, ex):
        h.frames["VT"]["Dividends"] = 0.0
    ex.frames["VT"].loc[_first_day(ex, 1), "Dividends"] = 5.0  # the day of the initial buy
    vt = lambda r: {c["ticker"]: c["pnl_cop"] for c in r.contribution}["VT"]  # noqa: E731
    r0 = await run_backtest(bt_settings(tmp_path), years=1, history=base)
    r1 = await run_backtest(bt_settings(tmp_path), years=1, history=ex)
    assert vt(r1) == pytest.approx(vt(r0))


async def test_dividend_goes_to_shares_held_at_the_previous_close(tmp_path):
    base, ex = history(), history()
    for h in (base, ex):
        h.frames["VT"]["Dividends"] = 0.0
    later = DAYS[DAYS.get_loc(_first_day(ex, 1)) + 10]
    ex.frames["VT"].loc[later, "Dividends"] = 5.0
    vt = lambda r: {c["ticker"]: c["pnl_cop"] for c in r.contribution}["VT"]  # noqa: E731
    r0 = await run_backtest(bt_settings(tmp_path), years=1, history=base)
    r1 = await run_backtest(bt_settings(tmp_path), years=1, history=ex)
    assert vt(r1) > vt(r0)  # credited (net of 30% US withholding)


async def test_start_capital_uses_the_starting_fx_rate(tmp_path):
    h = history()
    h.usdcop = pd.Series(np.linspace(4000, 6000, len(DAYS)), index=DAYS)
    res = await run_backtest(bt_settings(tmp_path), years=1, history=h)
    fx0 = float(h.usdcop[h.usdcop.index < _first_day(h, 1)].iloc[-1])
    assert res.params["start_usdcop"] == pytest.approx(fx0)
    assert res.params["start_capital_cop"] == pytest.approx(20_000_000 + 5_000 * fx0)
