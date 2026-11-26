"""
Bot orchestrator — two daily jobs (Bogotá time, weekdays).

Decide (08:00, before the open):
  prices (yfinance) → mark-to-market → dividends → settle anything fillable → strategies
  → risk → BVC/US orders queued as pending (crypto fills now) → snapshot → ntfy summary
Settle (16:30, after the close):
  pending orders fill at the opening price of the first session after the decision
  → snapshot → short ntfy push (with the fills, or "sin órdenes" so the day always closes)
Scans (`schedule.scans`, e.g. London open and the London–NY overlap):
  only the LLM analysts re-run with fresh prices + news → crypto fills now, BVC/US queue for the
  next open → push only when something was decided. No snapshot (the 08:00 decide stays the
  day's main cycle).

Crypto fills can be mirrored on Binance Spot Testnet (`execution.binance_testnet`).

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
from poly_bot.execution.binance_testnet import BinanceError, BinanceTestnet
from poly_bot.execution.models import OrderRequest, Transaction
from poly_bot.execution.paper_executor import PaperExecutor
from poly_bot.execution.sessions import fills_at_open, next_open_fill, next_session_open
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


@dataclass(frozen=True)
class Scan:
    """An extra intraday analyst run, scheduled in its own market's timezone."""

    name: str
    label: str
    at: str  # "HH:MM" in `tz`
    tz: str

    @classmethod
    def parse(cls, raw: list[dict[str, Any]] | None) -> list["Scan"]:
        return [cls(name=str(r["name"]), label=str(r.get("label") or r["name"]),
                    at=str(r["at"]), tz=str(r.get("tz", "America/Bogota")))
                for r in raw or [] if r.get("enabled", True)]


