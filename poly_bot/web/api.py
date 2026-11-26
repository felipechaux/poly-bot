"""
FastAPI dashboard + JSON API. Runs in the same process/event loop as the bot.

The dashboard polls the API (daily bot — no need for WebSockets).
Executing a cycle via POST requires `Authorization: Bearer $ADMIN_TOKEN` when ADMIN_TOKEN is set
(dry runs without notification are open).
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
            "next_job": bot.next_job,
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

    @app.get("/api/llm")
    async def llm(days: int = 30) -> dict[str, Any]:
        """Per-analyst usage, latest analysis, hit rates and sleeve P&L, plus the shared journal."""
        from datetime import timedelta

        from poly_bot.strategies.llm_analyst import LLMAnalystStrategy

        store = _store()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        analyses = await store.events("analysis", since=since)
        recs = await store.events("recommendation", since=since - timedelta(days=30))
        outcomes = await store.events("outcome", since=since - timedelta(days=30))
        by_rec: dict[int, dict[int, Any]] = defaultdict(dict)
        for o in outcomes:
            by_rec[o["payload"]["rec_id"]][o["payload"]["horizon_days"]] = o["payload"]
        snap = bot.tracker.snapshot()
        today = datetime.now(bot.tz).date().isoformat()

        def _sum(evs: list[dict[str, Any]], key: str) -> float:
            return sum(float(e["payload"].get("usage", {}).get(key, 0) or 0) for e in evs)

        def _hit_rates(outs: list[dict[str, Any]]) -> dict[str, Any]:
            res: dict[str, Any] = {}
            for h in (1, 7, 30):
                scored = [o["payload"] for o in outs if o["payload"]["horizon_days"] == h]
                res[f"{h}d"] = {
                    "n": len(scored),
                    "hit_rate": (sum(1 for o in scored if o["hit"]) / len(scored)) if scored else None,
                    "avg_ret": (sum(o["ret"] for o in scored) / len(scored)) if scored else None,
                }
            return res

        analysts = []
        for st in bot.strategies:
            if not isinstance(st, LLMAnalystStrategy):
                continue
            mine = [a for a in analyses if a["strategy"] == st.name]
            todays = [a for a in mine if datetime.fromisoformat(a["occurred_at"])
                      .astimezone(bot.tz).date().isoformat() == today]
            latest = mine[-1] if mine else None
            status = await bot._analyst_status(st, snap)
            analysts.append({
                "name": st.name, "label": st.label, "provider": st.provider, "model": st.p["model"],
                "usage": {
                    "calls_today": len(todays),
                    "tokens_today": _sum(todays, "total_tokens"),
                    "cost_equiv_today": _sum(todays, "cost_usd_equiv"),
                    f"calls_{days}d": len(mine),
                    f"tokens_{days}d": _sum(mine, "total_tokens"),
                    f"cost_equiv_{days}d": _sum(mine, "cost_usd_equiv"),
                    "avg_duration_ms": (_sum(mine, "duration_ms") / len(mine)) if mine else None,
                },
                "latest": {
                    "occurred_at": latest["occurred_at"],
                    "resumen_mercado": latest["payload"].get("resumen_mercado"),
                    "acciones": latest["payload"].get("acciones", []),
                    "usage": latest["payload"].get("usage", {}),
                } if latest else None,
                "hit_rates": _hit_rates([o for o in outcomes if o["strategy"] == st.name]),
                "sleeve": status["sleeve"],
                "semaforo": status["semaforo"].as_dict(),
                "guard": status["guard"],
            })
        labels = {a["name"]: a["label"] for a in analysts}
        return {
            "base_currency": snap.base_currency,
            "analysts": analysts,
            "journal": [
                {"id": r["id"], "day": r["occurred_at"][:10], "strategy": r["strategy"],
                 "analyst": labels.get(r["strategy"], r["strategy"]), "ticker": r["ticker"],
                 "decision": r["decision"], "reasoning": r["reasoning"],
                 "confianza": r["payload"].get("confianza"), "peso": r["payload"].get("peso_objetivo"),
                 "price_at": r["payload"].get("price_at"), "planned": r["payload"].get("planned"),
                 "outcomes": {f"{h}d": by_rec.get(r["id"], {}).get(h) for h in (1, 7, 30)}}
                for r in reversed(recs[-80:])
            ],
        }

    @app.get("/api/orders")
    async def orders(limit: int = 50) -> dict[str, Any]:
        """Orders waiting for the next open, plus recently closed ones."""
        store = _store()
        return {"fill_mode": bot.fill_mode, "settle_time": bot.settle_time,
                "pending": await store.pending_orders(),
                "recent": [o for o in await store.pending_orders(status=None, limit=limit)
                           if o["status"] != "pending"]}

    binance_cache: dict[str, Any] = {"at": 0.0, "balances": None, "error": None}

    @app.get("/api/binance")
    async def binance() -> dict[str, Any]:
        """Binance Spot Testnet: state, live testnet balances (cached 60 s) and mirrored orders."""
        from poly_bot.execution.binance_testnet import BinanceError

        out: dict[str, Any] = {"state": bot.binance_state, "balances": None, "error": None}
        if bot.binance is not None:
            now = datetime.now(timezone.utc).timestamp()
            if now - binance_cache["at"] > 60:
                try:
                    bal = await bot.binance.balances()
                    binance_cache.update(balances={k: v for k, v in bal.items()
                                                   if k in {"USDT", "BTC", "ETH", "SOL"}}, error=None)
                except BinanceError as exc:
                    binance_cache.update(balances=None, error=str(exc))
                binance_cache["at"] = now
            out.update(balances=binance_cache["balances"], error=binance_cache["error"])
        since = datetime.now(timezone.utc) - timedelta(days=30)
        evs = await _store().events("binance", since=since)
        ok = [e for e in evs if e["payload"].get("status") == "ok"]
        devs = [abs(e["payload"]["deviation"]) for e in ok if e["payload"].get("deviation") is not None]
        out["stats_30d"] = {"ok": len(ok), "fallback": len(evs) - len(ok),
                            "avg_abs_deviation": sum(devs) / len(devs) if devs else None}
        out["orders"] = [{"occurred_at": e["occurred_at"], "strategy": e["strategy"],
                          "ticker": e["ticker"], "side": e["decision"], "reason": e["reasoning"],
                          **e["payload"]} for e in reversed(evs[-30:])]
        # Real order-book fills (crypto_fill: orderbook)
        fills = await _store().events("crypto_fill", since=since)
        book = [f for f in fills if f["payload"].get("source") == "orderbook"]
        def avg(key: str) -> float | None:
            return sum(f["payload"][key] for f in book) / len(book) if book else None

        saved = [f["payload"]["model_price"] / f["payload"]["vwap"] - 1 if f["decision"] == "BUY"
                 else f["payload"]["vwap"] / f["payload"]["model_price"] - 1
                 for f in book if f["payload"].get("model_price")]
        out["crypto_fill"] = bot.crypto_fill
        out["book_stats_30d"] = {"fills": len(book), "fallback": len(fills) - len(book),
                                 "avg_impact_pct": avg("impact_pct"), "avg_spread_pct": avg("spread_pct"),
                                 "avg_vs_model_pct": sum(saved) / len(saved) if saved else None}
        out["book_fills"] = [{"occurred_at": f["occurred_at"], "strategy": f["strategy"],
                              "ticker": f["ticker"], "side": f["decision"], "reason": f["reasoning"],
                              **f["payload"]} for f in reversed(fills[-30:])]
        return out

    @app.get("/api/events")
    async def events(limit: int = 100) -> dict[str, Any]:
        return {"events": await _store().recent_events(limit=limit)}

    # ── Actions ─────────────────────────────────────────────────────────────

    @app.post("/api/settle")
    async def settle(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Fill pending orders whose opening price is known (admin)."""
        _require_admin(authorization)
        r = await bot.settle()
        return {"executed": len(r.executed), "rejected": len(r.rejected), "title": r.title,
                "message": r.message, "notified": r.notified}

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
