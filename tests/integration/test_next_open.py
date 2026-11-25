"""Next-open fills: orders decided before the open fill at that session's opening price."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

import poly_bot.bot as botmod
from poly_bot.bot import Bot, _business_days
from poly_bot.execution.sessions import next_open_fill
from tests.conftest import FakeProvider
from tests.integration.test_bot_cycle import settings

REAL_DT = botmod.datetime


def at(y: int, m: int, d: int, hh: int, mm: int = 0) -> type:
    class D(REAL_DT):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
            return REAL_DT(y, m, d, hh, mm, tzinfo=botmod.ZoneInfo("America/Bogota")).astimezone(tz)
    return D


def next_open_settings(tmp_path):
    s = settings(tmp_path)
    s.execution = {"fill": "next_open", "settle_time": "16:30", "pending_max_days": 3}
    s.schedule = {**s.schedule, "daily_run_time": "08:00"}
    return s


def test_next_open_fill_never_uses_an_open_already_known():
    opens = pd.Series([10.0, 11.0], index=pd.to_datetime(["2026-11-24", "2026-11-25"]))
    before = datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc)   # 08:00 Bogotá
    after = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)    # 10:00 Bogotá, after the 9:30 open
    assert next_open_fill("US", before, opens) == (date(2026, 11, 24), 10.0)
    assert next_open_fill("US", after, opens) == (date(2026, 11, 25), 11.0)
    assert next_open_fill("BVC", after, opens[:1]) is None
    assert next_open_fill("CRYPTO", before, opens) is None  # crypto fills immediately instead


def test_business_days():
    assert _business_days(date(2026, 11, 27), date(2026, 11, 30)) == 1  # Fri → Mon
    assert _business_days(date(2026, 11, 24), date(2026, 11, 24)) == 0


async def test_morning_queues_then_settle_fills_at_open(tmp_path):
    prov = FakeProvider()  # bars up to 2026-11-24
    bot = Bot(next_open_settings(tmp_path), provider=prov)
    with patch.object(botmod, "datetime", at(2026, 11, 24, 8)):
        r = await bot.run_cycle(notify=False)
    # Crypto filled immediately; VT and ICOLCAP.CL wait for the open
    assert {t.ticker for t in r.executed if t.kind == "BUY"} == {"BTC-USD"}
    assert {o["ticker"] for o in r.pending} == {"VT", "ICOLCAP.CL"}
    assert "Órdenes para la apertura" in r.message
    assert len(await bot.store.pending_orders()) == 2  # type: ignore[union-attr]

    # The real "now" is later than 08:00 on 11-24, so the 11-24 open can't be used: decided_at is
    # stored in real UTC time. Re-stamp the orders as decided at 08:00 to simulate the schedule.
    await bot.store._conn.execute(  # type: ignore[union-attr]
        "UPDATE pending_orders SET decided_at=?", ("2026-11-24T13:00:00+00:00",))
    await bot.store._conn.commit()  # type: ignore[union-attr]
    prov.opens = {"VT": 102.0, "ICOLCAP.CL": 20_500.0}
    with patch.object(botmod, "datetime", at(2026, 11, 24, 16, 30)):
        s = await bot.settle(notify=False)
    fills = {t.ticker: t.price for t in s.executed if t.kind == "BUY"}
    assert set(fills) == {"VT", "ICOLCAP.CL"}
    assert abs(fills["VT"] - 102.0 * 1.0005) < 1e-6  # opening price + US slippage
    assert abs(fills["ICOLCAP.CL"] - 20_500.0 * 1.002) < 1e-6
    assert not await bot.store.pending_orders()  # type: ignore[union-attr]
    closed = await bot.store.pending_orders(status="filled")  # type: ignore[union-attr]
    assert {o["fill_day"] for o in closed} == {"2026-11-24"}
    assert "Órdenes ejecutadas" in s.title
    await bot._shutdown()


async def test_late_decision_waits_and_rerun_supersedes(tmp_path):
    bot = Bot(next_open_settings(tmp_path), provider=FakeProvider())
    await bot.run_cycle(notify=False)  # real now > 11-24 open: nothing can fill yet
    s = await bot.settle(notify=False)
    assert not s.executed and len(await bot.store.pending_orders()) == 2  # type: ignore[union-attr]
    await bot.run_cycle(notify=False)  # same-day re-run replaces, never duplicates
    assert len(await bot.store.pending_orders()) == 2  # type: ignore[union-attr]
    assert len(await bot.store.pending_orders(status="superseded")) == 2  # type: ignore[union-attr]
    await bot._shutdown()


async def test_dry_run_does_not_persist_pending(tmp_path):
    bot = Bot(next_open_settings(tmp_path), provider=FakeProvider())
    r = await bot.run_cycle(dry_run=True, notify=False)
    assert r.pending and not await bot.store.pending_orders()  # type: ignore[union-attr]
    await bot._shutdown()


def test_scheduler_alternates_decide_and_settle(tmp_path):
    bot = Bot(next_open_settings(tmp_path), provider=FakeProvider())
    tz = bot.tz
    assert bot._next_job(REAL_DT(2026, 11, 24, 7, 0, tzinfo=tz))[1] == "decide"
    t, job = bot._next_job(REAL_DT(2026, 11, 24, 12, 0, tzinfo=tz))
    assert job == "settle" and t.hour == 16 and t.minute == 30
    t, job = bot._next_job(REAL_DT(2026, 11, 27, 18, 0, tzinfo=tz))  # Friday evening → Monday
    assert t.weekday() == 0
