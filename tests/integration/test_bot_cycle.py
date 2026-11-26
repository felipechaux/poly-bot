"""End-to-end daily cycle with a fake price provider and a temp SQLite DB."""

from __future__ import annotations

from datetime import date

import pytest

from poly_bot.bot import Bot
from poly_bot.config.settings import Settings
from poly_bot.market_data.models import Dividend
from tests.conftest import ASSETS, TODAY, FakeProvider


def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "t.db"),
        ntfy_topic="",
        portfolio={"base_currency": "COP", "initial_cash": {"COP": 20_000_000.0, "USD": 5_000.0}},
        schedule={"timezone": "America/Bogota", "daily_run_time": "17:00", "run_on_start": False},
        market_data={"max_stale_days": 5000},
        costs={"fx_spread_pct": 0.005},
        execution={"fill": "close"},
        risk={"max_weight_per_asset": 0.7, "min_order_value_usd": 25.0},
        strategies={"core_allocation": {"enabled": True, "params": {
            "targets": {"VT": 0.6, "ICOLCAP.CL": 0.3, "BTC-USD": 0.1}, "cash_buffer_pct": 0.01}}},
        universe=[a.model_dump() for a in ASSETS],
    )


async def test_first_cycle_allocates_and_persists(tmp_path):
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    r = await bot.run_cycle()
    kinds = [t.kind for t in r.executed]
    assert "FX" in kinds and kinds.count("BUY") == 3
    snap = bot.snapshot()
    assert snap is not None
    for t, w in {"VT": 0.6, "ICOLCAP.CL": 0.3, "BTC-USD": 0.1}.items():
        assert snap.weight(t) == pytest.approx(w * 0.99, abs=0.01)
    total = snap.total_value()
    assert 0.985 * 40_000_000 < total < 40_000_000  # costs ate a little
    assert "Cartera" in r.title
    await bot._shutdown()

    # Restart: state rebuilt from the ledger, no new deposits, no re-allocation same month
    bot2 = Bot(settings(tmp_path), provider=FakeProvider())
    r2 = await bot2.run_cycle()
    assert [t for t in r2.executed if t.kind == "BUY"] == []
    snap2 = bot2.snapshot()
    assert snap2 is not None and snap2.total_value() == pytest.approx(total)
    assert snap2.deposits == {"COP": 20_000_000.0, "USD": 5_000.0}
    await bot2._shutdown()


async def test_dry_run_changes_nothing(tmp_path):
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    r = await bot.run_cycle(dry_run=True)
    assert r.executed
    assert bot.tracker.get_position("VT") is None
    assert bot.store is not None and await bot.store.snapshots() == []
    await bot._shutdown()


