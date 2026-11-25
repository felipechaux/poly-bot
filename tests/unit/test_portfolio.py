"""Position tracker: multi-currency valuation, realized P&L, replay."""

from __future__ import annotations

import pytest

from poly_bot.execution.models import Transaction
from poly_bot.portfolio.position_tracker import PositionTracker
from tests.conftest import USDCOP, make_quotes


def seeded() -> PositionTracker:
    t = PositionTracker("COP")
    t.apply(Transaction(kind="DEPOSIT", currency="COP", gross=10_000_000.0))
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=5_000.0))
    return t


def test_valuation_in_cop_and_usd():
    t = seeded()
    t.update_prices(make_quotes(), USDCOP)
    s = t.snapshot()
    assert s.total_value("COP") == pytest.approx(10_000_000 + 5_000 * USDCOP)
    assert s.total_value("USD") == pytest.approx(10_000_000 / USDCOP + 5_000)


def test_buy_sell_realized_pnl_and_fees():
    t = seeded()
    t.apply(Transaction(kind="BUY", currency="USD", ticker="VT", quantity=10, price=100, gross=1_000, fee=1))
    assert t.cash["USD"] == pytest.approx(3_999)
    pos = t.get_position("VT")
    assert pos is not None and pos.avg_cost == pytest.approx(100.1)
    sell = Transaction(kind="SELL", currency="USD", ticker="VT", quantity=10, price=110, gross=1_100, fee=1)
    t.apply(sell)
    assert sell.realized_pnl == pytest.approx(1_100 - 1 - 1_001)
    assert t.get_position("VT") is None
    t.update_prices(make_quotes(), USDCOP)
    assert t.snapshot().total_fees("USD") == pytest.approx(2.0)


def test_sell_more_than_held_raises():
    t = seeded()
    with pytest.raises(ValueError):
        t.apply(Transaction(kind="SELL", currency="USD", ticker="VT", quantity=1, price=1, gross=1))


def test_replay_reproduces_state():
    txs = [
        Transaction(kind="DEPOSIT", currency="COP", gross=1_000_000.0),
        Transaction(kind="FX", currency="USD", from_currency="COP", from_amount=402_000.0, gross=100.0),
        Transaction(kind="BUY", currency="USD", ticker="NU", quantity=9, price=10, gross=90, fee=1),
        Transaction(kind="DIVIDEND", currency="USD", ticker="NU", quantity=9, price=0.5, gross=4.5, fee=1.35),
    ]
    a = PositionTracker()
    for tx in txs:
        a.apply(tx)
    b = PositionTracker()
    b.replay(txs)
    assert dict(a.cash) == pytest.approx(dict(b.cash))
    assert b.cash["USD"] == pytest.approx(100 - 91 + 3.15)
    assert b.cash["COP"] == pytest.approx(598_000)
    pos = b.get_position("NU")
    assert pos is not None and pos.dividends == pytest.approx(3.15)
