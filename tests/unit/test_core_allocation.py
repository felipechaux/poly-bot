"""Core allocation: initial buy, monthly/drift triggers, sell-first ordering."""

from __future__ import annotations

from datetime import date

import pytest

from poly_bot.execution.models import Transaction
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.strategies.base import DailyContext
from poly_bot.strategies.core_allocation import CoreAllocationStrategy
from tests.conftest import TODAY, USDCOP, make_quotes

TARGETS = {"VT": 0.6, "ICOLCAP.CL": 0.3, "BTC-USD": 0.1}


def strat(**params) -> CoreAllocationStrategy:
    return CoreAllocationStrategy({"params": {"targets": TARGETS, "drift_band": 0.05,
                                              "cash_buffer_pct": 0.0, **params}})


def ctx(tracker, universe, last=None, prices=None, today=TODAY):
    q = make_quotes(prices)
    tracker.update_prices(q, USDCOP)
    return DailyContext(today=today, quotes=q, usdcop=USDCOP, portfolio=tracker.snapshot(),
                        universe=universe, last_signal_date=last)


def cash_tracker() -> PositionTracker:
    t = PositionTracker("COP")
    t.apply(Transaction(kind="DEPOSIT", currency="COP", gross=20_000_000.0))
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=5_000.0))
    return t  # total 40M COP


def test_targets_must_sum_to_one():
    with pytest.raises(ValueError):
        CoreAllocationStrategy({"params": {"targets": {"VT": 0.5}}})


async def test_initial_allocation_values(universe):
    sigs = await strat().on_daily(ctx(cash_tracker(), universe))
    by = {s.ticker: s for s in sigs}
    assert all(s.side == "BUY" for s in sigs)
    assert by["VT"].value == pytest.approx(0.6 * 40_000_000 / USDCOP)  # USD
    assert by["ICOLCAP.CL"].value == pytest.approx(0.3 * 40_000_000)  # COP
    assert sigs[0].ticker == "VT"  # largest first


async def test_no_rebalance_same_month_within_band(universe):
    t = cash_tracker()
    t.apply(Transaction(kind="BUY", currency="USD", ticker="VT", quantity=50, price=100, gross=5_000))
    t.apply(Transaction(kind="FX", currency="USD", from_currency="COP", from_amount=5_600_000, gross=1_400))
    t.apply(Transaction(kind="BUY", currency="USD", ticker="VT", quantity=10, price=100, gross=1_000))
    t.apply(Transaction(kind="BUY", currency="COP", ticker="ICOLCAP.CL", quantity=600, price=20_000, gross=12_000_000))
    t.apply(Transaction(kind="BUY", currency="USD", ticker="BTC-USD", quantity=0.02, price=50_000, gross=1_000))
    c = ctx(t, universe, last=date(2026, 11, 2))
    assert strat().rebalance_reason(c) is None
    assert await strat().on_daily(c) == []


async def test_monthly_rebalance_triggers(universe):
    c = ctx(cash_tracker(), universe, last=date(2026, 10, 30))
    assert strat().rebalance_reason(c) == "rebalanceo mensual"


async def test_drift_triggers_sell_then_buy(universe):
    t = cash_tracker()
    # All USD into BTC → BTC way overweight
    t.apply(Transaction(kind="BUY", currency="USD", ticker="BTC-USD", quantity=0.1, price=50_000, gross=5_000))
    sigs = await strat().on_daily(ctx(t, universe, last=date(2026, 11, 20)))
    assert sigs[0].side == "SELL" and sigs[0].ticker == "BTC-USD"
    # sell down to 10% of 40M COP = 4M COP = 1000 USD = 0.02 BTC → sell 0.08
    assert sigs[0].quantity == pytest.approx(0.08)
    assert {s.ticker for s in sigs if s.side == "BUY"} == {"VT", "ICOLCAP.CL"}


async def test_sells_dropped_targets(universe):
    t = cash_tracker()
    t.apply(Transaction(kind="BUY", currency="USD", ticker="NU", quantity=10, price=10, gross=100,
                        strategy="core_allocation"))
    sigs = await strat().on_daily(ctx(t, universe, last=date(2026, 10, 1)))
    nu = [s for s in sigs if s.ticker == "NU"]
    assert nu and nu[0].side == "SELL" and nu[0].quantity == 10
