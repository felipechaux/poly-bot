"""Unit tests for the risk manager."""

from __future__ import annotations

import pytest
from poly_bot.execution.models import OrderRequest
from poly_bot.portfolio.models import PortfolioSnapshot
from poly_bot.risk.manager import RiskManager


def make_request(**kwargs):
    defaults = dict(token_id="tok1", side="BUY", price=0.50, size_usdc=100.0, strategy="test")
    defaults.update(kwargs)
    return OrderRequest(**defaults)


def make_portfolio(**kwargs):
    defaults = dict(cash_usdc=10_000.0)
    defaults.update(kwargs)
    return PortfolioSnapshot(**defaults)


def test_approve_normal_order():
    risk = RiskManager()
    req = make_request()
    snap = make_portfolio()
    approved, reason = risk.check(req, snap)
    assert approved is True
    assert reason == ""


def test_reject_price_too_low():
    risk = RiskManager(min_price=0.02)
    req = make_request(price=0.01)
    approved, reason = risk.check(req, make_portfolio())
    assert not approved
    assert "minimum" in reason


def test_reject_price_too_high():
    risk = RiskManager(max_price=0.98)
    req = make_request(price=0.99)
    approved, reason = risk.check(req, make_portfolio())
    assert not approved
    assert "maximum" in reason


def test_reject_oversized_order():
    risk = RiskManager(max_position_size_usdc=200.0)
    req = make_request(size_usdc=500.0)
    approved, reason = risk.check(req, make_portfolio())
    assert not approved
    assert "exceeds max" in reason


def test_reject_insufficient_cash():
    risk = RiskManager()
    req = make_request(size_usdc=500.0)
    snap = make_portfolio(cash_usdc=100.0)
    approved, reason = risk.check(req, snap)
    assert not approved
    assert "cash" in reason.lower()


def test_debounce_duplicate():
    risk = RiskManager(order_debounce_seconds=60.0)
    req = make_request()
    snap = make_portfolio()
    # First submission OK
    ok, _ = risk.check(req, snap)
    assert ok
    # Second within debounce window should be rejected
    ok2, reason = risk.check(req, snap)
    assert not ok2
    assert "debounce" in reason.lower()
