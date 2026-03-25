"""Persist fills, portfolio snapshots, and agent activity to SQLite."""

from __future__ import annotations

import json
import aiosqlite

from poly_bot.execution.models import Fill
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot

log = get_logger(__name__)


class TradeStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save_fill(self, fill: Fill) -> None:
        try:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO fills
                    (fill_id, order_id, token_id, side, price, size,
                     cost_usdc, fee_usdc, realized_pnl, strategy, is_paper, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.token_id,
                    fill.side,
                    fill.price,
                    fill.size,
                    fill.cost_usdc,
                    fill.fee_usdc,
                    fill.realized_pnl,
                    fill.strategy,
                    1 if fill.is_paper else 0,
                    fill.filled_at.isoformat(),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.error("store.save_fill_failed", error=str(exc))

    async def save_snapshot(self, snap: PortfolioSnapshot) -> None:
        try:
            await self._conn.execute(
                """
                INSERT INTO snapshots
                    (cash_usdc, total_value, realized_pnl, unrealized_pnl,
                     fees_paid, trade_count, win_count, taken_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.cash_usdc,
                    snap.total_value,
                    snap.total_realized_pnl,
                    snap.total_unrealized_pnl,
                    snap.total_fees_paid,
                    snap.trade_count,
                    snap.win_count,
                    snap.taken_at.isoformat(),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.error("store.save_snapshot_failed", error=str(exc))

    async def recent_fills(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def equity_history(self, limit: int = 500) -> list[dict]:
        """Return historical equity snapshots for charting (oldest first)."""
        cursor = await self._conn.execute(
            """
            SELECT taken_at, total_value, cash_usdc, realized_pnl,
                   unrealized_pnl, trade_count, win_count, fees_paid
            FROM snapshots
            ORDER BY taken_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        # Reverse so oldest is first (for chart x-axis)
        return [dict(row) for row in reversed(rows)]

    async def save_agent_event(self, event: dict) -> None:
        try:
            await self._conn.execute(
                """
                INSERT INTO agent_events
                    (event_type, strategy, condition_id, question, gemini_p, market_p,
                     edge, confidence, reasoning, key_factors, decision, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("event_type"),
                    event.get("strategy", "ai_research"),
                    event.get("condition_id", ""),
                    event.get("question", ""),
                    event.get("gemini_p"),
                    event.get("market_p"),
                    event.get("edge"),
                    event.get("confidence"),
                    event.get("reasoning"),
                    json.dumps(event.get("key_factors", [])),
                    event.get("decision"),
                    event.get("occurred_at", ""),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.error("store.save_agent_event_failed", error=str(exc))

    async def recent_agent_events(self, limit: int = 100) -> list[dict]:
        cursor = await self._conn.execute(
            """
            SELECT id, event_type, strategy, condition_id, question,
                   gemini_p, market_p, edge, confidence, reasoning,
                   key_factors, decision, occurred_at
            FROM agent_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["key_factors"] = json.loads(d["key_factors"] or "[]")
            except Exception:
                d["key_factors"] = []
            result.append(d)
        return result

    async def strategy_stats(self) -> list[dict]:
        """Aggregate P&L and win rate per strategy from closed trades."""
        cursor = await self._conn.execute(
            """
            SELECT
                strategy,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS closed_trades,
                SUM(CASE WHEN side = 'SELL' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN side = 'SELL' AND realized_pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN side = 'SELL' THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl,
                SUM(cost_usdc) AS total_volume,
                SUM(fee_usdc) AS total_fees
            FROM fills
            GROUP BY strategy
            ORDER BY realized_pnl DESC
            """
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            closed = d["closed_trades"] or 0
            wins = d["wins"] or 0
            d["win_rate"] = round(wins / closed, 4) if closed > 0 else 0.0
            result.append(d)
        return result
