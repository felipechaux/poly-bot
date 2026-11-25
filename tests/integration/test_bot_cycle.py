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
    prov.dividends = {"VT": [Dividend(ticker="VT", ex_date=date(2026, 11, 25), amount_per_share=1.0, currency="USD")]}
    from unittest.mock import patch
    import poly_bot.bot as botmod

    class D(botmod.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
            return botmod.datetime(2026, 11, 25, 17, 0, tzinfo=tz)

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
