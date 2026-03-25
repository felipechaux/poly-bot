"""Unit tests for the paper trading executor."""

from __future__ import annotations

import pytest
from poly_bot.execution.models import OrderRequest
from poly_bot.execution.paper_executor import PaperExecutor
from poly_bot.market_data.models import OrderBookSummary, PriceLevel


def make_book(bids: list[tuple], asks: list[tuple], token_id: str = "tok1") -> OrderBookSummary:
    return OrderBookSummary(
        market="mkt",
        asset_id=token_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


def make_request(side="BUY", price=0.56, size=100.0, token_id="tok1") -> OrderRequest:
    return OrderRequest(
        token_id=token_id,
        side=side,
        price=price,
        size_usdc=size,
        strategy="test",
    )


@pytest.mark.asyncio
async def test_instant_fill():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="instant", simulated_latency_ms=0)
    req = make_request()
    report = await executor.submit_order(req)
    assert report.status == "filled"
    assert len(report.fills) == 1
    assert executor.balance_usdc == pytest.approx(900.0)


@pytest.mark.asyncio
async def test_order_book_full_fill():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="order_book", simulated_latency_ms=0)
    book = make_book([], [(0.56, 200)])  # 200 shares available at 0.56
    req = make_request(price=0.60, size=100.0)  # Buy $100 worth, limit 0.60
    report = await executor.submit_order_with_book(req, book)
    assert report.status == "filled"
    assert report.total_filled_usdc == pytest.approx(100.0, rel=0.01)


@pytest.mark.asyncio
async def test_order_book_partial_fill():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="order_book", simulated_latency_ms=0)
    book = make_book([], [(0.56, 50)])  # Only $28 worth available (50 shares * 0.56)
    req = make_request(price=0.60, size=100.0)  # Want $100 worth
    report = await executor.submit_order_with_book(req, book)
    assert report.status == "partial"
    assert len(executor._resting) == 1  # Remainder became resting order


@pytest.mark.asyncio
async def test_insufficient_balance():
    executor = PaperExecutor(initial_balance_usdc=50.0, fill_mode="instant", simulated_latency_ms=0)
    req = make_request(size=100.0)  # Want $100 but only have $50
    report = await executor.submit_order(req)
    assert report.status == "rejected"
    assert "balance" in report.rejection_reason.lower()


@pytest.mark.asyncio
async def test_resting_order_fills_on_market_update():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="order_book", simulated_latency_ms=0)

    fills_received = []
    executor.on_fill(lambda f: fills_received.append(f))

    # Submit order with no liquidity — goes resting
    empty_book = make_book([], [])
    req = make_request(side="BUY", price=0.55, size=100.0)
    report = await executor.submit_order_with_book(req, empty_book)
    assert report.status == "open"
    assert len(executor._resting) == 1

    # Market update with ask at 0.54 (below our limit of 0.55) — should match
    new_book = make_book([(0.54, 200)], [(0.54, 200)])
    await executor.on_market_update(new_book)

    assert len(fills_received) == 1
    assert len(executor._resting) == 0  # Resting order consumed


@pytest.mark.asyncio
async def test_cancel_order():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="order_book", simulated_latency_ms=0)
    empty_book = make_book([], [])
    req = make_request(size=100.0)
    report = await executor.submit_order_with_book(req, empty_book)

    order_id = report.order_id
    cancelled = await executor.cancel_order(order_id)
    assert cancelled is True
    assert order_id not in executor._resting


@pytest.mark.asyncio
async def test_fill_callback_fires():
    executor = PaperExecutor(initial_balance_usdc=1000.0, fill_mode="instant", simulated_latency_ms=0)
    fills = []
    executor.on_fill(lambda f: fills.append(f))
    await executor.submit_order(make_request())
    assert len(fills) == 1
    assert fills[0].strategy == "test"
