"""
Bot orchestrator — wires all components together and runs the main event loop.

Data flow:
  MarketDataFeed → asyncio.Queue → StrategyRunner → ExecutionEngine → PositionTracker
"""

from __future__ import annotations

import asyncio
from typing import Union

from poly_bot.config.settings import Settings
from poly_bot.execution.live_executor import LiveExecutor
from poly_bot.execution.models import Fill, OrderRequest
from poly_bot.execution.paper_executor import PaperExecutor
from poly_bot.market_data.clob_client import AsyncClobClient
from poly_bot.market_data.feed import MarketDataFeed
from poly_bot.market_data.gamma_client import GammaClient
from poly_bot.market_data.models import MarketUpdate
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.risk.manager import RiskManager
from poly_bot.strategies.base import Signal, StrategyContext
from poly_bot.strategies.registry import load_strategies

log = get_logger(__name__)

ExecutorType = Union[PaperExecutor, LiveExecutor]


class Bot:
    """
    Main bot orchestrator.
    Call run() to start, stop() to shut down gracefully.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[MarketUpdate] = asyncio.Queue(maxsize=200)

        # Build clients
        private_key = None
        if settings.private_key:
            private_key = settings.private_key.get_secret_value()

        self._clob = AsyncClobClient(
            host=settings.clob_host,
            chain_id=settings.chain_id,
            private_key=private_key,
        )
        self._gamma = GammaClient(host=settings.gamma_host)

        # Build executor (paper or live)
        if settings.mode == "live":
            log.warning("bot.live_mode_active", msg="REAL MONEY trading is ENABLED")
            self._executor: ExecutorType = LiveExecutor(self._clob)
        else:
            paper_cfg = settings.paper
            self._executor = PaperExecutor(
                initial_balance_usdc=settings.paper_initial_balance,
                fill_mode=paper_cfg.get("fill_mode", "order_book"),
                simulated_latency_ms=paper_cfg.get("simulated_latency_ms", 200),
            )

        # Portfolio tracking
        initial_balance = (
            settings.paper_initial_balance if settings.mode == "paper" else 0.0
        )
        self._portfolio = PositionTracker(initial_balance_usdc=initial_balance)

        # Wire fill events: executor → position tracker
        self._executor.on_fill(self._on_fill)

        # Risk manager
        risk_cfg = settings.risk
        self._risk = RiskManager(
            max_position_size_usdc=risk_cfg.get("max_position_size_usdc", 500.0),
            max_total_exposure_usdc=risk_cfg.get("max_total_exposure_usdc", 3000.0),
            max_position_count=risk_cfg.get("max_position_count", 10),
            min_market_liquidity_usdc=risk_cfg.get("min_market_liquidity_usdc", 1000.0),
            min_price=risk_cfg.get("min_price", 0.02),
            max_price=risk_cfg.get("max_price", 0.98),
            order_debounce_seconds=risk_cfg.get("order_debounce_seconds", 30.0),
        )

        # Strategies
        self._strategies = load_strategies(settings.strategies)

        # Wire agent event callback for AI research strategy
        from poly_bot.strategies.ai_research import AIResearchStrategy
        for s in self._strategies:
            if isinstance(s, AIResearchStrategy):
                s.on_agent_event(self._on_agent_event)

        # Feed
        feed_cfg = settings.feed
        self._feed = MarketDataFeed(
            clob_client=self._clob,
            gamma_client=self._gamma,
            queue=self._queue,
            poll_interval=feed_cfg.get("poll_interval_seconds", 2.0),
            market_refresh_interval=feed_cfg.get("market_refresh_seconds", 60.0),
            max_markets=feed_cfg.get("max_markets", 50),
            min_liquidity=risk_cfg.get("min_market_liquidity_usdc", 1000.0),
            min_yes_price=feed_cfg.get("min_yes_price", 0.0),
            max_yes_price=feed_cfg.get("max_yes_price", 1.0),
        )

        self._running = False
        self._db = None
        self._store = None

        log.info(
            "bot.initialized",
            mode=settings.mode,
            strategies=[s.name for s in self._strategies],
            paper_balance=initial_balance if settings.mode == "paper" else None,
        )

    async def run(self) -> None:
        """Start all components and run until stopped."""
        self._running = True

        # Optional: init DB
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore
            self._db = await init_db()
            self._store = TradeStore(self._db)
        except Exception as exc:
            log.warning("bot.db_init_failed", error=str(exc), msg="Continuing without persistence")

        # Start strategies
        for s in self._strategies:
            await s.on_start()

        log.info("bot.running", strategy_count=len(self._strategies))

        try:
            await asyncio.gather(
                self._feed.run(),
                self._consume_updates(),
                self._snapshot_loop(),
            )
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._running = False
        await self._feed.stop()
        log.info("bot.stopping")

    def get_snapshot(self) -> PortfolioSnapshot:
        return self._portfolio.snapshot()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _consume_updates(self) -> None:
        """Consume market updates from the queue and dispatch to strategies."""
        while self._running:
            try:
                update = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Update current price for P&L
            mid = update.order_book.mid_price
            yes_token = update.market.yes_token
            if mid is not None and yes_token:
                self._portfolio.update_price(yes_token.token_id, mid)

            # Notify paper executor of new book (for resting order matching)
            if isinstance(self._executor, PaperExecutor):
                await self._executor.on_market_update(update.order_book)

            # Run all strategies
            portfolio_snap = self._portfolio.snapshot()
            for strategy in self._strategies:
                try:
                    ctx = StrategyContext(
                        update=update,
                        position=self._portfolio.get_position(
                            yes_token.token_id if yes_token else ""
                        ),
                        portfolio=portfolio_snap,
                        config=self._settings.get_strategy_config(strategy.name),
                    )
                    signals = await strategy.on_market_update(ctx)
                    for signal in signals:
                        await self._process_signal(signal, update, strategy.name)
                except Exception as exc:
                    log.error(
                        "bot.strategy_error",
                        strategy=strategy.name,
                        error=str(exc),
                        exc_info=True,
                    )

            self._queue.task_done()

    async def _process_signal(
        self,
        signal: Signal,
        update: MarketUpdate,
        strategy_name: str,
    ) -> None:
        """Risk-check a signal and submit to executor if approved."""
        portfolio_snap = self._portfolio.snapshot()

        approved, reason = self._risk.check(
            request=OrderRequest(
                token_id=signal.token_id,
                side=signal.side,
                price=signal.price,
                size_usdc=signal.size_usdc,
                strategy=strategy_name,
            ),
            portfolio=portfolio_snap,
            market=update.market,
        )

        if not approved:
            log.debug(
                "bot.signal_rejected",
                strategy=strategy_name,
                token_id=signal.token_id,
                reason=reason,
            )
            return

        request = OrderRequest(
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size_usdc=signal.size_usdc,
            order_type=signal.order_type,
            strategy=strategy_name,
            rationale=signal.rationale,
        )

        log.info(
            "bot.signal_approved",
            strategy=strategy_name,
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size_usdc=signal.size_usdc,
            rationale=signal.rationale,
        )

        if isinstance(self._executor, PaperExecutor):
            report = await self._executor.submit_order_with_book(
                request, update.order_book
            )
        else:
            report = await self._executor.submit_order(request)

        log.info(
            "bot.order_result",
            order_id=report.order_id,
            status=report.status,
            fills=len(report.fills),
        )

    def _on_agent_event(self, event: dict) -> None:
        """Callback from AI strategy on every research/signal/skip event."""
        if self._store:
            asyncio.create_task(self._store.save_agent_event(event))
        try:
            from poly_bot.web.api import broadcast_agent_event
            asyncio.create_task(broadcast_agent_event(event))
        except Exception:
            pass

    def _on_fill(self, fill: Fill) -> None:
        """Callback from executor on every fill."""
        self._portfolio.on_fill(fill)
        if self._store:
            asyncio.create_task(self._store.save_fill(fill))
        # Push fill to web dashboard
        try:
            from poly_bot.web.api import broadcast_fill, broadcast_portfolio
            asyncio.create_task(broadcast_fill({
                "fill_id": fill.fill_id,
                "order_id": fill.order_id,
                "token_id": fill.token_id,
                "side": fill.side,
                "price": fill.price,
                "size": fill.size,
                "cost_usdc": fill.cost_usdc,
                "fee_usdc": fill.fee_usdc,
                "strategy": fill.strategy,
                "is_paper": fill.is_paper,
                "filled_at": fill.filled_at.isoformat(),
            }))
            # Also push updated portfolio
            snap = self._portfolio.snapshot()
            from poly_bot.portfolio.pnl import format_pnl_summary
            asyncio.create_task(broadcast_portfolio({
                "cash_usdc": snap.cash_usdc,
                "total_value": snap.total_value,
                "total_position_value": snap.total_position_value,
                "realized_pnl": snap.total_realized_pnl,
                "unrealized_pnl": snap.total_unrealized_pnl,
                "total_pnl": snap.total_pnl,
                "win_rate": snap.win_rate,
                "trade_count": snap.trade_count,
                "win_count": snap.win_count,
                "loss_count": snap.loss_count,
                "fees_paid": snap.total_fees_paid,
            }))
        except Exception:
            pass

    async def _snapshot_loop(self, interval_seconds: float = 300.0) -> None:
        """Save a portfolio snapshot every 5 minutes and push to dashboard."""
        while self._running:
            await asyncio.sleep(interval_seconds)
            if not self._running:
                break
            snap = self._portfolio.snapshot()
            if self._store:
                await self._store.save_snapshot(snap)
            try:
                from poly_bot.web.api import broadcast_equity_point
                asyncio.create_task(broadcast_equity_point({
                    "taken_at": snap.taken_at.isoformat(),
                    "total_value": snap.total_value,
                    "cash_usdc": snap.cash_usdc,
                    "realized_pnl": snap.total_realized_pnl,
                    "unrealized_pnl": snap.total_unrealized_pnl,
                    "trade_count": snap.trade_count,
                    "fees_paid": snap.total_fees_paid,
                }))
            except Exception:
                pass
            log.info("bot.snapshot_saved", total_value=snap.total_value)

    async def _shutdown(self) -> None:
        for s in self._strategies:
            await s.on_stop()
        await self._clob.close()
        await self._gamma.close()
        if self._db:
            await self._db.close()
        log.info("bot.shutdown_complete")
