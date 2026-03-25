"""Portfolio-level data models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Position(BaseModel):
    """An open position in a specific token."""

    token_id: str
    market_question: str = ""
    outcome: str = ""  # "Yes" or "No"
    side: str = "BUY"
    shares: float = 0.0
    avg_cost_basis: float = 0.0  # Average price paid per share
    total_cost_usdc: float = 0.0
    realized_pnl: float = 0.0
    strategy: str = ""
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    @property
    def unrealized_pnl(self, current_price: float = 0.0) -> float:
        """Unrealized P&L — requires current_price to be set externally."""
        return 0.0  # Computed by PositionTracker.snapshot() with live prices


class PortfolioSnapshot(BaseModel):
    """Point-in-time snapshot of portfolio state."""

    cash_usdc: float
    positions: list[Position] = Field(default_factory=list)
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    trade_count: int = 0
    taken_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def total_pnl(self) -> float:
        return self.total_realized_pnl + self.total_unrealized_pnl

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.0

    @property
    def total_position_value(self) -> float:
        return sum(p.total_cost_usdc for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.cash_usdc + self.total_position_value

    def get_position(self, token_id: str) -> Position | None:
        for p in self.positions:
            if p.token_id == token_id:
                return p
        return None
