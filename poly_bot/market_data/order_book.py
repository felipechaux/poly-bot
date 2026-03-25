"""
Stateless order book analysis functions.
All functions are pure — no side effects, fully testable.
"""

from __future__ import annotations

from poly_bot.market_data.models import OrderBookSummary, PriceLevel


def best_bid(book: OrderBookSummary) -> float | None:
    return book.best_bid


def best_ask(book: OrderBookSummary) -> float | None:
    return book.best_ask


def mid_price(book: OrderBookSummary) -> float | None:
    return book.mid_price


def spread(book: OrderBookSummary) -> float | None:
    return book.spread


def relative_spread(book: OrderBookSummary) -> float | None:
    """Spread as a fraction of mid price."""
    mid = book.mid_price
    s = book.spread
    if mid is None or s is None or mid == 0:
        return None
    return s / mid


def book_imbalance(book: OrderBookSummary, depth: int = 5) -> float:
    """
    Order book imbalance in [-1, 1].
    +1 = all bid pressure (buy signal), -1 = all ask pressure (sell signal).
    Uses top `depth` levels on each side weighted by size.
    """
    bid_vol = sum(b.size for b in sorted(book.bids, key=lambda x: -x.price)[:depth])
    ask_vol = sum(a.size for a in sorted(book.asks, key=lambda x: x.price)[:depth])
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def weighted_mid(book: OrderBookSummary, depth: int = 3) -> float | None:
    """Size-weighted mid price using top `depth` levels."""
    top_bids = sorted(book.bids, key=lambda x: -x.price)[:depth]
    top_asks = sorted(book.asks, key=lambda x: x.price)[:depth]
    if not top_bids or not top_asks:
        return None
    bid_notional = sum(b.price * b.size for b in top_bids)
    ask_notional = sum(a.price * a.size for a in top_asks)
    bid_vol = sum(b.size for b in top_bids)
    ask_vol = sum(a.size for a in top_asks)
    total_vol = bid_vol + ask_vol
    if total_vol == 0:
        return None
    return (bid_notional + ask_notional) / total_vol


def estimate_fill_price(
    book: OrderBookSummary,
    side: str,
    size_usdc: float,
) -> tuple[float, float]:
    """
    Walk the order book for a given USDC size.
    Returns (average_fill_price, total_slippage).

    side: "BUY" (walks asks) or "SELL" (walks bids)
    size_usdc: amount of USDC to spend/receive

    Returns (avg_price, slippage_from_mid) or (0.0, 0.0) if no liquidity.
    """
    mid = book.mid_price or 0.0
    levels: list[PriceLevel]

    if side.upper() == "BUY":
        levels = sorted(book.asks, key=lambda x: x.price)
    else:
        levels = sorted(book.bids, key=lambda x: -x.price)

    if not levels:
        return 0.0, 0.0

    remaining = size_usdc
    total_shares = 0.0
    total_cost = 0.0

    for level in levels:
        level_cost = level.price * level.size
        if remaining <= 0:
            break
        fill_cost = min(remaining, level_cost)
        fill_shares = fill_cost / level.price
        total_cost += fill_cost
        total_shares += fill_shares
        remaining -= fill_cost

    if total_shares == 0:
        return 0.0, 0.0

    avg_price = total_cost / total_shares
    slippage = abs(avg_price - mid) if mid > 0 else 0.0
    return avg_price, slippage


def total_bid_liquidity(book: OrderBookSummary) -> float:
    """Total USDC on the bid side."""
    return sum(b.price * b.size for b in book.bids)


def total_ask_liquidity(book: OrderBookSummary) -> float:
    """Total USDC on the ask side."""
    return sum(a.price * a.size for a in book.asks)
