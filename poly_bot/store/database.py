"""
SQLite database — persists fills, orders, and portfolio snapshots.
Uses aiosqlite for async I/O. Migrations are applied on startup.
"""

from __future__ import annotations

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
    # Create tables if they don't exist
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            fill_id       TEXT PRIMARY KEY,
            order_id      TEXT NOT NULL,
            token_id      TEXT NOT NULL,
            side          TEXT NOT NULL,
            price         REAL NOT NULL,
            size          REAL NOT NULL,
            cost_usdc     REAL NOT NULL,
            fee_usdc      REAL NOT NULL DEFAULT 0,
            realized_pnl  REAL,
            strategy      TEXT NOT NULL DEFAULT '',
            is_paper      INTEGER NOT NULL DEFAULT 1,
            filled_at     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_fills_token    ON fills(token_id);
        CREATE INDEX IF NOT EXISTS idx_fills_strategy ON fills(strategy);
        CREATE INDEX IF NOT EXISTS idx_fills_time     ON fills(filled_at);

        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cash_usdc       REAL NOT NULL,
            total_value     REAL NOT NULL,
            realized_pnl    REAL NOT NULL,
            unrealized_pnl  REAL NOT NULL,
            fees_paid       REAL NOT NULL DEFAULT 0,
            trade_count     INTEGER NOT NULL,
            win_count       INTEGER NOT NULL,
            taken_at        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(taken_at);

        CREATE TABLE IF NOT EXISTS agent_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT NOT NULL,  -- research | signal | skip
            strategy     TEXT NOT NULL DEFAULT 'ai_research',
            condition_id TEXT NOT NULL,
            question     TEXT NOT NULL,
            gemini_p     REAL,           -- AI estimated probability
            market_p     REAL,           -- market mid price at event time
            edge         REAL,           -- gemini_p - market_p
            confidence   TEXT,
            reasoning    TEXT,
            key_factors  TEXT,           -- JSON array
            decision     TEXT,           -- BUY_YES | BUY_NO | EXIT | SKIP_EDGE | SKIP_CONF | SKIP_POSITION
            occurred_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_events_time     ON agent_events(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_agent_events_strategy ON agent_events(strategy);
    """)
    await conn.commit()

    # Additive migrations for pre-existing databases
    await _add_column_if_missing(conn, "fills", "realized_pnl", "REAL")
    await _add_column_if_missing(conn, "snapshots", "fees_paid", "REAL NOT NULL DEFAULT 0")


async def _add_column_if_missing(
    conn: aiosqlite.Connection, table: str, column: str, col_def: str
) -> None:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if column not in cols:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        await conn.commit()
        log.info("db.migration_applied", table=table, column=column)
