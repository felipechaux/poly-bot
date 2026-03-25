"""Shared test fixtures."""

from __future__ import annotations

import pytest
from datetime import datetime

from poly_bot.market_data.models import Market, OrderBookSummary, PriceLevel, Token
from poly_bot.portfolio.models import PortfolioSnapshot, Position


@pytest.fixture
def sample_order_book() -> OrderBookSummary:
    return OrderBookSummary(
        market="0xabc123",
        asset_id="0xtoken_yes",
        bids=[
            PriceLevel(price=0.54, size=100.0),
            PriceLevel(price=0.53, size=200.0),
            PriceLevel(price=0.52, size=300.0),
        ],
        asks=[
            PriceLevel(price=0.56, size=100.0),
            PriceLevel(price=0.57, size=200.0),
            PriceLevel(price=0.58, size=300.0),
        ],
    )


@pytest.fixture
def sample_market() -> Market:
    return Market(
        condition_id="0xabc123",
        question="Will BTC close above $100k on April 1, 2026?",
        liquidity=50000.0,
        volume=100000.0,
        tokens=[
            Token(token_id="0xtoken_yes", outcome="Yes", price=0.55),
            Token(token_id="0xtoken_no", outcome="No", price=0.45),
        ],
    )


@pytest.fixture
def empty_portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(cash_usdc=10_000.0)
