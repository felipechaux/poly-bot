"""
Pydantic models for all Polymarket API responses.
These are the canonical data types used throughout the bot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PriceLevel(BaseModel):
    """A single price level in the order book."""

    price: float
    size: float


class OrderBookSummary(BaseModel):
    """Snapshot of a market's order book."""

    market: str  # condition_id
    asset_id: str  # token_id (YES or NO)
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> float | None:
        if not self.bids:
            return None
        return max(b.price for b in self.bids)

    @property
    def best_ask(self) -> float | None:
        if not self.asks:
            return None
        return min(a.price for a in self.asks)

    @property
    def mid_price(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb


class Token(BaseModel):
    """A YES or NO token within a market."""

    token_id: str
    outcome: str  # "Yes" or "No"
    price: float = 0.0


class Market(BaseModel):
    """Full market metadata from the Gamma API."""

    condition_id: str
    question_id: str = ""
    question: str
    description: str = ""
    market_slug: str = ""
    category: str = ""
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    volume: float = 0.0
    liquidity: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    last_trade_price: float = 0.0
    outcome_prices: list[float] = Field(default_factory=list)
    tokens: list[Token] = Field(default_factory=list)
    end_date_iso: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Extra Gamma API fields
    one_hour_price_change: float = 0.0
    one_day_price_change: float = 0.0

    @property
    def yes_token(self) -> Token | None:
        for t in self.tokens:
            if t.outcome.lower() == "yes":
                return t
        return None

    @property
    def no_token(self) -> Token | None:
        for t in self.tokens:
            if t.outcome.lower() == "no":
                return t
        return None


class MarketUpdate(BaseModel):
    """Published by the feed to all consumers."""

    market: Market
    order_book: OrderBookSummary
    received_at: datetime = Field(default_factory=datetime.utcnow)
