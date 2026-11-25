"""
Execution models.

Every change to the paper portfolio is a Transaction. The portfolio state is rebuilt
by replaying transactions in order, so the bot survives restarts with no extra state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from poly_bot.market_data.models import Currency

OrderSide = Literal["BUY", "SELL"]
TxKind = Literal["DEPOSIT", "BUY", "SELL", "FX", "DIVIDEND"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderRequest(BaseModel):
    """An order to buy/sell `value` (asset currency) or `quantity` shares of a ticker."""

    ticker: str
    side: OrderSide
    value: float | None = None
    quantity: float | None = None
    strategy: str = ""
    rationale: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class Transaction(BaseModel):
    tx_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: TxKind
    currency: Currency  # currency of `gross` / `fee` (for FX: the destination currency)
    ticker: str = ""
    quantity: float = 0.0
    price: float = 0.0  # per share (BUY/SELL/DIVIDEND); FX rate for FX
    gross: float = 0.0  # quantity * price; DEPOSIT/FX: amount credited
    fee: float = 0.0  # commission (BUY/SELL), withholding tax (DIVIDEND), spread cost (FX)
    realized_pnl: float | None = None  # SELL only, asset currency
    from_currency: Currency | None = None  # FX only
    from_amount: float = 0.0  # FX only: amount debited in from_currency
    strategy: str = ""
    rationale: str = ""
    is_paper: bool = True
    occurred_at: datetime = Field(default_factory=utcnow)

    @property
    def cash_delta(self) -> dict[str, float]:
        """Effect of this transaction on cash balances."""
        if self.kind == "DEPOSIT":
            return {self.currency: self.gross}
        if self.kind == "BUY":
            return {self.currency: -(self.gross + self.fee)}
        if self.kind == "SELL":
            return {self.currency: self.gross - self.fee}
        if self.kind == "DIVIDEND":
            return {self.currency: self.gross - self.fee}
        if self.kind == "FX":
            assert self.from_currency is not None
            return {self.from_currency: -self.from_amount, self.currency: self.gross}
        return {}


class ExecutionReport(BaseModel):
    request: OrderRequest
    status: Literal["filled", "rejected"]
    transaction: Transaction | None = None
    rejection_reason: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"
