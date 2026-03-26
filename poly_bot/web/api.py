"""
FastAPI web server — dashboard API + WebSocket real-time feed.
Runs alongside the trading bot in the same process.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

# ── WebSocket connection manager ─────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        log.info("ws.connected", total=len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.remove(ws)
        log.info("ws.disconnected", total=len(self.active))

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(data, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()

# ── App factory ──────────────────────────────────────────────────────────────

def create_app(bot_ref: list) -> FastAPI:
    """
    Create the FastAPI app.
    bot_ref is a mutable list holding the Bot instance once started,
    so the API can reference it without circular imports.
    """

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    app = FastAPI(
        title="Poly Bot Dashboard",
        description="Polymarket trading bot web interface",
        version="0.1.0",
    )

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(templates_dir))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_bot():
        return bot_ref[0] if bot_ref else None

    def _portfolio_dict() -> dict[str, Any]:
        bot = _get_bot()
        if not bot:
            return {}
        snap = bot.get_snapshot()
        from poly_bot.portfolio.pnl import format_pnl_summary
        summary = format_pnl_summary(snap)
        return {
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
            "formatted": summary,
        }

    def _positions_list() -> list[dict]:
        bot = _get_bot()
        if not bot:
            return []
        snap = bot.get_snapshot()
        return [
            {
                "token_id": p.token_id,
                "outcome": p.outcome,
                "side": p.side,
                "shares": round(p.shares, 4),
                "avg_cost": round(p.avg_cost_basis, 4),
                "total_cost_usdc": round(p.total_cost_usdc, 2),
                "realized_pnl": round(p.realized_pnl, 4),
                "strategy": p.strategy,
                "opened_at": p.opened_at.isoformat(),
            }
            for p in snap.positions
        ]

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/api/status")
    async def status():
        bot = _get_bot()
        return {
            "running": bot is not None and bot._running,
            "mode": bot._settings.mode if bot else "unknown",
            "strategies": [s.name for s in bot._strategies] if bot else [],
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/api/portfolio")
    async def portfolio():
        return _portfolio_dict()

    @app.get("/api/positions")
    async def positions():
        return {"positions": _positions_list()}

    @app.get("/api/trades")
    async def trades(limit: int = 50):
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore
            conn = await init_db()
            store = TradeStore(conn)
            fills = await store.recent_fills(limit=limit)
            await conn.close()
            return {"trades": fills}
        except Exception as exc:
            return {"trades": [], "error": str(exc)}

    @app.get("/api/markets")
    async def markets(limit: int = 20, min_liquidity: float = 1000.0):
        # Prefer live feed data if the bot is running and feed is populated
        bot = _get_bot()
        tracked = bot._feed.tracked_markets if bot else []

        if tracked:
            filtered = [m for m in tracked if m.liquidity >= min_liquidity]
            filtered.sort(key=lambda m: m.liquidity, reverse=True)
            return {
                "markets": [
                    {
                        "condition_id": m.condition_id,
                        "question": m.question,
                        "category": m.category,
                        "liquidity": m.liquidity,
                        "volume": m.volume,
                        "yes_price": (m.yes_token.price if m.yes_token and m.yes_token.price > 0
                                      else m.outcome_prices[0] if m.outcome_prices
                                      else m.last_trade_price),
                    }
                    for m in filtered[:limit]
                ]
            }

        # Fall back to fetching directly from Gamma API
        try:
            from poly_bot.market_data.gamma_client import GammaClient
            gamma = GammaClient()
            raw = await gamma.get_markets(limit=limit * 2, active=True, closed=False)
            await gamma.close()
            filtered = [m for m in raw if m.liquidity >= min_liquidity]
            filtered.sort(key=lambda m: m.liquidity, reverse=True)
            return {
                "markets": [
                    {
                        "condition_id": m.condition_id,
                        "question": m.question,
                        "category": m.category,
                        "liquidity": m.liquidity,
                        "volume": m.volume,
                        "yes_price": (m.yes_token.price if m.yes_token and m.yes_token.price > 0
                                      else m.outcome_prices[0] if m.outcome_prices
                                      else m.last_trade_price),
                    }
                    for m in filtered[:limit]
                ]
            }
        except Exception as exc:
            return {"markets": [], "error": str(exc)}

    @app.get("/api/debug/gamma")
    async def debug_gamma(limit: int = 3):
        """Return raw Gamma API response for diagnosing price field issues."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"limit": limit, "active": "true", "closed": "false"},
                    headers={"User-Agent": "poly-bot/0.1.0"},
                )
                resp.raise_for_status()
                data = resp.json()
            # Return first market raw so we can see all available fields
            return {"raw_markets": data[:limit] if isinstance(data, list) else data}
        except Exception as exc:
            return {"error": str(exc)}

    @app.get("/api/agent")
    async def agent_events(limit: int = 100):
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore
            conn = await init_db()
            store = TradeStore(conn)
            events = await store.recent_agent_events(limit=limit)
            await conn.close()
            return {"events": events}
        except Exception as exc:
            return {"events": [], "error": str(exc)}

    @app.get("/api/equity")
    async def equity(limit: int = 500):
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore
            conn = await init_db()
            store = TradeStore(conn)
            history = await store.equity_history(limit=limit)
            await conn.close()
            return {"equity": history}
        except Exception as exc:
            return {"equity": [], "error": str(exc)}

    @app.get("/api/stats")
    async def stats():
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore
            conn = await init_db()
            store = TradeStore(conn)
            strategy_data = await store.strategy_stats()
            history = await store.equity_history(limit=1000)
            await conn.close()

            # Compute max drawdown and ROI from equity history
            max_drawdown = 0.0
            roi_pct = 0.0
            sharpe = 0.0
            if history:
                values = [h["total_value"] for h in history]
                first_val = values[0]
                roi_pct = round((values[-1] - first_val) / first_val * 100, 2) if first_val else 0.0
                peak = values[0]
                for v in values:
                    if v > peak:
                        peak = v
                    dd = (peak - v) / peak if peak > 0 else 0.0
                    if dd > max_drawdown:
                        max_drawdown = dd

                # Simple Sharpe: mean daily return / std (using snapshots as proxy)
                if len(values) >= 2:
                    import statistics
                    returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values)) if values[i - 1] > 0]
                    if returns and statistics.stdev(returns) > 0:
                        sharpe = round(statistics.mean(returns) / statistics.stdev(returns), 3)

            return {
                "roi_pct": roi_pct,
                "max_drawdown_pct": round(max_drawdown * 100, 2),
                "sharpe_ratio": sharpe,
                "strategies": strategy_data,
            }
        except Exception as exc:
            return {"roi_pct": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0, "strategies": [], "error": str(exc)}

    @app.post("/api/bot/stop")
    async def stop_bot():
        bot = _get_bot()
        if bot:
            await bot.stop()
            return {"status": "stopped"}
        return {"status": "not_running"}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await manager.connect(ws)
        try:
            # Send initial state immediately on connect
            await ws.send_text(json.dumps({
                "type": "portfolio",
                "data": _portfolio_dict(),
            }, default=str))
            await ws.send_text(json.dumps({
                "type": "positions",
                "data": _positions_list(),
            }, default=str))
            # Send equity history for chart bootstrap
            try:
                from poly_bot.store.database import init_db
                from poly_bot.store.trade_store import TradeStore
                _conn = await init_db()
                _store = TradeStore(_conn)
                _history = await _store.equity_history(limit=500)
                _stats = await _store.strategy_stats()
                await _conn.close()
                await ws.send_text(json.dumps({
                    "type": "equity_history",
                    "data": _history,
                }, default=str))
                await ws.send_text(json.dumps({
                    "type": "strategy_stats",
                    "data": _stats,
                }, default=str))
                _agent_events = await _store.recent_agent_events(limit=100)
                await ws.send_text(json.dumps({
                    "type": "agent_history",
                    "data": _agent_events,
                }, default=str))
            except Exception:
                pass

            # Keep alive — client sends pings
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                    if msg == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
                except asyncio.TimeoutError:
                    # Push portfolio update every 30s even without ping
                    await ws.send_text(json.dumps({
                        "type": "portfolio",
                        "data": _portfolio_dict(),
                    }, default=str))
        except WebSocketDisconnect:
            manager.disconnect(ws)

    return app


async def broadcast_fill(fill_data: dict[str, Any]) -> None:
    """Called by the bot on every fill to push to all dashboard clients."""
    await manager.broadcast({"type": "fill", "data": fill_data})


async def broadcast_portfolio(portfolio_data: dict[str, Any]) -> None:
    """Push portfolio update to all connected dashboard clients."""
    await manager.broadcast({"type": "portfolio", "data": portfolio_data})


async def broadcast_agent_event(event: dict[str, Any]) -> None:
    """Push a live agent activity event to all dashboard clients."""
    await manager.broadcast({"type": "agent_event", "data": event})


async def broadcast_equity_point(point: dict[str, Any]) -> None:
    """Push a new equity snapshot point to dashboard clients for live chart updates."""
    await manager.broadcast({"type": "equity_point", "data": point})
