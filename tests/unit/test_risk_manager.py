"""Risk manager: whitelist, stale prices, weight limits, no shorts, min size."""

from __future__ import annotations

from datetime import timedelta

from poly_bot.execution.models import OrderRequest, Transaction
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.risk.manager import RiskManager
from tests.conftest import TODAY, USDCOP, make_quotes


def snap(buys: list[tuple[str, str, float, float]] = ()):  # type: ignore[assignment]
    t = PositionTracker()
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=10_000.0))
    for ticker, ccy, qty, price in buys:
        t.apply(Transaction(kind="BUY", currency=ccy, ticker=ticker, quantity=qty, price=price,
                            gross=qty * price))
    t.update_prices(make_quotes(), USDCOP)
    return t.snapshot()


def rm(universe, **kw) -> RiskManager:
    return RiskManager(universe, max_weight_per_asset=0.5,
                       max_weight_per_class={"crypto": 0.10}, max_weight_per_region={"CO": 0.4},
                       min_order_value_usd=25.0, max_stale_days=5, **kw)


def test_rejects_ticker_outside_universe(universe, quotes):
    ok, reason = rm(universe).check(OrderRequest(ticker="GME", side="BUY", value=100), None, snap(), TODAY)
    assert not ok and "universo" in reason


def test_rejects_stale_price(universe):
    q = make_quotes(as_of=TODAY - timedelta(days=10))["VT"]
    ok, reason = rm(universe).check(OrderRequest(ticker="VT", side="BUY", value=100), q, snap(), TODAY)
    assert not ok and "desactualizado" in reason


def test_asset_weight_limit(universe, quotes):
    ok, _ = rm(universe).check(OrderRequest(ticker="VT", side="BUY", value=4_000), quotes["VT"], snap(), TODAY)
    assert ok
    ok, reason = rm(universe).check(OrderRequest(ticker="VT", side="BUY", value=6_000), quotes["VT"], snap(), TODAY)
    assert not ok and "Peso" in reason


def test_class_limit_crypto(universe, quotes):
    s = snap([("BTC-USD", "USD", 0.018, 50_000.0)])  # ~9% already
    ok, reason = rm(universe).check(OrderRequest(ticker="BTC-USD", side="BUY", value=200), quotes["BTC-USD"], s, TODAY)
    assert not ok and "crypto" in reason


def test_region_limit_colombia(universe, quotes):
    # 4,500 USD of 10,000 = 45% in CO → any CO buy rejected
    t = PositionTracker()
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=5_500.0))
    t.apply(Transaction(kind="DEPOSIT", currency="COP", gross=4_500 * USDCOP))
    t.apply(Transaction(kind="BUY", currency="COP", ticker="ICOLCAP.CL", quantity=900, price=20_000.0,
                        gross=18_000_000.0))
    t.update_prices(make_quotes(), USDCOP)
    s = t.snapshot()
    ok, reason = rm(universe).check(OrderRequest(ticker="ECOPETROL.CL", side="BUY", value=500_000),
                                    quotes["ECOPETROL.CL"], s, TODAY)
    assert not ok and "CO" in reason


def test_no_short_and_min_size(universe, quotes):
    ok, reason = rm(universe).check(OrderRequest(ticker="NU", side="SELL", quantity=1), quotes["NU"], snap(), TODAY)
    assert not ok and "cortos" in reason
    ok, reason = rm(universe).check(OrderRequest(ticker="NU", side="BUY", value=10), quotes["NU"], snap(), TODAY)
    assert not ok and "pequeña" in reason