async def test_dividend_credited_once_with_withholding(tmp_path):
    prov = FakeProvider()
    bot = Bot(settings(tmp_path), provider=prov)
    await bot.run_cycle()
    vt = bot.tracker.held_quantity("VT")
    usd_before = bot.tracker.cash["USD"]

    # Next day: VT pays a dividend dated after the position was opened
    from datetime import timedelta
    ex = date.today() + timedelta(days=1)  # relative: the position was opened today (real clock)
    prov.dividends = {"VT": [Dividend(ticker="VT", ex_date=ex, amount_per_share=1.0, currency="USD")]}
    from unittest.mock import patch
    import poly_bot.bot as botmod

    class D(botmod.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
            return botmod.datetime(ex.year, ex.month, ex.day, 17, 0, tzinfo=tz)

    with patch.object(botmod, "datetime", D):
        await bot.run_cycle()
        await bot.run_cycle()  # second run same day must not double-credit
    assert bot.tracker.cash["USD"] == pytest.approx(usd_before + vt * 1.0 * 0.70)
    await bot._shutdown()


def test_next_run_skips_weekend(tmp_path):
    from datetime import datetime

    bot = Bot(settings(tmp_path), provider=FakeProvider())
    fri_evening = datetime(2026, 11, 27, 18, 0, tzinfo=bot.tz)  # Friday after 17:00
    nxt = bot._next_run_time(fri_evening)
    assert nxt.weekday() == 0 and nxt.hour == 17
    assert TODAY.weekday() == 1


def test_api_cycle_requires_token_only_for_real_runs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from poly_bot.web.api import create_app

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    with TestClient(create_app(bot)) as client:
        assert client.post("/api/cycle?dry_run=true").status_code == 200
        assert client.post("/api/cycle?dry_run=false").status_code == 401
        ok = client.post("/api/cycle?dry_run=false", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json()["executed"]
        assert client.get("/api/portfolio").json()["ready"] is True


async def test_missed_today_only_before_first_cycle(tmp_path):
    bot = Bot(settings(tmp_path), provider=FakeProvider())
    await bot.start()
    assert await bot._missed_today()
    await bot.run_cycle(notify=False)
    assert not await bot._missed_today()
    await bot._shutdown()


async def test_llm_cycle_journal_scoring_and_api(tmp_path, monkeypatch):
    from datetime import datetime as real_dt
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    import poly_bot.bot as botmod
    from poly_bot.web.api import create_app
    from tests.unit.test_llm_analyst import FakeClaude, idea, no_news

    s = settings(tmp_path)
    s.strategies["core_allocation"]["params"]["portfolio_share"] = 0.85
    s.strategies["llm_analyst"] = {"enabled": True, "params": {"feeds": [], "max_position_pct": 0.05}}
    prov = FakeProvider()
    bot = Bot(s, provider=prov)
    llm = bot.strategies[1]
    assert llm.excluded == {"VT", "ICOLCAP.CL", "BTC-USD"}
    fake = FakeClaude({"resumen_mercado": "mercado mixto", "acciones": [idea("NU", conf=0.8)]})
    llm.client, llm.news_fetcher = fake, no_news

    r = await bot.run_cycle()
    assert any(t.ticker == "NU" and t.kind == "BUY" for t in r.executed)
    assert "Claude" in r.message
    await bot.run_cycle()  # same day: cached analysis, no second call
    assert fake.calls == 1

    # 8 days later NU is up 20% → 1d and 7d outcomes are hits
    prov.prices["NU"] = 12.0

    class D(botmod.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
            return real_dt(2026, 12, 2, 8, 0, tzinfo=tz)

    with patch.object(botmod, "datetime", D):
        await bot.run_cycle(notify=False)
    assert fake.calls == 2
    outcomes = await bot.store.events("outcome")  # type: ignore[union-attr]
    assert {o["payload"]["horizon_days"] for o in outcomes} == {1, 7}
    assert all(o["payload"]["hit"] for o in outcomes)
    # FakeProvider's close history is flat, so the basket return is 0 and excess = NU's +20%
    assert all(o["payload"]["basket_ret"] == 0 for o in outcomes)
    assert all(abs(o["payload"]["excess"] - 0.20) < 1e-9 for o in outcomes)

    with TestClient(create_app(bot)) as client:
        d = client.get("/api/llm").json()
    a = d["analysts"][0]
    assert a["name"] == "llm_analyst" and a["label"] == "Claude"
    assert a["usage"]["calls_30d"] == 2 and a["usage"]["tokens_30d"] == 2400
    assert a["hit_rates"]["7d"]["hit_rate"] == 1.0
    assert a["sleeve"]["positions"] == ["NU"] and a["sleeve"]["pnl"] > 0
    assert a["semaforo"]["color"] == "gris" and a["semaforo"]["n"] == 1 and a["guard"] is None
    assert d["journal"][-1]["outcomes"]["7d"]["hit"] is True
    assert d["journal"][-1]["analyst"] == "Claude"
    await bot._shutdown()


async def test_missed_today_respects_run_time(tmp_path):
    from datetime import datetime as real_dt
    from unittest.mock import patch

    import poly_bot.bot as botmod

    bot = Bot(settings(tmp_path), provider=FakeProvider())  # daily_run_time 17:00
    await bot.run_cycle(notify=False)  # creates today's snapshot (2026-11-24 real date)

    def at(y, m, d, hh):
        class D(botmod.datetime):  # type: ignore[misc,valid-type]
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
                return real_dt(y, m, d, hh, 0, tzinfo=tz)
        return patch.object(botmod, "datetime", D)

    with at(2030, 1, 2, 6):   # Wednesday 06:00, before run time
        assert not await bot._missed_today()
    with at(2030, 1, 2, 18):  # after run time, no snapshot that day
        assert await bot._missed_today()
    with at(2030, 1, 5, 18):  # Saturday
        assert not await bot._missed_today()
    await bot._shutdown()


async def test_two_analysts_compare_side_by_side(tmp_path):
    from fastapi.testclient import TestClient

    from poly_bot.web.api import create_app
    from tests.unit.test_llm_analyst import FakeClaude, idea, no_news

    s = settings(tmp_path)
    s.strategies["core_allocation"]["params"]["portfolio_share"] = 0.85
    common = {"feeds": [], "sleeve_pct": 0.075, "max_position_pct": 0.04}
    s.strategies["llm_claude"] = {"class": "llm_analyst", "enabled": True,
                                  "params": {**common, "provider": "claude"}}
    s.strategies["llm_gemini"] = {"class": "llm_analyst", "enabled": True,
                                  "params": {**common, "provider": "agy", "model": "gemini-4-neon-high"}}
    bot = Bot(s, provider=FakeProvider())
    claude, gemini = bot.strategies[1], bot.strategies[2]
    assert (claude.name, gemini.name) == ("llm_claude", "llm_gemini")
    assert type(gemini.client).__name__ == "AgyCLI"
    # Both want NU; Claude runs first and gets it, so NU is no longer eligible for Gemini
    claude.client = FakeClaude({"resumen_mercado": "a", "acciones": [idea("NU", peso=0.04)]})
    gemini.client = FakeClaude({"resumen_mercado": "b", "acciones": [
        idea("NU", peso=0.04), idea("ECOPETROL.CL", peso=0.04)]})
    claude.news_fetcher = gemini.news_fetcher = no_news

    r = await bot.run_cycle(notify=False)
    owners = {p.ticker: p.strategy for p in bot.tracker.snapshot().positions}
    assert owners["NU"] == "llm_claude" and owners["ECOPETROL.CL"] == "llm_gemini"
    assert "Claude" in r.message and "Gemini" in r.message
    assert "NU" not in gemini.client.last_schema["properties"]["acciones"]["items"]["properties"]["ticker"]["enum"]

    with TestClient(create_app(bot)) as client:
        d = client.get("/api/llm").json()
    assert [a["label"] for a in d["analysts"]] == ["Claude", "Gemini"]
    assert d["analysts"][1]["sleeve"]["positions"] == ["ECOPETROL.CL"]
    assert {j["analyst"] for j in d["journal"]} == {"Claude", "Gemini"}
    await bot._shutdown()
