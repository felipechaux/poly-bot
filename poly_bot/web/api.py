"""
FastAPI dashboard + JSON API. Runs in the same process/event loop as the bot.

The dashboard polls the API (daily bot — no need for WebSockets).
Executing a cycle via POST requires `Authorization: Bearer $ADMIN_TOKEN` when ADMIN_TOKEN is set
(dry runs without notification are open).
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from poly_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from poly_bot.bot import Bot
    from poly_bot.store.trade_store import TradeStore

log = get_logger(__name__)


def create_app(bot: "Bot") -> FastAPI:
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app = FastAPI(title="Invest Bot", version="0.3.0")
    admin_token = os.environ.get("ADMIN_TOKEN", "")

    def _require_admin(authorization: str | None) -> None:
        if admin_token and authorization != f"Bearer {admin_token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    def _store() -> "TradeStore":
        if bot.store is None:
            raise HTTPException(status_code=503, detail="bot starting")
        return bot.store

    # ── Pages ────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):  # type: ignore[no-untyped-def]
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── Read API ────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        lc = bot.last_cycle
        return {
            "running": bot.running,
            "mode": bot.mode,
            "strategies": [s.name for s in bot.strategies],
            "universe_size": len(bot.universe),
            "notify_enabled": bot.notifier.enabled,
            "next_run_at": bot.next_run_at.isoformat() if bot.next_run_at else None,
            "last_cycle": {"day": str(lc.day), "title": lc.title, "message": lc.message,
                           "executed": len(lc.executed), "rejected": len(lc.rejected)} if lc else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/portfolio")
    async def portfolio() -> dict[str, Any]:
        snap = bot.snapshot()
        if snap is None:
            return {"ready": False}
        alloc: dict[str, dict[str, float]] = {
            "asset_class": defaultdict(float), "region": defaultdict(float), "currency": defaultdict(float)
        }
        total = snap.total_value()
        for p in snap.positions:
            a = bot.universe.get(p.ticker)
            v = snap.to(p.market_value, p.currency) / total if total else 0
            alloc["asset_class"][a.asset_class if a else "?"] += v
            alloc["region"][a.region if a else "?"] += v
            alloc["currency"][p.currency] += v
        for ccy, amt in snap.cash.items():
            alloc["asset_class"]["cash"] += snap.to(amt, ccy) / total if total else 0
            alloc["currency"][ccy] += snap.to(amt, ccy) / total if total else 0
        first = await _store().first_snapshot()
        from poly_bot.notify.summary import SummaryInput, benchmark_returns, since_start_return
        inp = SummaryInput(snap=snap, quotes={}, universe=bot.universe, first_snapshot=first,
                           benchmarks_now={a.ticker: q.price for a in bot.universe.benchmarks
                                           if (q := (bot.last_market.quotes.get(a.ticker)
                                                     if bot.last_market else None))})
        return {
            "ready": True,
            "base_currency": snap.base_currency,
            "usdcop": snap.usdcop,
            "total_cop": snap.total_value("COP"),
            "total_usd": snap.total_value("USD"),
            "cash": snap.cash,
            "positions_value_cop": snap.positions_value("COP"),
            "unrealized_cop": snap.unrealized_pnl("COP"),
            "realized_cop": snap.realized("COP"),
            "fees_cop": snap.total_fees("COP"),
            "dividends_cop": snap.total_dividends("COP"),
            "return_since_start": since_start_return(inp),
            "benchmarks_since_start": benchmark_returns(inp),
            "allocation": {k: dict(v) for k, v in alloc.items()},
        }

    @app.get("/api/positions")
    async def positions() -> dict[str, Any]:
        snap = bot.snapshot()
        if snap is None:
            return {"positions": []}
        out = []
        for p in sorted(snap.positions, key=lambda p: -snap.to(p.market_value, p.currency)):
            a = bot.universe.get(p.ticker)
            out.append({
                "ticker": p.ticker, "name": a.name if a else "", "currency": p.currency,
                "asset_class": a.asset_class if a else "", "region": a.region if a else "",
                "quantity": p.quantity, "avg_cost": p.avg_cost, "price": p.last_price,
                "market_value": p.market_value, "value_cop": snap.to(p.market_value, p.currency, "COP"),
                "weight": snap.weight(p.ticker), "unrealized": p.unrealized_pnl,
                "unrealized_pct": p.unrealized_pct, "dividends": p.dividends, "strategy": p.strategy,
            })
        return {"positions": out}

    @app.get("/api/transactions")
    async def transactions(limit: int = 100) -> dict[str, Any]:
        return {"transactions": await _store().recent_transactions(limit=limit)}

    @app.get("/api/equity")
    async def equity(limit: int = 2000) -> dict[str, Any]:
        return {"snapshots": await _store().snapshots(limit=limit)}

    @app.get("/api/universe")
    async def universe() -> dict[str, Any]:
        quotes = bot.last_market.quotes if bot.last_market else await _store().load_quotes()
        rows = []
        for a in bot.universe:
            q = quotes.get(a.ticker)
            rows.append({
                **a.model_dump(), "currency": a.currency,
                "price": q.price if q else None, "as_of": str(q.as_of) if q else None,
                "trend": q.trend if q else "sin datos",
                **(q.indicators.model_dump() if q else {}),
            })
        return {"assets": rows}

    @app.get("/api/events")
    async def events(limit: int = 100) -> dict[str, Any]:
        return {"events": await _store().recent_events(limit=limit)}

    # ── Actions ─────────────────────────────────────────────────────────────

    @app.post("/api/cycle")
    async def run_cycle(dry_run: bool = True, notify: bool = False,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not dry_run or notify:  # simulating is harmless; executing/notifying needs the token
            _require_admin(authorization)
        result = await bot.run_cycle(dry_run=dry_run, notify=notify)
        return {
            "day": str(result.day), "dry_run": result.dry_run,
            "executed": [t.model_dump(mode="json") for t in result.executed],
            "rejected": [{"ticker": s.ticker, "side": s.side, "reason": r} for s, r in result.rejected],
            "title": result.title, "message": result.message, "notified": result.notified,
        }

    return app
