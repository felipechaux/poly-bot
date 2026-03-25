"""Unit tests for order book analysis functions."""

from __future__ import annotations

import pytest
from poly_bot.market_data import order_book as ob
from poly_bot.market_data.models import OrderBookSummary, PriceLevel


def make_book(bids: list[tuple], asks: list[tuple]) -> OrderBookSummary:
    return OrderBookSummary(
        market="test",
        asset_id="test_token",
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


def test_best_bid_ask():
    book = make_book([(0.54, 100), (0.53, 200)], [(0.56, 100), (0.57, 200)])
    assert book.best_bid == 0.54
    assert book.best_ask == 0.56


def test_mid_price():
    book = make_book([(0.54, 100)], [(0.56, 100)])
    assert book.mid_price == pytest.approx(0.55)


def test_spread():
    book = make_book([(0.54, 100)], [(0.56, 100)])
    assert book.spread == pytest.approx(0.02)


def test_book_imbalance_balanced():
    book = make_book([(0.54, 100)], [(0.56, 100)])
    imbalance = ob.book_imbalance(book)
    assert imbalance == pytest.approx(0.0, abs=0.01)


def test_book_imbalance_bid_heavy():
    book = make_book([(0.54, 1000)], [(0.56, 100)])
    imbalance = ob.book_imbalance(book)
    assert imbalance > 0.8  # strong bid pressure


def test_estimate_fill_price_buy():
    book = make_book([], [(0.56, 100), (0.57, 100)])
    avg_price, slippage = ob.estimate_fill_price(book, "BUY", size_usdc=100.0)
    assert avg_price == pytest.approx(0.56, rel=0.01)
    assert slippage >= 0.0


def test_estimate_fill_price_large_order():
    """Large buy walks through multiple levels."""
    book = make_book([], [(0.56, 50), (0.57, 50), (0.58, 50)])
    avg_price, slippage = ob.estimate_fill_price(book, "BUY", size_usdc=100.0)
    # Should fill at blended price between 0.56 and 0.57
    assert 0.56 <= avg_price <= 0.58


def test_empty_book_returns_zero():
    book = make_book([], [])
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.mid_price is None
    avg, slippage = ob.estimate_fill_price(book, "BUY", 100.0)
    assert avg == 0.0


def test_total_liquidity():
    book = make_book([(0.54, 100), (0.53, 200)], [(0.56, 100)])
    assert ob.total_bid_liquidity(book) == pytest.approx(0.54 * 100 + 0.53 * 200)
    assert ob.total_ask_liquidity(book) == pytest.approx(0.56 * 100)
