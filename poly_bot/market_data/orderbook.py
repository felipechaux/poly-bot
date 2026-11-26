"""
Real order-book fills for crypto — simulate a market order against Binance's live book.

Public market data from data-api.binance.vision (Binance's market-data-only endpoint: no keys,
and reachable from regions where api.binance.com is blocked). A BUY walks the asks, a SELL the
bids, until the quantity is filled; the fill price is the volume-weighted average of the levels
consumed. That captures the real spread and market impact instead of a fixed slippage %.

Positions are still marked to market with Yahoo prices; if the book's mid strays too far from
Yahoo's quote (bad data / a USDT de-peg) the caller falls back to the fixed-slippage model.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

DATA_URL = "https://data-api.binance.vision"


class OrderBookError(Exception):
    pass


@dataclass(frozen=True)
class BookFill:
    symbol: str
    side: str
    quantity: float
    vwap: float  # average fill price walking the book
    best: float  # best ask (BUY) / best bid (SELL)
    mid: float
    spread_pct: float  # (ask - bid) / mid
    impact_pct: float  # how much worse than mid (positive = cost)
    levels: int  # book levels consumed


def to_symbol(ticker: str) -> str:
    """BTC-USD → BTCUSDT."""
    return f"{ticker.upper().split('-')[0]}USDT"


def walk_book(levels: list[tuple[float, float]], quantity: float) -> tuple[float, int]:
    """(vwap, levels used) to fill `quantity` from best to worst; OrderBookError if too thin."""
    if quantity <= 0:
        raise OrderBookError("cantidad 0")
    left, cost, used = quantity, 0.0, 0
    for price, size in levels:
        take = min(left, size)
        cost += take * price
        left -= take
        used += 1
        if left <= 1e-12:
            return cost / quantity, used
    raise OrderBookError(f"libro sin profundidad suficiente para {quantity:g}")


class OrderBook:
    def __init__(self, base_url: str = DATA_URL, depth: int = 100, max_deviation_pct: float = 0.02,
                 timeout_seconds: float = 10.0, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.depth = depth
        self.max_deviation_pct = max_deviation_pct
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def book(self, symbol: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        try:
            resp = await self._client.get(f"{self.base_url}/api/v3/depth",
                                          params={"symbol": symbol, "limit": self.depth})
        except httpx.HTTPError as exc:
            raise OrderBookError(f"sin conexión con el libro de Binance: {exc}") from exc
        if resp.status_code >= 400:
            raise OrderBookError(f"libro de Binance HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        if not bids or not asks:
            raise OrderBookError(f"libro vacío para {symbol}")
        return bids, asks

    async def fill(self, ticker: str, side: str, quantity: float, reference_price: float) -> BookFill:
        symbol = to_symbol(ticker)
        bids, asks = await self.book(symbol)
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        if reference_price > 0 and abs(mid / reference_price - 1) > self.max_deviation_pct:
            raise OrderBookError(f"libro {mid:,.2f} se aleja {abs(mid / reference_price - 1):.1%} "
                                 f"del precio de referencia ({reference_price:,.2f})")
        vwap, used = walk_book(asks if side == "BUY" else bids, quantity)
        impact = vwap / mid - 1 if side == "BUY" else 1 - vwap / mid
        fill = BookFill(symbol=symbol, side=side, quantity=quantity, vwap=vwap,
                        best=ask if side == "BUY" else bid, mid=mid,
                        spread_pct=(ask - bid) / mid, impact_pct=impact, levels=used)
        log.info("orderbook.fill", symbol=symbol, side=side, qty=quantity, vwap=round(vwap, 4),
                 impact_bps=round(impact * 1e4, 2), levels=used)
        return fill
