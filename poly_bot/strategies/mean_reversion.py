"""
Mean Reversion Strategy
-----------------------
Binary prediction markets often overshoot near their resolution boundaries.
This strategy fades extremes:
  - Buys YES when price < low_threshold (e.g., 0.07) — market oversold
  - Exits when price reverts to > exit_threshold (e.g., 0.15)
  - Symmetric logic on the high end (buy NO when YES > high_threshold)

Paper trading validation metrics to watch:
  - Win rate should be > 55% to be worthwhile
  - Average win should be > average loss (positive expectancy)
  - Check that fills are happening (not too illiquid)
"""

from __future__ import annotations

from typing import Any

from poly_bot.execution.models import Fill
from poly_bot.strategies.base import Signal, Strategy, StrategyContext


class MeanReversionStrategy(Strategy):
    """Fade price extremes in binary prediction markets."""

    @property
    def name(self) -> str:
        return "mean_reversion"

    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        mid = ctx.mid_price
        if mid is None:
            return []

        low_threshold: float = self._param("low_threshold", 0.07)
        high_threshold: float = self._param("high_threshold", 0.93)
        exit_threshold: float = self._param("exit_threshold", 0.15)
        size_usdc: float = self._param("position_size_usdc", 100.0)

        signals: list[Signal] = []
        position = ctx.position
        yes_token = ctx.market.yes_token
        no_token = ctx.market.no_token

        # --- Entry: YES token is very cheap ---
        if mid < low_threshold and yes_token and position is None:
            # Buy YES at best ask (we're buying a cheap outcome)
            ask = ctx.best_ask or mid
            signals.append(Signal(
                token_id=yes_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                rationale=f"YES oversold: mid={mid:.3f} < threshold={low_threshold:.3f}",
            ))

        # --- Exit: YES position, price reverted ---
        elif position and position.side == "BUY" and mid > exit_threshold:
            bid = ctx.best_bid or mid
            signals.append(Signal(
                token_id=position.token_id,
                side="SELL",
                price=bid,
                size_usdc=position.total_cost_usdc,
                rationale=f"YES reverted: mid={mid:.3f} > exit={exit_threshold:.3f}",
            ))

        # --- Entry: YES token is very expensive (buy NO instead) ---
        elif mid > high_threshold and no_token and position is None:
            no_price = 1.0 - mid  # NO price = 1 - YES price
            no_ask = no_price  # simplified; ideally fetch NO order book
            signals.append(Signal(
                token_id=no_token.token_id,
                side="BUY",
                price=no_ask,
                size_usdc=size_usdc,
                rationale=f"YES overbought ({mid:.3f}): buying NO at {no_price:.3f}",
            ))

        return signals

    async def on_fill(self, fill: Fill, ctx: StrategyContext) -> list[Signal]:
        """Log fill but no automatic follow-up orders."""
        return []
