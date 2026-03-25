"""
SQLite database — persists fills, orders, and portfolio snapshots.
Uses aiosqlite for async I/O. Migrations are applied on startup.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

DB_PATH = Path("data/poly_bot.db")


async def init_db(path: Path = DB_PATH) -> aiosqlite.Connection:
    """Open the database and run migrations. Returns open connection."""
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await _migrate(conn)
    log.info("db.initialized", path=str(path))
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            fill_id     TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL,
            token_id    TEXT NOT NULL,
            side        TEXT NOT NULL,
            price       REAL NOT NULL,
            size        REAL NOT NULL,
            cost_usdc   REAL NOT NULL,
            fee_usdc    REAL NOT NULL DEFAULT 0,
            strategy    TEXT NOT NULL DEFAULT '',
            is_paper    INTEGER NOT NULL DEFAULT 1,
            filled_at   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_fills_token ON fills(token_id);
        CREATE INDEX IF NOT EXISTS idx_fills_strategy ON fills(strategy);

        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cash_usdc       REAL NOT NULL,
            total_value     REAL NOT NULL,
            realized_pnl    REAL NOT NULL,
            unrealized_pnl  REAL NOT NULL,
            trade_count     INTEGER NOT NULL,
            win_count       INTEGER NOT NULL,
            taken_at        TEXT NOT NULL
        );
    """)
    await conn.commit()
