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
        return templates.TemplateResponse("dashboard.html", {"request": request})

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
    async def markets(limit: int = 20, min_liquidity: float = 5000.0):
        bot = _get_bot()
        if not bot:
            return {"markets": []}
        tracked = bot._feed.tracked_markets[:limit]
        return {
            "markets": [
                {
                    "condition_id": m.condition_id,
                    "question": m.question,
                    "category": m.category,
                    "liquidity": m.liquidity,
                    "volume": m.volume,
                    "yes_price": m.yes_token.price if m.yes_token else 0.0,
                }
                for m in tracked
                if m.liquidity >= min_liquidity
            ]
        }

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
