"""Paper executor: slippage, commissions, whole shares on BVC, FX spread."""

from __future__ import annotations

import pytest

from poly_bot.execution.models import OrderRequest
from poly_bot.execution.paper_executor import PaperExecutor

COSTS = {
    "BVC": {"commission_pct": 0.003, "min_commission": 10_000.0, "slippage_pct": 0.002, "fractional": False},
    "US": {"commission_pct": 0.001, "min_commission": 1.0, "slippage_pct": 0.0, "fractional": True},
    "fx_spread_pct": 0.005,
}


def ex() -> PaperExecutor:
    return PaperExecutor(dict(COSTS))


def test_us_buy_fractional_with_commission(universe, quotes):
    r = ex().execute(OrderRequest(ticker="VT", side="BUY", value=1_000.0), universe.require("VT"),
                     quotes["VT"], cash_available=5_000.0)
    tx = r.transaction
    assert r.is_filled and tx is not None
    assert tx.quantity == pytest.approx(10.0)
    assert tx.fee == pytest.approx(1.0)  # max(1.0, 0.1% of 1000)
    assert tx.cash_delta == {"USD": pytest.approx(-1_001.0)}


def test_bvc_buy_whole_shares_and_min_commission(universe, quotes):
    r = ex().execute(OrderRequest(ticker="ICOLCAP.CL", side="BUY", value=1_000_000.0),
                     universe.require("ICOLCAP.CL"), quotes["ICOLCAP.CL"], cash_available=5_000_000.0)
    tx = r.transaction
    assert tx is not None and tx.currency == "COP"
    assert tx.quantity == 49  # 1e6 / (20000*1.002) = 49.9 → 49 whole shares
    assert tx.price == pytest.approx(20_040.0)
    assert tx.fee == pytest.approx(10_000.0)  # 0.3% of ~982k < min


def test_buy_shrinks_to_available_cash(universe, quotes):
    r = ex().execute(OrderRequest(ticker="VT", side="BUY", value=1_000.0), universe.require("VT"),
                     quotes["VT"], cash_available=500.0)
    tx = r.transaction
    assert tx is not None
    assert tx.gross + tx.fee <= 500.0 + 1e-9
    assert tx.quantity > 4.9


def test_buy_rejected_without_cash(universe, quotes):
    r = ex().execute(OrderRequest(ticker="ICOLCAP.CL", side="BUY", value=100_000.0),
                     universe.require("ICOLCAP.CL"), quotes["ICOLCAP.CL"], cash_available=15_000.0)
    assert r.status == "rejected"


def test_sell_capped_at_holding(universe, quotes):
    r = ex().execute(OrderRequest(ticker="VT", side="SELL", quantity=50.0), universe.require("VT"),
                     quotes["VT"], cash_available=0.0, held_quantity=3.0)
    assert r.transaction is not None and r.transaction.quantity == 3.0


def test_fx_spread_both_ways():
    e = ex()
    to_usd = e.convert("COP", "USD", 4_020_000.0, usdcop=4_000.0)
    assert to_usd.gross == pytest.approx(1_000.0)  # 4,020,000 / (4000*1.005)
    to_cop = e.convert("USD", "COP", 1_000.0, usdcop=4_000.0)
    assert to_cop.gross == pytest.approx(3_980_000.0)
    assert e.from_amount_for("COP", 1_000.0, 4_000.0) == pytest.approx(4_020_000.0)
