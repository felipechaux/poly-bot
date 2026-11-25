"""LLM analyst: limits, exclusions, stop-loss, dry-run/cache never call Claude, failures."""

from __future__ import annotations

import pytest

from poly_bot.execution.models import Transaction
from poly_bot.llm.claude_cli import ClaudeError, ClaudeResult, ClaudeUsage
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.strategies.base import DailyContext
from poly_bot.strategies.llm_analyst import LLMAnalystStrategy
from tests.conftest import TODAY, USDCOP, make_quotes


class FakeClaude:
    def __init__(self, output=None, error: Exception | None = None):
        self.output = output or {"resumen_mercado": "ok", "acciones": []}
        self.error = error
        self.calls = 0
        self.last_schema = None

    async def run(self, prompt, schema, system_prompt):
        self.calls += 1
        self.last_schema = schema
        if self.error:
            raise self.error
        return ClaudeResult(output=self.output, usage=ClaudeUsage(input_tokens=1000, output_tokens=200,
                                                                   cost_usd_equiv=0.02, model="sonnet"))


async def no_news(*a, **k):
    return []


def idea(ticker, accion="BUY", peso=0.05, conf=0.8):
    return {"ticker": ticker, "accion": accion, "peso_objetivo": peso, "confianza": conf,
            "horizonte": "1 mes", "razon": "tendencia", "riesgos": "volatilidad"}


def strat(client, **params):
    s = LLMAnalystStrategy({"params": {"feeds": [{"name": "x", "url": "http://x"}], **params}},
                           client=client, news_fetcher=no_news)
    s.excluded = {"VT", "ICOLCAP.CL", "BTC-USD"}  # core targets
    return s


def ctx(universe, tracker=None, **extra):
    t = tracker or PositionTracker()
    if not t.has_deposits:
        t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=10_000.0))
    q = make_quotes()
    t.update_prices(q, USDCOP)
    return DailyContext(today=TODAY, quotes=q, usdcop=USDCOP, portfolio=t.snapshot(),
                        universe=universe, extra=extra)


async def test_buy_ideas_respect_limits_and_exclusions(universe):
    client = FakeClaude({"resumen_mercado": "x", "acciones": [
        idea("NU", peso=0.05, conf=0.9), idea("ECOPETROL.CL", peso=0.04, conf=0.75),
        idea("NU", accion="HOLD"), idea("ECOPETROL.CL", conf=0.5)]})
    c = ctx(universe)
    sigs = await strat(client).on_daily(c)
    assert client.calls == 1
    # core tickers are not even offered to the model
    assert "VT" not in client.last_schema["properties"]["acciones"]["items"]["properties"]["ticker"]["enum"]
    by = {s.ticker: s for s in sigs}
    assert by["NU"].value == pytest.approx(500.0)  # 5% of 10k USD
    assert by["ECOPETROL.CL"].value == pytest.approx(0.04 * 10_000 * USDCOP)  # in COP
    kinds = [e["event_type"] for e in c.events]
    assert kinds.count("analysis") == 1 and kinds.count("recommendation") == 3
    assert any("Claude" in n for n in c.notes)


async def test_max_new_per_day_and_sleeve_cap(universe):
    client = FakeClaude({"resumen_mercado": "x", "acciones": [idea("NU"), idea("ECOPETROL.CL")]})
    sigs = await strat(client, max_new_per_day=1).on_daily(ctx(universe))
    assert len(sigs) == 1
    sigs = await strat(client, sleeve_pct=0.06).on_daily(ctx(universe))
    total = sum(s.value or 0 for s in sigs if s.ticker == "NU") + \
        sum((s.value or 0) / USDCOP for s in sigs if s.ticker == "ECOPETROL.CL")
    assert total == pytest.approx(600.0)  # 6% of 10k USD


async def test_stop_loss_sells_without_llm_buying_back(universe):
    t = PositionTracker()
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=10_000.0))
    t.apply(Transaction(kind="BUY", currency="USD", ticker="NU", quantity=10, price=20.0, gross=200,
                        strategy="llm_analyst"))  # bought at 20, now 10 → -50%
    client = FakeClaude({"resumen_mercado": "x", "acciones": [idea("NU")]})
    sigs = await strat(client).on_daily(ctx(universe, t))
    assert [(s.ticker, s.side, s.quantity) for s in sigs] == [("NU", "SELL", 10)]


async def test_dry_run_and_cache_never_call_claude(universe):
    client = FakeClaude({"resumen_mercado": "x", "acciones": [idea("NU")]})
    c = ctx(universe, dry_run=True)
    assert await strat(client).on_daily(c) == []
    cached = {"resumen_mercado": "x", "acciones": [idea("NU")], "usage": {}}
    c2 = ctx(universe, cached_analysis=cached)
    sigs = await strat(client).on_daily(c2)
    assert client.calls == 0 and sigs[0].ticker == "NU"
    assert not [e for e in c2.events if e["event_type"] == "recommendation"]  # not re-journaled


async def test_claude_failure_is_contained(universe):
    c = ctx(universe)
    sigs = await strat(FakeClaude(error=ClaudeError("usage limit"))).on_daily(c)
    assert sigs == []
    assert c.events[0]["event_type"] == "error" and "usage limit" in c.notes[0]


async def test_sell_idea_reduces_held_position(universe):
    t = PositionTracker()
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=10_000.0))
    t.apply(Transaction(kind="BUY", currency="USD", ticker="NU", quantity=40, price=10.0, gross=400,
                        strategy="llm_analyst"))
    client = FakeClaude({"resumen_mercado": "x", "acciones": [idea("NU", accion="SELL", peso=0.0, conf=0.6)]})
    sigs = await strat(client).on_daily(ctx(universe, t))
    assert sigs[0].side == "SELL" and sigs[0].quantity == pytest.approx(40)


async def test_guard_blocks_buys_but_keeps_exits(universe):
    t = PositionTracker()
    t.apply(Transaction(kind="DEPOSIT", currency="USD", gross=10_000.0))
    t.apply(Transaction(kind="BUY", currency="USD", ticker="NU", quantity=10, price=10, gross=100,
                        strategy="llm_analyst"))
    client = FakeClaude({"resumen_mercado": "x", "acciones": [
        idea("ECOPETROL.CL", conf=0.9), idea("NU", accion="SELL", peso=0, conf=0.8)]})
    c = ctx(universe, t, guard="pérdida de -12.0% de su cartera")
    sigs = await strat(client).on_daily(c)
    assert [(s.ticker, s.side) for s in sigs] == [("NU", "SELL")]
    assert any("en pausa" in n for n in c.notes)
