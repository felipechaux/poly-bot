"""Crypto fills walking Binance's real order book (public data) instead of a fixed slippage."""

from __future__ import annotations

import httpx
import pytest

from poly_bot.bot import Bot
from poly_bot.market_data.orderbook import OrderBook, OrderBookError, walk_book
from tests.conftest import FakeProvider
from tests.integration.test_bot_cycle import settings

# BTC real price in the fake provider: 50,000
ASKS = [["50010", "0.005"], ["50020", "0.01"], ["50100", "5"]]
BIDS = [["49990", "0.005"], ["49980", "0.01"], ["49900", "5"]]


def test_walk_book_vwap():
    levels = [(100.0, 1.0), (101.0, 1.0), (105.0, 10.0)]
    assert walk_book(levels, 0.5) == (100.0, 1)
    vwap, used = walk_book(levels, 3.0)
    assert used == 3 and abs(vwap - (100 + 101 + 105) / 3) < 1e-12
    with pytest.raises(OrderBookError):
        walk_book(levels, 50.0)


def book_client(asks=ASKS, bids=BIDS, status: int = 200) -> tuple[OrderBook, list[str]]:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        assert req.url.path == "/api/v3/depth" and req.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(status, json={"bids": bids, "asks": asks})

    return OrderBook(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


def book_settings(tmp_path):
    s = settings(tmp_path)
    s.execution = {"fill": "close", "crypto_fill": "orderbook"}
    return s


async def test_crypto_buy_walks_the_real_book(tmp_path):
    bot = Bot(book_settings(tmp_path), provider=FakeProvider())
    assert bot.orderbook is not None
    bot.orderbook, seen = book_client()
    r = await bot.run_cycle(notify=False)
    [btc] = [t for t in r.executed if t.ticker == "BTC-USD"]
    # ~US$ 990 of BTC ≈ 0.0198 BTC: consumes 0.005 @50010 + 0.01 @50020 + rest @50100
    assert seen and 50_010 < btc.price < 50_100
    assert "libro Binance: 3 niveles" in btc.rationale
    vt = next(t for t in r.executed if t.ticker == "VT")
    assert abs(vt.price - 100 * 1.0005) < 1e-9  # stocks keep the slippage model

    [ev] = await bot.store.events("crypto_fill")  # type: ignore[union-attr]
    p = ev["payload"]
    assert p["source"] == "orderbook" and p["levels"] == 3 and p["mid"] == 50_000
    assert abs(p["vwap"] - btc.price) < 1e-9 and p["model_price"] == pytest.approx(50_050)

    from fastapi.testclient import TestClient

    from poly_bot.web.api import create_app
    with TestClient(create_app(bot)) as client:
        d = client.get("/api/binance").json()
        assert "Ejecución cripto" in client.get("/").text
    assert d["crypto_fill"] == "orderbook" and d["book_stats_30d"]["fills"] == 1
    assert d["book_stats_30d"]["avg_vs_model_pct"] is not None
    assert d["state"] == "off"  # testnet stays off
    await bot._shutdown()


async def test_book_far_from_market_falls_back_to_model(tmp_path):
    bot = Bot(book_settings(tmp_path), provider=FakeProvider())
    bot.orderbook, _ = book_client(asks=[["60010", "9"]], bids=[["59990", "9"]])
    r = await bot.run_cycle(notify=False)
    [btc] = [t for t in r.executed if t.ticker == "BTC-USD"]
    assert abs(btc.price - 50_000 * 1.001) < 1e-6 and "libro" not in btc.rationale
    assert "Libro de Binance" in r.message
    [ev] = await bot.store.events("crypto_fill")  # type: ignore[union-attr]
    assert ev["payload"]["source"] == "model" and "se aleja" in ev["reasoning"]
    await bot._shutdown()


async def test_book_unavailable_falls_back_to_model(tmp_path):
    bot = Bot(book_settings(tmp_path), provider=FakeProvider())
    bot.orderbook, _ = book_client(status=451)
    r = await bot.run_cycle(notify=False)
    [btc] = [t for t in r.executed if t.ticker == "BTC-USD"]
    assert abs(btc.price - 50_000 * 1.001) < 1e-6
    await bot._shutdown()
