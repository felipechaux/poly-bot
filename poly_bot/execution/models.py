"""
Execution layer data models: orders, fills, execution reports.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["GTC", "FOK", "GTD"]
OrderStatus = Literal["open", "filled", "partial", "cancelled", "rejected"]


class OrderRequest(BaseModel):
    """A request to place an order, emitted by a strategy as a Signal."""

    token_id: str
    side: OrderSide
    price: float
    size_usdc: float  # Amount of USDC to spend/receive
    order_type: OrderType = "GTC"
    strategy: str = ""
    rationale: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Fill(BaseModel):
    """A confirmed fill event (partial or complete)."""

    fill_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    token_id: str
    side: OrderSide
    price: float
    size: float  # Shares filled
    cost_usdc: float  # USDC spent/received
    fee_usdc: float = 0.0
    realized_pnl: float | None = None  # Set on SELL fills
    strategy: str = ""
    filled_at: datetime = Field(default_factory=datetime.utcnow)
    is_paper: bool = True


class ExecutionReport(BaseModel):
    """Result of submitting an order."""

    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request: OrderRequest
    status: OrderStatus
    fills: list[Fill] = Field(default_factory=list)
    rejection_reason: str = ""
    submitted_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def total_filled_usdc(self) -> float:
        return sum(f.cost_usdc for f in self.fills)

    @property
    def total_fees(self) -> float:
        return sum(f.fee_usdc for f in self.fills)