@dataclass
class CycleResult:
    day: date
    dry_run: bool
    executed: list[Transaction] = field(default_factory=list)
    rejected: list[tuple[Signal, str]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)  # queued for the next open
    notes: list[str] = field(default_factory=list)  # execution warnings for the summary
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
        self.scans: list[Scan] = Scan.parse(settings.schedule.get("scans"))
        bn = ex.get("binance_testnet") or {}
        self.binance: BinanceTestnet | None = None
        has_keys = bool(settings.binance_testnet_api_key and settings.binance_testnet_api_secret)
        # off: disabled in config · no_keys: enabled but .env lacks keys · on: mirroring
        self.binance_state = "off" if not bn.get("enabled") else "on" if has_keys else "no_keys"
        if self.binance_state == "on":
            self.binance = BinanceTestnet(
                settings.binance_testnet_api_key, settings.binance_testnet_api_secret,
                base_url=str(bn.get("base_url", "https://testnet.binance.vision")),
                max_deviation_pct=float(bn.get("max_deviation_pct", 0.02)))
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
                 universe=len(self.universe), notify=self.notifier.enabled,
                 scans=[sc.name for sc in self.scans], binance_testnet=self.binance is not None)
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
                        await self._safe_settle(scheduled=True)
                    elif self.next_job.startswith("scan:"):
                        await self._safe_scan(self.next_job.removeprefix("scan:"))
                    else:
                        await self._safe_cycle()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._running = False

    async def _shutdown(self) -> None:
        for s in self.strategies:
            await s.on_stop()
        if self.binance is not None:
            await self.binance.close()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self.store = None
        log.info("bot.stopped")

    def _next_at(self, now: datetime, hhmm: str, tz: ZoneInfo | None = None) -> datetime:
        """Next weekday HH:MM in `tz` (default: the bot's), returned in the bot's timezone."""
        tz = tz or self.tz
        hh, mm = (int(x) for x in hhmm.split(":"))
        weekdays_only = self._settings.schedule.get("weekdays_only", True)
        local = now.astimezone(tz)
        day = local.date()
        while True:
            candidate = datetime.combine(day, time(hh, mm), tzinfo=tz)
            if candidate > local and not (weekdays_only and candidate.weekday() >= 5):
                return candidate.astimezone(self.tz)
            day += timedelta(days=1)

    def _next_run_time(self, now: datetime) -> datetime:
        return self._next_at(now, str(self._settings.schedule.get("daily_run_time", "17:00")))

    def _next_job(self, now: datetime) -> tuple[datetime, str]:
        decide = self._next_run_time(now)
        if self.fill_mode != "next_open":
            return decide, "decide"
        jobs = [(decide, "decide"), (self._next_at(now, self.settle_time), "settle")]
        jobs += [(self._next_at(now, sc.at, ZoneInfo(sc.tz)), f"scan:{sc.name}") for sc in self.scans]
        return min(jobs, key=lambda j: j[0])

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

    async def _safe_scan(self, name: str) -> None:
        scan = next((sc for sc in self.scans if sc.name == name), None)
        if scan is None:
            return
        try:
            await self.run_cycle(scan=scan)
        except Exception as exc:
            log.exception("bot.scan_failed", scan=name, error=str(exc))
            if self.store:
                await self.store.save_event("error", reasoning=f"scan {name}: {exc}")
            await self.notifier.send(f"El escaneo «{scan.label}» falló: {exc}",
                                     title="⚠️ Invest bot: error", tags=["warning"], priority=3)

    async def _safe_settle(self, scheduled: bool = False) -> None:
        try:
            await self.settle(scheduled=scheduled)
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

    async def run_cycle(self, dry_run: bool = False, notify: bool = True,
                        scan: Scan | None = None) -> CycleResult:
        """The 08:00 decide cycle, or (with `scan`) an intraday analysts-only scan."""
        await self.start()
        assert self.store is not None
        async with self._cycle_lock:
            today = datetime.now(self.tz).date()
            cycle_start = datetime.now(timezone.utc)
            result = CycleResult(day=today, dry_run=dry_run)
            slot = f"scan:{scan.name}" if scan else "decide"

            md = await self._provider.fetch(list(self.universe))
            self.last_market = md
            tracker = copy.deepcopy(self.tracker) if dry_run else self.tracker
            tracker.update_prices(md.quotes, md.usdcop)
            if not dry_run:
                await self.store.save_quotes(md.quotes)
            if not dry_run and scan is None:
                await self._credit_dividends(tracker, md, today, result)
                await self._settle_pending(md, tracker, today, result)
                await self._score_recommendations(md, today)

            strategies = [st for st in self.strategies
                          if scan is None or isinstance(st, LLMAnalystStrategy)]
            strategy_notes: list[str] = []
            for strategy in strategies:
                last = await self.store.last_event_time(strategy.name, "signals")
                # Scans only add ideas: every queued order stays (the analyst doesn't see them, so a
                # scan must not cancel the morning's decision); decide re-runs replace the orders
                # that haven't reached their open yet.
                locked = await self._locked_pending(strategy.name, all_pending=scan is not None)
                ctx = DailyContext(
                    today=today, quotes=md.quotes, usdcop=md.usdcop,
                    portfolio=tracker.snapshot(), universe=self.universe,
                    last_signal_date=last.astimezone(self.tz).date() if last else None,
                    extra={
                        "dry_run": dry_run,
                        "slot": slot,
                        "session": scan.label if scan else "",
                        "cached_analysis": await self._todays_analysis(strategy.name, today, slot),
                        "track_record": await self._track_record(strategy.name),
                        "guard": await self._guard(strategy, tracker),
                        "new_today": self._new_today(strategy.name, tracker, today, locked),
                        "locked": {o["ticker"] for o in locked},
                    },
                )
                signals = await strategy.on_daily(ctx)
                strategy_notes += ctx.notes
                if not dry_run:
                    for ev in ctx.events:
                        await self.store.save_event(**ev)
                    if self.fill_mode == "next_open" and scan is None:
                        await self.store.supersede_pending(strategy.name,
                                                           keep={o["id"] for o in locked})
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
            if scan is not None:
                return await self._finish_scan(scan, result, snap, strategy_notes + result.notes,
                                               notify, dry_run)
            benchmarks = {a.ticker: md.quotes[a.ticker].price
                          for a in self.universe.benchmarks if a.ticker in md.quotes}
            if not dry_run:
                await self.store.save_snapshot(today, snap, benchmarks)

            # Summary
            txs_today = [t.model_dump() for t in result.executed] if dry_run else \
                await self.store.transactions_since(cycle_start)
            notes = [f"⛔ Rechazadas {len(result.rejected)}: " + "; ".join(
                f"{s.ticker}: {r}" for s, r in result.rejected[:3])] if result.rejected else []
            notes = strategy_notes + result.notes + notes + self._pending_notes(result.pending)
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

    async def _finish_scan(self, scan: Scan, result: CycleResult, snap: PortfolioSnapshot,
                           notes: list[str], notify: bool, dry_run: bool) -> CycleResult:
        """Scans push only when they decided something (or an analyst failed)."""
        assert self.store is not None
        trades = [t for t in result.executed if t.kind != "FX"]
        lines = [f"• {'Compra' if t.kind == 'BUY' else 'Venta'} {t.ticker} {t.quantity:g} @ "
                 f"{t.price:,.2f} = {fmt_money(t.gross, t.currency)} · {t.strategy}" for t in trades]
        if result.rejected:
            lines.append(f"⛔ Rechazadas {len(result.rejected)}: " + "; ".join(
                f"{sg.ticker}: {r}" for sg, r in result.rejected[:3]))
        lines += self._pending_notes(result.pending)
        failed = [n for n in notes if n.startswith("⚠️")]
        lines += failed
        worth = bool(trades or result.pending or result.rejected or failed)
        result.title = (f"🌍 {scan.label}: {len(trades)} ejecutadas, {len(result.pending)} para "
                        f"la apertura" if worth else f"🌍 {scan.label}: sin cambios")
        lines += [n for n in notes if not n.startswith("⚠️")][:6]
        lines.append(f"💼 Cartera: {fmt_money(snap.total_value(), 'COP')}")
        lines.append("— Simulado (paper). No es asesoría financiera.")
        result.message = "\n".join(lines)
        if notify and not dry_run and worth:
            result.notified = await self.notifier.send(result.message, title=result.title,
                                                       tags=["globe_with_meridians"], priority=2)
        if not dry_run:
            await self.store.save_event("scan", strategy="", reasoning=scan.name, payload={
                "scan": scan.name, "executed": len(trades), "pending": len(result.pending),
                "rejected": len(result.rejected), "notified": result.notified})
        log.info("bot.scan_done", scan=scan.name, executed=len(trades),
                 pending=len(result.pending), rejected=len(result.rejected), dry_run=dry_run)
        return result

    async def _locked_pending(self, strategy: str, all_pending: bool = False) -> list[dict[str, Any]]:
        """Pending orders a new decision must neither cancel nor duplicate: those whose opening
        session already started (they fill at that open), or all of them (`all_pending`)."""
        assert self.store is not None
        now = datetime.now(timezone.utc)
        out = []
        for o in await self.store.pending_orders():
            asset = self.universe.get(o["ticker"])
            if o["strategy"] != strategy or asset is None:
                continue
            if all_pending:
                out.append(o)
                continue
            opened = next_session_open(asset.market, datetime.fromisoformat(o["decided_at"]))
            if opened is not None and opened <= now:
                out.append(o)
        return out

    def _new_today(self, strategy: str, tracker: PositionTracker, today: date,
                   kept: list[dict[str, Any]]) -> int:
        """New positions this strategy already opened today, plus queued new entries that this
        run keeps — so the daily entry cap holds across the decide cycle and the scans."""
        positions = [p for p in tracker.snapshot().positions if p.strategy == strategy]
        held = {p.ticker for p in positions}
        opened = {p.ticker for p in positions if p.opened_at.astimezone(self.tz).date() == today}
        queued = {o["ticker"] for o in kept if o["side"] == "BUY" and o["ticker"] not in held
                  and datetime.fromisoformat(o["decided_at"]).astimezone(self.tz).date() == today}
        return len(opened | queued)

    # ------------------------------------------------------------------
    # Pending orders → fills at the next open
    # ------------------------------------------------------------------

    async def settle(self, notify: bool = True, scheduled: bool = False) -> CycleResult:
        """After-close job: fill pending orders at their session's opening price.

        `scheduled` (the 16:30 job) always pushes, even with nothing to fill, so a quiet day is
        distinguishable from a dead bot."""
        await self.start()
        assert self.store is not None
        async with self._cycle_lock:
            today = datetime.now(self.tz).date()
            result = CycleResult(day=today, dry_run=False)
            if not await self.store.pending_orders():
                if scheduled:
                    await self._settle_quiet(result, notify, waiting=0)
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
            elif scheduled:
                await self._settle_quiet(result, notify,
                                         waiting=len(await self.store.pending_orders()))
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

    async def _settle_quiet(self, result: CycleResult, notify: bool, waiting: int) -> None:
        assert self.store is not None
        snap = self.snapshot()
        lines = [f"⏳ {waiting} órdenes siguen esperando una apertura con precio." if waiting
                 else "No había órdenes pendientes: los analistas mantuvieron (HOLD) y la core no "
                      "rebalanceó."]
        if snap is not None:
            lines.append(f"💼 Cartera: {fmt_money(snap.total_value(), 'COP')}")
        lines.append("— Simulado (paper). No es asesoría financiera.")
        result.title = "🕓 Cierre: sin órdenes para ejecutar"
        result.message = "\n".join(lines)
        if notify:
            result.notified = await self.notifier.send(result.message, title=result.title,
                                                       tags=["clock4"], priority=2)
        await self.store.save_event("settle", payload={"executed": 0, "rejected": 0,
                                                       "waiting": waiting,
                                                       "notified": result.notified})

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

    async def _todays_analysis(self, strategy: str, today: date, slot: str) -> dict[str, Any] | None:
        """Today's analysis for this slot (decide or a given scan), so a re-run reuses it but a
        scan never reuses the morning's call (or vice versa)."""
        assert self.store is not None
        start = datetime.combine(today, time(0), tzinfo=self.tz).astimezone(timezone.utc)
        for ev in reversed(await self.store.events("analysis", since=start, strategy=strategy)):
            if ev["payload"].get("slot", "decide") == slot:
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
        tx = report.transaction
        if self.binance is not None and asset.market == "CRYPTO" and not dry_run:
            tx = await self._mirror_on_binance(tx, quote.price, result)
        await self._record(tx, tracker, persist=not dry_run)
        result.executed.append(tx)
        return tx, ""

    async def _mirror_on_binance(self, tx: Transaction, market_price: float,
                                 result: CycleResult) -> Transaction:
        """Send the crypto order to Binance Spot Testnet and book its real fill.

        On any testnet problem (no keys, price drift, filters, balance) keep the simulated fill,
        so the strategy's portfolio never diverges from its decisions; the reason is journaled."""
        assert self.binance is not None
        try:
            fill = await self.binance.market_order(tx.ticker, tx.kind, tx.quantity, market_price)
        except BinanceError as exc:
            log.warning("binance.fallback_paper", ticker=tx.ticker, side=tx.kind, error=str(exc))
            if self.store:
                await self.store.save_event("error", strategy=tx.strategy, ticker=tx.ticker,
                                            decision=tx.kind, reasoning=f"binance testnet: {exc}")
                await self.store.save_event(
                    "binance", strategy=tx.strategy, ticker=tx.ticker, decision=tx.kind,
                    reasoning=str(exc), payload={"status": "fallback", "market_price": market_price,
                                                 "quantity": tx.quantity, "paper_price": tx.price})
            result.notes.append(f"⚠️ Binance testnet ({tx.ticker}): {str(exc)[:100]} → simulado")
            return tx
        if self.store:
            await self.store.save_event(
                "binance", strategy=tx.strategy, ticker=tx.ticker, decision=tx.kind,
                payload={"status": "ok", "order_id": fill.order_id, "symbol": fill.symbol,
                         "quantity": fill.quantity, "price": fill.avg_price, "fee": fill.fee,
                         "market_price": market_price, "paper_price": tx.price,
                         "deviation": fill.avg_price / market_price - 1 if market_price else None})
        return tx.model_copy(update={  # realized P&L is computed by the tracker on apply
            "quantity": fill.quantity, "price": fill.avg_price,
            "gross": fill.quantity * fill.avg_price, "fee": fill.fee,
            "rationale": f"{tx.rationale} [Binance testnet #{fill.order_id}]"[:500],
        })

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
