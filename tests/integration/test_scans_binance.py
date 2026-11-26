"""Intraday scans (London / London–NY), the always-on 16:30 push, and Binance testnet mirroring."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

from poly_bot.bot import Bot
from poly_bot.execution.binance_testnet import BinanceTestnet, parse_fill, round_step, to_symbol
from tests.conftest import FakeProvider
from tests.integration.test_bot_cycle import settings
from tests.unit.test_llm_analyst import FakeClaude, idea, no_news

BOG = ZoneInfo("America/Bogota")
SCANS = [{"name": "londres", "label": "Apertura Londres", "at": "08:00", "tz": "Europe/London"},
         {"name": "londres_ny", "label": "Solape Londres–NY", "at": "10:00", "tz": "America/New_York"}]


def scan_settings(tmp_path, analyst: bool = True):
    s = settings(tmp_path)
    s.execution = {"fill": "next_open", "settle_time": "16:30", "pending_max_days": 3}
    s.schedule = {**s.schedule, "daily_run_time": "08:00", "scans": SCANS}
    if analyst:
        s.strategies["core_allocation"]["params"]["portfolio_share"] = 0.85
        s.strategies["llm_claude"] = {"class": "llm_analyst", "enabled": True, "params": {
            "feeds": [], "sleeve_pct": 0.075, "max_position_pct": 0.04}}
    return s


# ── Scheduler ────────────────────────────────────────────────────────────


def test_scheduler_runs_scans_in_their_own_timezone(tmp_path):
    bot = Bot(scan_settings(tmp_path, analyst=False), provider=FakeProvider())
    t, job = bot._next_job(datetime(2026, 11, 24, 0, 0, tzinfo=BOG))
    assert job == "scan:londres" and (t.hour, t.minute) == (3, 0)  # 08:00 London (GMT) = 03:00 Bogotá
    t, job = bot._next_job(datetime(2026, 11, 24, 8, 30, tzinfo=BOG))
    assert job == "scan:londres_ny" and (t.hour, t.minute) == (10, 0)  # 10:00 NY (EST)
    t, job = bot._next_job(datetime(2026, 11, 24, 10, 30, tzinfo=BOG))
    assert job == "settle"
    t, job = bot._next_job(datetime(2026, 11, 27, 18, 0, tzinfo=BOG))  # Friday evening → Monday
    assert job == "scan:londres" and t.date().isoformat() == "2026-11-30"
    # London summer time (BST) moves the scan one hour earlier in Bogotá
    t, _ = bot._next_job(datetime(2026, 6, 2, 0, 0, tzinfo=BOG))
    assert (t.hour, t.minute) == (2, 0)


# ── 16:30 push ───────────────────────────────────────────────────────────


async def test_scheduled_settle_always_reports(tmp_path):
    bot = Bot(scan_settings(tmp_path, analyst=False), provider=FakeProvider())
    quiet = await bot.settle(notify=False)  # manual / on-start settle stays silent
    assert quiet.title == ""
    r = await bot.settle(notify=False, scheduled=True)
    assert "sin órdenes" in r.title and "No había órdenes pendientes" in r.message
    ev = await bot.store.events("settle")  # type: ignore[union-attr]
    assert ev[-1]["payload"]["waiting"] == 0
    await bot._shutdown()


# ── Scans ────────────────────────────────────────────────────────────────


async def test_scan_is_a_fresh_analysts_only_call(tmp_path):
    bot = Bot(scan_settings(tmp_path), provider=FakeProvider())
    llm = bot.strategies[1]
    fake = FakeClaude({"resumen_mercado": "a", "acciones": [idea("NU", peso=0.04)]})
    llm.client, llm.news_fetcher = fake, no_news

    await bot.run_cycle(notify=False)  # 08:00 decide: core + analyst
    snaps = len(await bot.store.snapshots())  # type: ignore[union-attr]
    core_signals = len(await bot.store.events("signals", strategy="core_allocation"))  # type: ignore[union-attr]

    r = await bot.run_cycle(notify=False, scan=bot.scans[0])
    assert fake.calls == 2  # the morning's analysis is not reused by the scan
    assert "Apertura Londres" in r.title
    assert len(await bot.store.snapshots()) == snaps  # type: ignore[union-attr]
    assert len(await bot.store.events("signals", strategy="core_allocation")) == core_signals  # type: ignore[union-attr]
    analyses = await bot.store.events("analysis", strategy="llm_claude")  # type: ignore[union-attr]
    assert [a["payload"]["slot"] for a in analyses] == ["decide", "scan:londres"]

    await bot.run_cycle(notify=False, scan=bot.scans[0])  # same scan again (restart): cached
    await bot.run_cycle(notify=False)  # decide re-run reuses the decide analysis, not the scan's
    assert fake.calls == 2
    await bot._shutdown()


async def test_scan_keeps_orders_already_at_their_open(tmp_path):
    bot = Bot(scan_settings(tmp_path), provider=FakeProvider())
    llm = bot.strategies[1]
    llm.client = FakeClaude({"resumen_mercado": "a", "acciones": [idea("NU", peso=0.04)]})
    llm.news_fetcher = no_news
    await bot.run_cycle(notify=False)
    [order] = [o for o in await bot.store.pending_orders() if o["ticker"] == "NU"]  # type: ignore[union-attr]
    # Decided before an open that has already happened → it fills at that open; keep it
    await bot.store._conn.execute(  # type: ignore[union-attr]
        "UPDATE pending_orders SET decided_at=? WHERE id=?", ("2026-11-02T13:00:00+00:00", order["id"]))
    await bot.store._conn.commit()  # type: ignore[union-attr]

    r = await bot.run_cycle(notify=False, scan=bot.scans[1])
    pending = [o for o in await bot.store.pending_orders() if o["ticker"] == "NU"]  # type: ignore[union-attr]
    assert [o["id"] for o in pending] == [order["id"]]  # not superseded, not duplicated
    assert not r.pending
    await bot._shutdown()


async def test_daily_entry_cap_holds_across_scans(tmp_path):
    s = scan_settings(tmp_path)
    s.strategies["llm_claude"]["params"]["max_new_per_day"] = 1
    bot = Bot(s, provider=FakeProvider())
    llm = bot.strategies[1]
    llm.client = FakeClaude({"resumen_mercado": "a", "acciones": [idea("NU", peso=0.03)]})
    llm.news_fetcher = no_news
    await bot.run_cycle(notify=False)
    llm.client = FakeClaude({"resumen_mercado": "b", "acciones": [idea("ECOPETROL.CL", peso=0.03)]})
    r = await bot.run_cycle(notify=False, scan=bot.scans[1])
    assert not r.pending  # NU (queued this morning) already used today's only new entry
    assert [o["ticker"] for o in await bot.store.pending_orders()  # type: ignore[union-attr]
            if o["strategy"] == "llm_claude"] == ["NU"]  # the scan never cancels the morning's order


async def test_decide_rerun_replaces_only_orders_not_yet_at_their_open(tmp_path):
    bot = Bot(scan_settings(tmp_path), provider=FakeProvider())
    llm = bot.strategies[1]
    llm.client = FakeClaude({"resumen_mercado": "a", "acciones": [
        idea("NU", peso=0.03), idea("ECOPETROL.CL", peso=0.03)]})
    llm.news_fetcher = no_news
    await bot.run_cycle(notify=False)
    orders = {o["ticker"]: o for o in await bot.store.pending_orders()  # type: ignore[union-attr]
              if o["strategy"] == "llm_claude"}
    # Decided after the 11-24 open → fills at the 11-25 open, which already happened (real clock)
    # but has no price bar yet: locked, so the re-run must keep it
    await bot.store._conn.execute(  # type: ignore[union-attr]
        "UPDATE pending_orders SET decided_at=? WHERE id=?",
        ("2026-11-24T16:00:00+00:00", orders["NU"]["id"]))
    await bot.store._conn.commit()  # type: ignore[union-attr]
    await bot.store._conn.execute("DELETE FROM agent_events WHERE event_type='analysis'")  # type: ignore[union-attr]
    await bot.store._conn.commit()  # type: ignore[union-attr]
    await bot.run_cycle(notify=False)  # fresh decide: ECOPETROL re-queued, NU kept as is
    left = [(o["ticker"], o["id"]) for o in await bot.store.pending_orders()  # type: ignore[union-attr]
            if o["strategy"] == "llm_claude"]
    assert ("NU", orders["NU"]["id"]) in left and len(left) == 2
    await bot._shutdown()
    await bot._shutdown()


# ── Binance testnet ──────────────────────────────────────────────────────


def test_symbol_and_step_rounding():
    assert to_symbol("BTC-USD") == "BTCUSDT"
    assert round_step(0.0123456, 0.00001) == 0.01234
    assert round_step(3.7, 1.0) == 3.0


def test_parse_fill_with_base_asset_commission():
    f = parse_fill({"orderId": 7, "side": "BUY", "executedQty": "0.02", "cummulativeQuoteQty": "1000",
                    "fills": [{"price": "50000", "qty": "0.02", "commission": "0.00002",
                               "commissionAsset": "BTC"}]}, "BTCUSDT")
    assert f.avg_price == 50_000 and abs(f.quantity - 0.01998) < 1e-12 and abs(f.fee - 1.0) < 1e-9
    # cash out = gross + fee = what Binance charged in USDT
    assert abs(f.quantity * f.avg_price + f.fee - 1000) < 1e-6


def fake_binance(price: float) -> tuple[BinanceTestnet, list[dict[str, list[str]]]]:
    orders: list[dict[str, list[str]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(req.url)).query)
        path = req.url.path
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "BTCUSDT", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5"}]}]})
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": str(price)})
        if path == "/api/v3/order":
            assert "signature" in q and req.headers["X-MBX-APIKEY"] == "k"
            orders.append(q)
            qty = float(q["quantity"][0])
            return httpx.Response(200, content=json.dumps({
                "orderId": 42, "side": q["side"][0], "status": "FILLED", "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * price),
                "fills": [{"price": str(price), "qty": str(qty), "commission": str(qty * price * 0.001),
                           "commissionAsset": "USDT"}]}))
        return httpx.Response(404, json={"code": -1, "msg": "?"})

    client = BinanceTestnet("k", "s", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client, orders


async def test_crypto_orders_fill_on_binance_testnet(tmp_path):
    bot = Bot(settings(tmp_path), provider=FakeProvider())  # BTC-USD real price 50,000
    bot.binance, orders = fake_binance(50_100.0)
    r = await bot.run_cycle(notify=False)
    [btc] = [t for t in r.executed if t.ticker == "BTC-USD"]
    assert len(orders) == 1 and orders[0]["symbol"] == ["BTCUSDT"] and orders[0]["side"] == ["BUY"]
    assert btc.price == 50_100.0 and "Binance testnet #42" in btc.rationale
    assert abs(btc.fee - btc.gross * 0.001) < 1e-6
    assert round(btc.quantity, 5) == btc.quantity  # testnet lot size respected
    vt = next(t for t in r.executed if t.ticker == "VT")
    assert "Binance" not in vt.rationale  # stocks stay simulated
    await bot._shutdown()


async def test_testnet_price_drift_falls_back_to_paper(tmp_path):
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    bot.binance, orders = fake_binance(60_000.0)  # 20% away from the real market
    r = await bot.run_cycle(notify=False)
    [btc] = [t for t in r.executed if t.ticker == "BTC-USD"]
    assert not orders and abs(btc.price - 50_000 * 1.001) < 1e-6  # simulated fill
    assert "Binance testnet" in r.message
    errors = await bot.store.events("error")  # type: ignore[union-attr]
    assert "se aleja" in errors[-1]["reasoning"]
    await bot._shutdown()
