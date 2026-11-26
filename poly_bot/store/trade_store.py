"""Persist transactions, daily snapshots, quotes and agent events."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import aiosqlite

from poly_bot.execution.models import Transaction, utcnow
from poly_bot.market_data.models import Quote
from poly_bot.portfolio.models import PortfolioSnapshot

TX_COLUMNS = (
    "tx_id", "kind", "currency", "ticker", "quantity", "price", "gross", "fee",
    "realized_pnl", "from_currency", "from_amount", "strategy", "rationale",
    "is_paper", "occurred_at",
)


class TradeStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ── Transactions ────────────────────────────────────────────────────────

    async def save_transaction(self, tx: Transaction) -> None:
        values = tx.model_dump()
        values["is_paper"] = 1 if tx.is_paper else 0
        values["occurred_at"] = tx.occurred_at.isoformat()
        cols = ", ".join(TX_COLUMNS)
        marks = ", ".join("?" for _ in TX_COLUMNS)
        await self._conn.execute(
            f"INSERT INTO transactions ({cols}) VALUES ({marks})",
            tuple(values[c] for c in TX_COLUMNS),
        )
        await self._conn.commit()

    async def all_transactions(self) -> list[Transaction]:
        cur = await self._conn.execute(
            f"SELECT {', '.join(TX_COLUMNS)} FROM transactions ORDER BY seq"
        )
        rows = await cur.fetchall()
        return [
            Transaction(**{**dict(r), "is_paper": bool(r["is_paper"]),
                           "occurred_at": datetime.fromisoformat(r["occurred_at"])})
            for r in rows
        ]

    async def recent_transactions(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM transactions ORDER BY seq DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def transactions_since(self, since: datetime) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM transactions WHERE occurred_at >= ? ORDER BY seq",
            (since.isoformat(),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def dividend_recorded(self, ticker: str, ex_date: date) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM transactions WHERE kind='DIVIDEND' AND ticker=? AND occurred_at LIKE ?",
            (ticker, f"{ex_date.isoformat()}%"),
        )
        return await cur.fetchone() is not None

    # ── Snapshots ───────────────────────────────────────────────────────────

    async def save_snapshot(self, day: date, snap: PortfolioSnapshot,
                            benchmarks: dict[str, float]) -> None:
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO snapshots
                (day, taken_at, value_base, value_usd, usdcop, cash_cop, cash_usd,
                 invested_base, unrealized_base, realized_base, fees_base,
                 dividends_base, benchmarks)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day.isoformat(),
                snap.taken_at.isoformat(),
                snap.total_value(),
                snap.total_value("USD"),
                snap.usdcop,
                snap.cash.get("COP", 0.0),
                snap.cash.get("USD", 0.0),
                snap.invested(),
                snap.unrealized_pnl(),
                snap.realized(),
                snap.total_fees(),
                snap.total_dividends(),
                json.dumps(benchmarks),
            ),
        )
        await self._conn.commit()

    async def snapshots(self, limit: int = 2000) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM (SELECT * FROM snapshots ORDER BY day DESC LIMIT ?) ORDER BY day",
            (limit,),
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["benchmarks"] = json.loads(d["benchmarks"] or "{}")
            out.append(d)
        return out

    async def snapshot_before(self, day: date) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT * FROM snapshots WHERE day < ? ORDER BY day DESC LIMIT 1", (day.isoformat(),)
        )
        r = await cur.fetchone()
        return dict(r) if r else None

    async def first_snapshot(self) -> dict[str, Any] | None:
        cur = await self._conn.execute("SELECT * FROM snapshots ORDER BY day LIMIT 1")
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["benchmarks"] = json.loads(d["benchmarks"] or "{}")
        return d

    # ── Quotes ──────────────────────────────────────────────────────────────

    async def save_quotes(self, quotes: dict[str, Quote]) -> None:
        now = utcnow().isoformat()
        await self._conn.executemany(
            "INSERT OR REPLACE INTO quotes (ticker, price, currency, as_of, indicators, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (q.ticker, q.price, q.currency, q.as_of.isoformat(),
                 q.indicators.model_dump_json(), now)
                for q in quotes.values()
            ],
        )
        await self._conn.commit()

    async def load_quotes(self) -> dict[str, Quote]:
        cur = await self._conn.execute("SELECT * FROM quotes")
        out: dict[str, Quote] = {}
        for r in await cur.fetchall():
            out[r["ticker"]] = Quote(
                ticker=r["ticker"], price=r["price"], currency=r["currency"],
                as_of=date.fromisoformat(r["as_of"]),
                indicators=json.loads(r["indicators"]),
            )
        return out

    # ── Agent events ────────────────────────────────────────────────────────

    async def save_event(self, event_type: str, strategy: str = "", ticker: str = "",
                         decision: str = "", reasoning: str = "",
                         payload: dict[str, Any] | None = None,
                         occurred_at: datetime | None = None) -> None:
        await self._conn.execute(
            "INSERT INTO agent_events (event_type, strategy, ticker, decision, reasoning,"
            " payload, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, strategy, ticker, decision, reasoning,
             json.dumps(payload or {}, default=str),
             (occurred_at or utcnow()).isoformat()),
        )
        await self._conn.commit()

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    async def last_event(self, strategy: str, event_type: str) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT * FROM agent_events WHERE strategy=? AND event_type=? ORDER BY id DESC LIMIT 1",
            (strategy, event_type),
        )
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    async def events(self, event_type: str, since: datetime | None = None,
                     strategy: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_events WHERE event_type=?"
        args: list[Any] = [event_type]
        if since is not None:
            sql += " AND occurred_at >= ?"
            args.append(since.isoformat())
        if strategy is not None:
            sql += " AND strategy=?"
            args.append(strategy)
        cur = await self._conn.execute(sql + " ORDER BY id", args)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    async def last_event_time(self, strategy: str, event_type: str) -> datetime | None:
        cur = await self._conn.execute(
            "SELECT occurred_at FROM agent_events WHERE strategy=? AND event_type=?"
            " ORDER BY id DESC LIMIT 1",
            (strategy, event_type),
        )
        r = await cur.fetchone()
        return datetime.fromisoformat(r[0]) if r else None

    # ── Pending orders (next-open fills) ────────────────────────────────

    async def supersede_pending(self, strategy: str, keep: set[int] | None = None) -> int:
        """A new decision replaces any order the strategy still has waiting (except `keep`:
        orders whose opening session already started, i.e. conceptually already filled)."""
        sql = ("UPDATE pending_orders SET status='superseded', note='reemplazada por una nueva"
               " decisión', updated_at=? WHERE strategy=? AND status='pending'")
        args: list[Any] = [utcnow().isoformat(), strategy]
        if keep:
            sql += f" AND id NOT IN ({','.join('?' * len(keep))})"
            args += sorted(keep)
        cur = await self._conn.execute(sql, args)
        await self._conn.commit()
        return cur.rowcount or 0

    async def add_pending(self, decided_at: datetime, strategy: str, ticker: str, side: str,
                          value: float | None, quantity: float | None, rationale: str) -> int:
        cur = await self._conn.execute(
            "INSERT INTO pending_orders (decided_at, strategy, ticker, side, value, quantity,"
            " rationale, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decided_at.isoformat(), strategy, ticker, side, value, quantity, rationale,
             utcnow().isoformat()),
        )
        await self._conn.commit()
        return int(cur.lastrowid or 0)

    async def pending_orders(self, status: str | None = "pending", limit: int = 200) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM pending_orders", []
        if status is not None:
            sql += " WHERE status=?"
            args.append(status)
        cur = await self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit])
        return [dict(r) for r in reversed(list(await cur.fetchall()))]

    async def close_pending(self, order_id: int, status: str, note: str = "",
                            tx_id: str | None = None, fill_day: date | None = None) -> None:
        await self._conn.execute(
            "UPDATE pending_orders SET status=?, note=?, tx_id=?, fill_day=?, updated_at=? WHERE id=?",
            (status, note[:300], tx_id, fill_day.isoformat() if fill_day else None,
             utcnow().isoformat(), order_id),
        )
        await self._conn.commit()
