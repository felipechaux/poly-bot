"""Persist fills and portfolio snapshots to SQLite."""

from __future__ import annotations

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
                     cost_usdc, fee_usdc, strategy, is_paper, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                     trade_count, win_count, taken_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.cash_usdc,
                    snap.total_value,
                    snap.total_realized_pnl,
                    snap.total_unrealized_pnl,
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
