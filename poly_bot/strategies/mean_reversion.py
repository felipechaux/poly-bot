"""
Mean Reversion Strategy
-----------------------
Fades genuine news-driven overreactions in binary prediction markets.

Entry logic (changed from previous version):
  - Buys YES when 24h price DROPPED >= drop_threshold_pct AND mid is still
    in a meaningful uncertainty range (entry_low–entry_high, e.g. 0.20–0.45).
    This catches markets that overreacted to bad news but are still live.
  - Symmetric: buys NO when 24h price SPIKED >= drop_threshold_pct AND
    mid is in the inverse range (0.55–0.80).

Previous version (WRONG): bought when price was simply cheap (< 7%).
A 7% market is priced at 7% because it is a 7% event — that is not oversold.
Only buy when there is evidence of a recent sharp move that may be an overreaction.

Exit logic:
  - Profit target: exit when mid >= entry_price × (1 + profit_target_pct)
  - Stop-loss and max-hold still apply as safety nets.

Paper trading validation metrics to watch:
  - Win rate should be > 55%
  - Average win should be > average loss (positive expectancy)
  - Check that price_change signal is genuinely present at entry
"""

from __future__ import annotations

from datetime import datetime, timezone

from poly_bot.execution.models import Fill
from poly_bot.strategies.base import Signal, Strategy, StrategyContext


class MeanReversionStrategy(Strategy):
    """Fade news-driven overreactions in binary prediction markets."""

    @property
    def name(self) -> str:
        return "mean_reversion"

    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        mid = ctx.mid_price
        if mid is None:
            return []

        drop_threshold: float = self._param("drop_threshold_pct", 15.0)
        entry_low: float = self._param("entry_low", 0.20)
        entry_high: float = self._param("entry_high", 0.45)
        profit_target_pct: float = self._param("profit_target_pct", 0.15)
        size_usdc: float = self._param("position_size_usdc", 50.0)
        stop_loss_pct: float = self._param("stop_loss_pct", 0.20)
        max_hold_days: float = self._param("max_hold_days", 7)

        price_change = ctx.market.one_day_price_change  # % change, e.g. -20.0 means -20%

        signals: list[Signal] = []
        position = ctx.position
        yes_token = ctx.market.yes_token
        no_token = ctx.market.no_token

        # --- Stop-loss / max hold on open position ---
        if position is not None:
            bid = ctx.best_bid or mid
            current_value = (
                bid * (position.total_cost_usdc / position.avg_cost_basis)
                if position.avg_cost_basis > 0
                else 0.0
            )
            loss_pct = (
                (position.total_cost_usdc - current_value) / position.total_cost_usdc
                if position.total_cost_usdc > 0
                else 0.0
            )
            now = datetime.now(timezone.utc)
            opened = position.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_days = (now - opened).total_seconds() / 86400

            if loss_pct >= stop_loss_pct:
                return [Signal(
                    token_id=position.token_id,
                    side="SELL",
                    price=bid,
                    size_usdc=position.total_cost_usdc,
                    rationale=f"Stop-loss: down {loss_pct:.0%} (limit {stop_loss_pct:.0%})",
                )]
            if hold_days >= max_hold_days:
                return [Signal(
                    token_id=position.token_id,
                    side="SELL",
                    price=bid,
                    size_usdc=position.total_cost_usdc,
                    rationale=f"Max hold: {hold_days:.1f}d >= {max_hold_days}d limit",
                )]

        # --- Exit: YES position reached profit target ---
        if position and position.side == "BUY" and position.avg_cost_basis > 0:
            profit_target = position.avg_cost_basis * (1 + profit_target_pct)
            if mid >= profit_target:
                bid = ctx.best_bid or mid
                return [Signal(
                    token_id=position.token_id,
                    side="SELL",
                    price=bid,
                    size_usdc=position.total_cost_usdc,
                    rationale=(
                        f"Profit target: mid={mid:.3f} >= "
                        f"entry={position.avg_cost_basis:.3f} × {1 + profit_target_pct:.2f}"
                    ),
                )]

        # --- Entry: sharp DROP → potential YES overreaction ---
        # e.g. 24h change = -18% AND mid in [0.20, 0.45]
        if (
            price_change <= -drop_threshold
            and entry_low <= mid <= entry_high
            and yes_token
            and position is None
        ):
            ask = ctx.best_ask or mid
            signals.append(Signal(
                token_id=yes_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                rationale=(
                    f"YES overreaction: {price_change:+.1f}% 24h drop, mid={mid:.3f}"
                ),
            ))

        # --- Entry: sharp SPIKE → potential NO overreaction ---
        # e.g. 24h change = +18% AND mid in [0.55, 0.80]
        elif (
            price_change >= drop_threshold
            and (1 - entry_high) <= mid <= (1 - entry_low)
            and no_token
            and position is None
        ):
            no_price = 1.0 - mid
            signals.append(Signal(
                token_id=no_token.token_id,
                side="BUY",
                price=no_price,
                size_usdc=size_usdc,
                rationale=(
                    f"NO overreaction: YES +{price_change:.1f}% 24h spike, "
                    f"NO at {no_price:.3f}"
                ),
            ))

        return signals

    async def on_fill(self, fill: Fill, ctx: StrategyContext) -> list[Signal]:  # noqa: ARG002
        return []
