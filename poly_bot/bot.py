"""
Bot orchestrator — runs one cycle per day (after market close, Bogotá time).

Daily cycle:
  prices (yfinance) → mark-to-market → dividends → strategies → risk → paper fills
  → daily snapshot → ntfy summary

State lives in the SQLite transaction ledger and is replayed on startup.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from poly_bot.config.settings import Settings
from poly_bot.execution.models import OrderRequest, Transaction
from poly_bot.execution.paper_executor import PaperExecutor
from poly_bot.market_data.provider import MarketData, PriceProvider, YFinanceProvider
from poly_bot.market_data.universe import Universe
from poly_bot.notify.ntfy import NtfyNotifier
from poly_bot.notify.summary import SummaryInput, build_daily_summary
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.risk.manager import RiskManager
from poly_bot.store.database import init_db
from poly_bot.store.trade_store import TradeStore
from poly_bot.strategies.base import DailyContext, Signal, Strategy
from poly_bot.strategies.registry import load_strategies

log = get_logger(__name__)

FX_MIN_USD = 10.0  # don't bother converting less than this


@dataclass
class CycleResult:
    day: date
    dry_run: bool
    executed: list[Transaction] = field(default_factory=list)
    rejected: list[tuple[Signal, str]] = field(default_factory=list)
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
            if self._settings.schedule.get("run_on_start", True) and await self._missed_today():
                await self._safe_cycle()
            while self._running:
                self.next_run_at = self._next_run_time(datetime.now(self.tz))
                log.info("bot.next_cycle", at=self.next_run_at.isoformat())
                while self._running and datetime.now(self.tz) < self.next_run_at:
                    remaining = (self.next_run_at - datetime.now(self.tz)).total_seconds()
                    await asyncio.sleep(max(0.5, min(remaining, 30.0)))
                if self._running:
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

    def _next_run_time(self, now: datetime) -> datetime:
        hh, mm = (int(x) for x in str(self._settings.schedule.get("daily_run_time", "17:00")).split(":"))
        weekdays_only = self._settings.schedule.get("weekdays_only", True)
        candidate = datetime.combine(now.date(), time(hh, mm), tzinfo=self.tz)
        if candidate <= now:
            candidate += timedelta(days=1)
        while weekdays_only and candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    async def _missed_today(self) -> bool:
        """True if today's cycle hasn't run yet (avoids re-notifying on every restart)."""
        assert self.store is not None
        today = datetime.now(self.tz).date()
        snaps = await self.store.snapshots(limit=1)
        return not snaps or snaps[-1]["day"] != today.isoformat()

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

            for strategy in self.strategies:
                last = await self.store.last_event_time(strategy.name, "signals")
                ctx = DailyContext(
                    today=today, quotes=md.quotes, usdcop=md.usdcop,
                    portfolio=tracker.snapshot(), universe=self.universe,
                    last_signal_date=last.astimezone(self.tz).date() if last else None,
                )
                signals = await strategy.on_daily(ctx)
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
        sells = [s for s in signals if s.side == "SELL"]
        buys = [s for s in signals if s.side == "BUY"]
        for sig in sells:
            await self._execute_one(strategy, sig, tracker, md, today, result, dry_run)
        if buys:
            await self._fund_buys(strategy, buys, tracker, md, result, dry_run)
        for sig in buys:
            await self._execute_one(strategy, sig, tracker, md, today, result, dry_run)

    async def _execute_one(self, strategy: str, sig: Signal, tracker: PositionTracker,
                           md: MarketData, today: date, result: CycleResult,
                           dry_run: bool) -> None:
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
            return
        asset = self.universe.require(sig.ticker)
        report = self.executor.execute(
            req, asset, quote,
            cash_available=max(0.0, tracker.cash.get(asset.currency, 0.0)),
            held_quantity=tracker.held_quantity(sig.ticker),
        )
        if not report.is_filled or report.transaction is None:
            result.rejected.append((sig, report.rejection_reason))
            return
        await self._record(report.transaction, tracker, persist=not dry_run)
        result.executed.append(report.transaction)

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
