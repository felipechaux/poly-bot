"""
Position tracker — in-memory portfolio state rebuilt from the transaction ledger.

apply(tx) is the only way state changes; replay(txs) rebuilds after a restart.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from poly_bot.execution.models import Transaction
from poly_bot.market_data.models import Quote
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.models import PortfolioSnapshot, Position

log = get_logger(__name__)

DUST = 1e-9


class PositionTracker:
    def __init__(self, base_currency: str = "COP") -> None:
        self.base_currency = base_currency
        self.cash: dict[str, float] = defaultdict(float)
        self._positions: dict[str, Position] = {}
        self._realized: dict[str, float] = defaultdict(float)
        self._fees: dict[str, float] = defaultdict(float)
        self._dividends: dict[str, float] = defaultdict(float)
        self._deposits: dict[str, float] = defaultdict(float)
        self._trade_count = 0
        self._prices: dict[str, float] = {}
        self.usdcop: float | None = None

    # ------------------------------------------------------------------

    def replay(self, txs: Iterable[Transaction]) -> None:
        n = 0
        for tx in txs:
            self.apply(tx, quiet=True)
            n += 1
        log.info("portfolio.replayed", transactions=n, positions=len(self._positions))

    def apply(self, tx: Transaction, quiet: bool = False) -> None:
        if tx.kind == "BUY":
            self._on_buy(tx)
        elif tx.kind == "SELL":
            self._on_sell(tx)
        elif tx.kind == "DIVIDEND":
            pos = self._positions.get(tx.ticker)
            if pos:
                pos.dividends += tx.gross - tx.fee
            self._dividends[tx.currency] += tx.gross - tx.fee
        elif tx.kind == "DEPOSIT":
            self._deposits[tx.currency] += tx.gross
        for ccy, delta in tx.cash_delta.items():
            self.cash[ccy] += delta
        if tx.kind in ("BUY", "SELL"):
            self._fees[tx.currency] += tx.fee
        if not quiet:
            log.info("portfolio.tx_applied", kind=tx.kind, ticker=tx.ticker, ccy=tx.currency,
                     gross=round(tx.gross, 2), cash=dict(self.cash))

    def _on_buy(self, tx: Transaction) -> None:
        pos = self._positions.get(tx.ticker)
        cost = tx.gross + tx.fee
        if pos is None:
            self._positions[tx.ticker] = Position(
                ticker=tx.ticker,
                currency=tx.currency,
                quantity=tx.quantity,
                avg_cost=cost / tx.quantity,
                strategy=tx.strategy,
                opened_at=tx.occurred_at,
                last_price=self._prices.get(tx.ticker),
            )
        else:
            total_cost = pos.cost_basis + cost
            pos.quantity += tx.quantity
            pos.avg_cost = total_cost / pos.quantity

    def _on_sell(self, tx: Transaction) -> None:
        pos = self._positions.get(tx.ticker)
        if pos is None or pos.quantity + DUST < tx.quantity:
            raise ValueError(f"Cannot sell {tx.quantity} {tx.ticker}: not enough held")
        realized = tx.gross - tx.fee - pos.avg_cost * tx.quantity
        tx.realized_pnl = realized
        pos.realized_pnl += realized
        pos.quantity -= tx.quantity
        self._realized[tx.currency] += realized
        self._trade_count += 1
        if pos.quantity <= DUST:
            del self._positions[tx.ticker]

    # ------------------------------------------------------------------

    def update_prices(self, quotes: dict[str, Quote], usdcop: float) -> None:
        self.usdcop = usdcop
        for t, q in quotes.items():
            self._prices[t] = q.price
            if t in self._positions:
                self._positions[t].last_price = q.price

    def get_position(self, ticker: str) -> Position | None:
        return self._positions.get(ticker)

    def held_quantity(self, ticker: str) -> float:
        pos = self._positions.get(ticker)
        return pos.quantity if pos else 0.0

    @property
    def has_deposits(self) -> bool:
        return any(v > 0 for v in self._deposits.values())

    def snapshot(self) -> PortfolioSnapshot:
        if self.usdcop is None:
            raise RuntimeError("No FX rate yet — call update_prices() first")
        return PortfolioSnapshot(
            cash=dict(self.cash),
            positions=[p.model_copy() for p in self._positions.values()],
            usdcop=self.usdcop,
            base_currency=self.base_currency,  # type: ignore[arg-type]
            realized_pnl=dict(self._realized),
            fees=dict(self._fees),
            dividends=dict(self._dividends),
            deposits=dict(self._deposits),
            trade_count=self._trade_count,
        )
