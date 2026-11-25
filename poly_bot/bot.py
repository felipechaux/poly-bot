"""
Bot orchestrator — two daily jobs (Bogotá time, weekdays).

Decide (08:00, before the open):
  prices (yfinance) → mark-to-market → dividends → settle anything fillable → strategies
  → risk → BVC/US orders queued as pending (crypto fills now) → snapshot → ntfy summary
Settle (16:30, after the close):
  pending orders fill at the opening price of the first session after the decision
  → snapshot → short ntfy push with the fills

`execution.fill: close` restores the old behaviour (fill everything at the last close).

State lives in the SQLite transaction ledger and is replayed on startup.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from poly_bot.analytics.scorecard import (
    basket_return,
    guard_reason,
    semaforo_for,
    sleeve_stats,
)
from poly_bot.config.settings import Settings
from poly_bot.execution.models import OrderRequest, Transaction
from poly_bot.execution.paper_executor import PaperExecutor
from poly_bot.execution.sessions import fills_at_open, next_open_fill
from poly_bot.market_data.provider import MarketData, PriceProvider, YFinanceProvider
from poly_bot.market_data.universe import Universe
from poly_bot.notify.ntfy import NtfyNotifier
from poly_bot.notify.summary import SummaryInput, build_daily_summary
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot
from poly_bot.portfolio.pnl import fmt_money
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.risk.manager import RiskManager
from poly_bot.store.database import init_db
from poly_bot.store.trade_store import TradeStore
from poly_bot.strategies.base import DailyContext, Signal, Strategy
from poly_bot.strategies.llm_analyst import LLMAnalystStrategy
from poly_bot.strategies.registry import load_strategies

log = get_logger(__name__)

FX_MIN_USD = 10.0  # don't bother converting less than this
SCORE_HORIZONS = (1, 7, 30)  # days after a recommendation to score it


@dataclass
class CycleResult:
    day: date
    dry_run: bool
    executed: list[Transaction] = field(default_factory=list)
    rejected: list[tuple[Signal, str]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)  # queued for the next open
    title: str = ""
    message: str = ""
    notified: bool = False


class Bot:
    def __init__(self, settings: Settings, provider: PriceProvider | None = None) -> None:
        self._settings = settings
        self.tz = ZoneInfo(settings.schedule.get("timezone", "America/Bogota"))
        self.universe = Universe.from_config(settings.universe)
        md_cfg = settings.market_data
        self._provider: PriceProvider = provider or YFinanceProvider(
            fx_ticker=md_cfg.get("fx_ticker", "COP=X"),
            history_period=md_cfg.get("history_period", "2y"),
            cache_ttl_minutes=float(md_cfg.get("cache_ttl_minutes", 30)),
        )
        self.executor = PaperExecutor(settings.costs)
        ex = settings.execution or {}
        self.fill_mode: str = str(ex.get("fill", "close"))  # close | next_open
        self.settle_time: str = str(ex.get("settle_time", "16:30"))
        self.pending_max_days = int(ex.get("pending_max_days", 3))
        self.risk = RiskManager.from_config(self.universe, settings.risk, md_cfg)
        self.tracker = PositionTracker(base_currency=settings.base_currency)
        self.strategies: list[Strategy] = load_strategies(settings.strategies)
        for s in self.strategies:
            s.validate(self.universe)
        self.notifier = NtfyNotifier(
            settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token, settings.dashboard_url
        )
        self.store: TradeStore | None = None
        self._conn: Any = None
        self._running = False
        self._cycle_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.last_market: MarketData | None = None
        self.last_cycle: CycleResult | None = None
        self.next_run_at: datetime | None = None
        self.next_job: str = "decide"

    @property
    def mode(self) -> str:
        return self._settings.mode

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open DB, rebuild portfolio from the ledger, seed initial cash if new. Idempotent."""
        async with self._start_lock:
            if self.store is None:
                await self._start()

    async def _start(self) -> None:
        self._conn = await init_db(self._settings.db_path)
        self.store = TradeStore(self._conn)
        self.tracker.replay(await self.store.all_transactions())
        if not self.tracker.has_deposits:
            for ccy, amount in self._settings.initial_cash.items():
                if amount > 0:
                    await self._record(Transaction(
                        kind="DEPOSIT", currency=ccy, gross=amount,  # type: ignore[arg-type]
                        rationale="Capital inicial ficticio",
                    ))
        # Warm prices from the last run so the dashboard works before the first cycle
        quotes = await self.store.load_quotes()
        snaps = await self.store.snapshots(limit=1)
        if snaps:
            self.tracker.update_prices(quotes, snaps[-1]["usdcop"])
        for s in self.strategies:
            await s.on_start()

    async def run(self) -> None:
        await self.start()
        self._running = True
        log.info("bot.started", strategies=[s.name for s in self.strategies],
                 universe=len(self.universe), notify=self.notifier.enabled)
        try:
            assert self.store is not None
            if await self.store.pending_orders():
                await self._safe_settle()  # fills anything whose opening price is now known
            if self._settings.schedule.get("run_on_start", True) and await self._missed_today():
                await self._safe_cycle()
            while self._running:
                self.next_run_at, self.next_job = self._next_job(datetime.now(self.tz))
                log.info("bot.next_cycle", at=self.next_run_at.isoformat(), job=self.next_job)
                while self._running and datetime.now(self.tz) < self.next_run_at:
                    remaining = (self.next_run_at - datetime.now(self.tz)).total_seconds()
                    await asyncio.sleep(max(0.5, min(remaining, 30.0)))
                if self._running:
                    if self.next_job == "settle":
                        await self._safe_settle()
                    else:
                        await self._safe_cycle()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._running = False

    async def _shutdown(self) -> None:
        for s in self.strategies:
            await s.on_stop()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self.store = None
        log.info("bot.stopped")

    def _next_at(self, now: datetime, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        weekdays_only = self._settings.schedule.get("weekdays_only", True)
        candidate = datetime.combine(now.date(), time(hh, mm), tzinfo=self.tz)
        if candidate <= now:
            candidate += timedelta(days=1)
        while weekdays_only and candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    def _next_run_time(self, now: datetime) -> datetime:
        return self._next_at(now, str(self._settings.schedule.get("daily_run_time", "17:00")))

    def _next_job(self, now: datetime) -> tuple[datetime, str]:
        decide = self._next_run_time(now)
        if self.fill_mode != "next_open":
            return decide, "decide"
        settle = self._next_at(now, self.settle_time)
        return (settle, "settle") if settle < decide else (decide, "decide")

    async def _missed_today(self) -> bool:
        """True if today's scheduled time has passed and the cycle hasn't run yet.

        Avoids re-notifying on every restart and running early (e.g. a 6am restart)."""
        assert self.store is not None
        now = datetime.now(self.tz)
        snaps = await self.store.snapshots(limit=1)
        if snaps and snaps[-1]["day"] == now.date().isoformat():
            return False
        if not snaps:
            return True  # brand-new portfolio: allocate right away
        hh, mm = (int(x) for x in str(self._settings.schedule.get("daily_run_time", "08:00")).split(":"))
        weekdays_only = self._settings.schedule.get("weekdays_only", True)
        if weekdays_only and now.weekday() >= 5:
            return False
        return now.time() >= time(hh, mm)

    async def _safe_settle(self) -> None:
        try:
            await self.settle()
        except Exception as exc:
            log.exception("bot.settle_failed", error=str(exc))
            if self.store:
                await self.store.save_event("error", reasoning=f"settle: {exc}")
            await self.notifier.send(f"La ejecución de órdenes falló: {exc}",
                                     title="⚠️ Invest bot: error", tags=["warning"], priority=4)

    async def _safe_cycle(self) -> None:
        try:
            await self.run_cycle()
        except Exception as exc:  # keep the scheduler alive; report the failure
            log.exception("bot.cycle_failed", error=str(exc))
            if self.store:
                await self.store.save_event("error", reasoning=str(exc))
            await self.notifier.send(f"El ciclo diario falló: {exc}", title="⚠️ Invest bot: error",
                                     tags=["warning"], priority=4)

    # ------------------------------------------------------------------
    # Daily cycle
    # ------------------------------------------------------------------

    def snapshot(self) -> PortfolioSnapshot | None:
        return self.tracker.snapshot() if self.tracker.usdcop else None

    async def run_cycle(self, dry_run: bool = False, notify: bool = True) -> CycleResult:
        await self.start()
        assert self.store is not None
        async with self._cycle_lock:
            today = datetime.now(self.tz).date()
            cycle_start = datetime.now(timezone.utc)
            result = CycleResult(day=today, dry_run=dry_run)

            md = await self._provider.fetch(list(self.universe))
            self.last_market = md
            tracker = copy.deepcopy(self.tracker) if dry_run else self.tracker
            tracker.update_prices(md.quotes, md.usdcop)
            if not dry_run:
                await self.store.save_quotes(md.quotes)
                await self._credit_dividends(tracker, md, today, result)

            if not dry_run:
                await self._settle_pending(md, tracker, today, result)
                await self._score_recommendations(md, today)

            strategy_notes: list[str] = []
            for strategy in self.strategies:
                last = await self.store.last_event_time(strategy.name, "signals")
                ctx = DailyContext(
                    today=today, quotes=md.quotes, usdcop=md.usdcop,
                    portfolio=tracker.snapshot(), universe=self.universe,
                    last_signal_date=last.astimezone(self.tz).date() if last else None,
                    extra={
                        "dry_run": dry_run,
                        "cached_analysis": await self._todays_event(strategy.name, "analysis", today),
                        "track_record": await self._track_record(strategy.name),
                        "guard": await self._guard(strategy, tracker),
                    },
                )
                signals = await strategy.on_daily(ctx)
                strategy_notes += ctx.notes
                if not dry_run:
                    for ev in ctx.events:
                        await self.store.save_event(**ev)
                    if self.fill_mode == "next_open":
                        await self.store.supersede_pending(strategy.name)
                if not signals:
                    continue
                log.info("bot.signals", strategy=strategy.name, count=len(signals))
                if not dry_run:
                    await self.store.save_event(
                        "signals", strategy=strategy.name,
                        reasoning=signals[0].rationale.split(":")[0],
                        payload={"signals": [s.__dict__ for s in signals]},
                    )
                await self._execute(strategy.name, signals, tracker, md, today, result, dry_run)

            snap = tracker.snapshot()
            benchmarks = {a.ticker: md.quotes[a.ticker].price
                          for a in self.universe.benchmarks if a.ticker in md.quotes}
            if not dry_run:
                await self.store.save_snapshot(today, snap, benchmarks)

            # Summary
            txs_today = [t.model_dump() for t in result.executed] if dry_run else \
                await self.store.transactions_since(cycle_start)
            notes = [f"⛔ Rechazadas {len(result.rejected)}: " + "; ".join(
                f"{s.ticker}: {r}" for s, r in result.rejected[:3])] if result.rejected else []
            notes = strategy_notes + notes + self._pending_notes(result.pending)
            if dry_run:
                notes.insert(0, "🧪 DRY RUN — nada se ejecutó")
            inp = SummaryInput(
                snap=snap, quotes=md.quotes, universe=self.universe,
                prev_snapshot=await self.store.snapshot_before(today),
                first_snapshot=await self.store.first_snapshot(),
                benchmarks_now=benchmarks, transactions_today=txs_today, notes=notes,
            )
            result.title, result.message, tags = build_daily_summary(inp)
            if notify and not dry_run:
                result.notified = await self.notifier.send(result.message, title=result.title, tags=tags)
                await self.store.save_event("cycle", payload={
                    "executed": len(result.executed), "rejected": len(result.rejected),
                    "value_base": snap.total_value(), "notified": result.notified,
                })
            if not dry_run:
                self.last_cycle = result
            log.info("bot.cycle_done", day=str(today), executed=len(result.executed),
                     rejected=len(result.rejected), value_cop=round(snap.total_value("COP")),
                     dry_run=dry_run)
            return result

    # ------------------------------------------------------------------
    # Pending orders → fills at the next open
    # ------------------------------------------------------------------

    async def settle(self, notify: bool = True) -> CycleResult:
        """After-close job: fill pending orders at their session's opening price."""
        await self.start()
        assert self.store is not None
        async with self._cycle_lock:
            today = datetime.now(self.tz).date()
            result = CycleResult(day=today, dry_run=False)
            if not await self.store.pending_orders():
                return result
            md = await self._provider.fetch(list(self.universe))
            self.last_market = md
            self.tracker.update_prices(md.quotes, md.usdcop)
            await self.store.save_quotes(md.quotes)
            await self._settle_pending(md, self.tracker, today, result)
            if result.executed or result.rejected:
                snap = self.tracker.snapshot()
                benchmarks = {a.ticker: md.quotes[a.ticker].price
                              for a in self.universe.benchmarks if a.ticker in md.quotes}
                await self.store.save_snapshot(today, snap, benchmarks)
                result.title, result.message = self._settle_message(result, snap)
                if notify:
                    result.notified = await self.notifier.send(result.message, title=result.title,
                                                               tags=["white_check_mark"])
                await self.store.save_event("settle", payload={
                    "executed": len(result.executed), "rejected": len(result.rejected),
                    "notified": result.notified})
            log.info("bot.settle_done", executed=len(result.executed), rejected=len(result.rejected))
            return result

    async def _settle_pending(self, md: MarketData, tracker: PositionTracker, today: date,
                              result: CycleResult) -> None:
        assert self.store is not None
        orders = await self.store.pending_orders()
        by_strategy: dict[str, list[tuple[dict[str, Any], Signal]]] = {}
        quotes = dict(md.quotes)
        for o in orders:
            asset = self.universe.get(o["ticker"])
            if asset is None:
                await self.store.close_pending(o["id"], "rejected", "fuera del universo")
                continue
            decided = datetime.fromisoformat(o["decided_at"])
            fill = next_open_fill(asset.market, decided, md.opens.get(o["ticker"]))
            if fill is None:
                if _business_days(decided.astimezone(self.tz).date(), today) > self.pending_max_days:
                    await self.store.close_pending(o["id"], "expired", "sin apertura disponible")
                continue
            day, price = fill
            base = md.quotes.get(o["ticker"])
            if base is None:
                continue
            quotes[o["ticker"]] = base.model_copy(update={"price": price, "as_of": day})
            sig = Signal(o["ticker"], o["side"], value=o["value"], quantity=o["quantity"],
                         rationale=o["rationale"])
            by_strategy.setdefault(o["strategy"], []).append((o, sig))
        if not by_strategy:
            return
        md_fill = MarketData(quotes=quotes, dividends=md.dividends, usdcop=md.usdcop,
                             usdcop_as_of=md.usdcop_as_of, closes=md.closes, opens=md.opens)
        for strategy, items in by_strategy.items():
            outcomes = await self._fill_now(strategy, [sig for _, sig in items], tracker,
                                            md_fill, today, result, dry_run=False)
            for o, sig in items:
                tx, reason = outcomes.get(id(sig), (None, "no ejecutada"))
                fill_day = quotes[o["ticker"]].as_of
                if tx is not None:
                    await self.store.close_pending(o["id"], "filled", f"apertura {fill_day}",
                                                   tx_id=tx.tx_id, fill_day=fill_day)
                else:
                    await self.store.close_pending(o["id"], "rejected", reason, fill_day=fill_day)

    def _pending_notes(self, pending: list[dict[str, Any]]) -> list[str]:
        if not pending:
            return []
        lines = [f"📝 Órdenes para la apertura ({len(pending)}), se ejecutan al precio de apertura:"]
        for o in pending[:8]:
            amount = fmt_money(o["value"], o["currency"]) if o["value"] else f"{o['quantity']:g} u."
            lines.append(f"  • {'Compra' if o['side'] == 'BUY' else 'Venta'} {o['ticker']} {amount}"
                         f" · {o['strategy']}")
        if len(pending) > 8:
            lines.append(f"  … y {len(pending) - 8} más")
        return lines

    def _settle_message(self, result: CycleResult, snap: PortfolioSnapshot) -> tuple[str, str]:
        title = f"✅ Órdenes ejecutadas: {len([t for t in result.executed if t.kind != 'FX'])}"
        lines = []
        for t in result.executed:
            if t.kind == "FX":
                continue
            lines.append(f"• {'Compra' if t.kind == 'BUY' else 'Venta'} {t.ticker} {t.quantity:g} @ "
                         f"{t.price:,.2f} = {fmt_money(t.gross, t.currency)} · {t.strategy}")
        if result.rejected:
            lines.append(f"⛔ Rechazadas {len(result.rejected)}: " + "; ".join(
                f"{sg.ticker}: {r}" for sg, r in result.rejected[:3]))
        lines.append(f"💼 Cartera: {fmt_money(snap.total_value(), 'COP')}")
        lines.append("— Simulado (paper), precio de apertura. No es asesoría financiera.")
        return title, "\n".join(lines)

    # ------------------------------------------------------------------
    # LLM decision journal
    # ------------------------------------------------------------------

    async def _todays_event(self, strategy: str, event_type: str, today: date) -> dict[str, Any] | None:
        assert self.store is not None
        ev = await self.store.last_event(strategy, event_type)
        if ev and datetime.fromisoformat(ev["occurred_at"]).astimezone(self.tz).date() == today:
            payload: dict[str, Any] = ev["payload"]
            return payload
        return None

    async def _score_recommendations(self, md: MarketData, today: date) -> None:
        """Record 1/7/30-day outcomes of past recommendations (executed or not)."""
        assert self.store is not None
        since = datetime.now(timezone.utc) - timedelta(days=max(SCORE_HORIZONS) + 10)
        recs = await self.store.events("recommendation", since=since)
        baskets = {s.name: [t for t in self.universe.tickers if t not in s.excluded]
                   for s in self.strategies if isinstance(s, LLMAnalystStrategy)}
        done = {(o["payload"].get("rec_id"), o["payload"].get("horizon_days"))
                for o in await self.store.events("outcome", since=since)}
        for rec in recs:
            p = rec["payload"]
            quote = md.quotes.get(rec["ticker"])
            if quote is None or not p.get("price_at"):
                continue
            age = (today - datetime.fromisoformat(rec["occurred_at"]).astimezone(self.tz).date()).days
            for h in SCORE_HORIZONS:
                if age < h or (rec["id"], h) in done:
                    continue
                ret = quote.price / float(p["price_at"]) - 1
                hit = ret > 0 if rec["decision"] == "BUY" else ret < 0
                start = date.fromisoformat(p["as_of"]) if p.get("as_of") else \
                    datetime.fromisoformat(rec["occurred_at"]).astimezone(self.tz).date() - timedelta(days=1)
                basket = basket_return(md.closes, baskets.get(rec["strategy"], self.universe.tickers), start)
                excess = None if basket is None else (ret - basket if rec["decision"] == "BUY" else basket - ret)
                await self.store.save_event(
                    "outcome", strategy=rec["strategy"], ticker=rec["ticker"], decision=rec["decision"],
                    payload={"rec_id": rec["id"], "horizon_days": h, "ret": ret, "hit": hit,
                             "basket_ret": basket, "excess": excess,
                             "planned": p.get("planned", False), "confianza": p.get("confianza")},
                )

    async def _analyst_status(self, strategy: LLMAnalystStrategy,
                              snap: PortfolioSnapshot | None = None) -> dict[str, Any]:
        """Semáforo + sleeve P&L + guard for one analyst."""
        assert self.store is not None
        outcomes = await self.store.events("outcome", strategy=strategy.name)
        cfg = strategy.semaforo_cfg
        light = semaforo_for(outcomes, strategy.name, horizon_days=int(cfg["horizon_days"]),
                             min_n=int(cfg["min_n"]), t_threshold=float(cfg["t_threshold"]))
        sleeve = sleeve_stats(strategy.name, snap or self.tracker.snapshot(),
                              await self.store.all_transactions(), strategy.p["sleeve_pct"])
        return {"semaforo": light, "sleeve": sleeve,
                "guard": guard_reason(sleeve, light, strategy.p["max_drawdown_pct"])}

    async def _guard(self, strategy: Strategy, tracker: PositionTracker) -> str | None:
        if not isinstance(strategy, LLMAnalystStrategy):
            return None
        status = await self._analyst_status(strategy, tracker.snapshot())
        reason: str | None = status["guard"]
        if reason:
            log.warning("analyst.paused", strategy=strategy.name, reason=reason)
        return reason

    async def _track_record(self, strategy: str | None = None, limit: int = 8) -> str:
        """Compact text of recent recommendations + 7-day outcomes, fed back to the LLM."""
        assert self.store is not None
        since = datetime.now(timezone.utc) - timedelta(days=60)
        outcomes = {(o["payload"]["rec_id"], o["payload"]["horizon_days"]): o["payload"]
                    for o in await self.store.events("outcome", since=since)}
        recs = (await self.store.events("recommendation", since=since, strategy=strategy))[-limit:]
        lines = []
        for r in recs:
            day = r["occurred_at"][:10]
            res = []
            for h in SCORE_HORIZONS:
                o = outcomes.get((r["id"], h))
                if o:
                    res.append(f"{h}d {o['ret'] * 100:+.1f}% {'✓' if o['hit'] else '✗'}")
            lines.append(f"- {day} {r['decision']} {r['ticker']} (conf {r['payload'].get('confianza')}): "
                         + (", ".join(res) or "sin resultado aún"))
        return "\n".join(lines)

    async def _credit_dividends(self, tracker: PositionTracker, md: MarketData,
                                today: date, result: CycleResult) -> None:
        assert self.store is not None
        for pos in list(tracker.snapshot().positions):
            asset = self.universe.get(pos.ticker)
            if asset is None:
                continue
            opened = pos.opened_at.astimezone(self.tz).date()
            for d in md.dividends.get(pos.ticker, []):
                if not (opened < d.ex_date <= today):
                    continue
                if await self.store.dividend_recorded(pos.ticker, d.ex_date):
                    continue
                gross = pos.quantity * d.amount_per_share
                withholding = self.executor.dividend_withholding.get(asset.market, 0.0)
                tx = Transaction(
                    kind="DIVIDEND", currency=asset.currency, ticker=pos.ticker,
                    quantity=pos.quantity, price=d.amount_per_share, gross=gross,
                    fee=gross * withholding, strategy=pos.strategy,
                    rationale=f"Dividendo ex {d.ex_date} (retención {withholding:.0%})",
                    occurred_at=datetime.combine(d.ex_date, time(0), tzinfo=timezone.utc),
                )
                await self._record(tx, tracker)
                result.executed.append(tx)

    async def _execute(self, strategy: str, signals: list[Signal], tracker: PositionTracker,
                       md: MarketData, today: date, result: CycleResult, dry_run: bool) -> None:
        if self.fill_mode == "next_open":
            deferred = [s for s in signals if (a := self.universe.get(s.ticker)) is not None
                        and fills_at_open(a.market)]
            signals = [s for s in signals if s not in deferred]
            await self._queue(strategy, deferred, md, result, dry_run)
        await self._fill_now(strategy, signals, tracker, md, today, result, dry_run)

    async def _queue(self, strategy: str, signals: list[Signal], md: MarketData,
                     result: CycleResult, dry_run: bool) -> None:
        now = datetime.now(timezone.utc)
        for sig in signals:
            order = {"strategy": strategy, "ticker": sig.ticker, "side": sig.side,
                     "value": sig.value, "quantity": sig.quantity, "rationale": sig.rationale,
                     "currency": self.universe.require(sig.ticker).currency}
            if not dry_run and self.store is not None:
                order["id"] = await self.store.add_pending(now, strategy, sig.ticker, sig.side,
                                                           sig.value, sig.quantity, sig.rationale)
            result.pending.append(order)
            log.info("order.queued", strategy=strategy, ticker=sig.ticker, side=sig.side)

    async def _fill_now(self, strategy: str, signals: list[Signal], tracker: PositionTracker,
                        md: MarketData, today: date, result: CycleResult,
                        dry_run: bool) -> dict[int, tuple[Transaction | None, str]]:
        """Execute signals at the quotes in `md`; returns {id(signal): (tx, rejection reason)}."""
        out: dict[int, tuple[Transaction | None, str]] = {}
        sells = [s for s in signals if s.side == "SELL"]
        buys = [s for s in signals if s.side == "BUY"]
        for sig in sells:
            out[id(sig)] = await self._execute_one(strategy, sig, tracker, md, today, result, dry_run)
        if buys:
            await self._fund_buys(strategy, buys, tracker, md, result, dry_run)
        for sig in buys:
            out[id(sig)] = await self._execute_one(strategy, sig, tracker, md, today, result, dry_run)
        return out

    async def _execute_one(self, strategy: str, sig: Signal, tracker: PositionTracker,
                           md: MarketData, today: date, result: CycleResult,
                           dry_run: bool) -> tuple[Transaction | None, str]:
        req = OrderRequest(ticker=sig.ticker, side=sig.side, value=sig.value,
                           quantity=sig.quantity, strategy=strategy, rationale=sig.rationale)
        quote = md.quotes.get(sig.ticker)
        ok, reason = self.risk.check(req, quote, tracker.snapshot(), today)
        if not ok or quote is None:
            result.rejected.append((sig, reason))
            log.info("risk.rejected", ticker=sig.ticker, side=sig.side, reason=reason)
            if not dry_run and self.store:
                await self.store.save_event("rejected", strategy=strategy, ticker=sig.ticker,
                                            decision=sig.side, reasoning=reason)
            return None, reason
        asset = self.universe.require(sig.ticker)
        report = self.executor.execute(
            req, asset, quote,
            cash_available=max(0.0, tracker.cash.get(asset.currency, 0.0)),
            held_quantity=tracker.held_quantity(sig.ticker),
        )
        if not report.is_filled or report.transaction is None:
            result.rejected.append((sig, report.rejection_reason))
            return None, report.rejection_reason
        await self._record(report.transaction, tracker, persist=not dry_run)
        result.executed.append(report.transaction)
        return report.transaction, ""

    async def _fund_buys(self, strategy: str, buys: list[Signal], tracker: PositionTracker,
                         md: MarketData, result: CycleResult, dry_run: bool) -> None:
        """Convert COP↔USD so each currency has enough cash for its buys."""
        need: dict[str, float] = {"COP": 0.0, "USD": 0.0}
        for sig in buys:
            asset = self.universe.get(sig.ticker)
            if asset is None or sig.value is None:
                continue
            c = self.executor.costs_for(asset)
            need[asset.currency] += sig.value * (1 + c.slippage_pct) * (1 + c.commission_pct) \
                + c.min_commission
        spare = {ccy: tracker.cash.get(ccy, 0.0) - need[ccy] for ccy in need}
        for to_ccy, from_ccy in (("USD", "COP"), ("COP", "USD")):
            shortfall = -spare[to_ccy]
            if shortfall <= 0 or spare[from_ccy] <= 0:
                continue
            from_needed = self.executor.from_amount_for(from_ccy, shortfall, md.usdcop)  # type: ignore[arg-type]
            from_amount = min(from_needed, spare[from_ccy])
            usd_equiv = from_amount if from_ccy == "USD" else from_amount / md.usdcop
            if usd_equiv < FX_MIN_USD:
                continue
            tx = self.executor.convert(
                from_ccy, to_ccy, from_amount, md.usdcop,  # type: ignore[arg-type]
                strategy=strategy, rationale=f"Fondear compras en {to_ccy}",
            )
            await self._record(tx, tracker, persist=not dry_run)
            result.executed.append(tx)
            spare[from_ccy] -= from_amount
            spare[to_ccy] += tx.gross

    async def _record(self, tx: Transaction, tracker: PositionTracker | None = None,
                      persist: bool = True) -> None:
        (tracker or self.tracker).apply(tx)
        if persist and self.store is not None:
            await self.store.save_transaction(tx)


def _business_days(start: date, end: date) -> int:
    """Weekdays strictly after `start` up to and including `end`."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n
