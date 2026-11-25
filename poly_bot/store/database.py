"""
SQLite database (aiosqlite). Schema is created on startup.

Uses a new file (data/invest_bot.db) — the old Polymarket data in data/poly_bot.db
is left untouched as an archive.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

DB_PATH = Path("data/invest_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,  -- replay order
    tx_id          TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL,      -- DEPOSIT | BUY | SELL | FX | DIVIDEND
    currency       TEXT NOT NULL,
    ticker         TEXT NOT NULL DEFAULT '',
    quantity       REAL NOT NULL DEFAULT 0,
    price          REAL NOT NULL DEFAULT 0,
    gross          REAL NOT NULL DEFAULT 0,
    fee            REAL NOT NULL DEFAULT 0,
    realized_pnl   REAL,
    from_currency  TEXT,
    from_amount    REAL NOT NULL DEFAULT 0,
    strategy       TEXT NOT NULL DEFAULT '',
    rationale      TEXT NOT NULL DEFAULT '',
    is_paper       INTEGER NOT NULL DEFAULT 1,
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_ticker ON transactions(ticker);
CREATE INDEX IF NOT EXISTS idx_tx_time   ON transactions(occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_dividend
    ON transactions(ticker, occurred_at) WHERE kind = 'DIVIDEND';

CREATE TABLE IF NOT EXISTS snapshots (
    day             TEXT PRIMARY KEY,   -- YYYY-MM-DD (Bogotá); one per day, last wins
    taken_at        TEXT NOT NULL,
    value_base      REAL NOT NULL,      -- total in base currency (COP)
    value_usd       REAL NOT NULL,
    usdcop          REAL NOT NULL,
    cash_cop        REAL NOT NULL,
    cash_usd        REAL NOT NULL,
    invested_base   REAL NOT NULL,
    unrealized_base REAL NOT NULL,
    realized_base   REAL NOT NULL,
    fees_base       REAL NOT NULL,
    dividends_base  REAL NOT NULL,
    benchmarks      TEXT NOT NULL DEFAULT '{}'   -- JSON {ticker: close}
);

CREATE TABLE IF NOT EXISTS quotes (
    ticker      TEXT PRIMARY KEY,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    indicators  TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,     -- signals | rejected | cycle | error | analysis
    strategy     TEXT NOT NULL DEFAULT '',
    ticker       TEXT NOT NULL DEFAULT '',
    decision     TEXT NOT NULL DEFAULT '',
    reasoning    TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',
    occurred_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time     ON agent_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_strategy ON agent_events(strategy, event_type);

-- Orders decided before the open, filled later at the next session's opening price
CREATE TABLE IF NOT EXISTS pending_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at  TEXT NOT NULL,      -- UTC ISO; fills only at an open strictly after this
    strategy    TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,      -- BUY | SELL
    value       REAL,               -- asset currency (BUY)
    quantity    REAL,               -- shares (SELL / sell-all)
    rationale   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | filled | rejected | expired | superseded
    note        TEXT NOT NULL DEFAULT '',
    tx_id       TEXT,
    fill_day    TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_orders(status);
"""


async def init_db(path: Path | str = DB_PATH) -> aiosqlite.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(SCHEMA)
    await conn.commit()
    log.info("db.initialized", path=str(path))
    return conn
