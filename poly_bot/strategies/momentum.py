"""
Momentum Strategy
-----------------
Follows recent price direction using the Gamma API's oneDayPriceChange field.
Buys markets showing strong positive momentum.

Best used for: markets with clear trend following potential (crypto event markets,
breaking news driven markets).

Paper trading notes:
- Momentum in prediction markets often reverses quickly after news incorporation
- Use a tight exit_pct to lock in gains before reversal
"""

from __future__ import annotations

from poly_bot.strategies.base import Signal, Strategy, StrategyContext


class MomentumStrategy(Strategy):
    """Follow strong price momentum in prediction markets."""

    @property
    def name(self) -> str:
        return "momentum"

    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        mid = ctx.mid_price
        if mid is None:
            return []

        min_change: float = self._param("min_change_pct", 5.0)
        reversal_pct: float = self._param("reversal_pct", 2.0)
        size_usdc: float = self._param("position_size_usdc", 100.0)

        price_change = ctx.market.one_day_price_change  # % change from Gamma API
        yes_token = ctx.market.yes_token
        position = ctx.position

        signals: list[Signal] = []

        # --- Entry: strong upward momentum in YES ---
        if (
            price_change >= min_change
            and yes_token
            and position is None
            and 0.1 < mid < 0.9  # avoid near-resolved markets
        ):
            ask = ctx.best_ask or mid
            signals.append(Signal(
                token_id=yes_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                rationale=f"Momentum: {price_change:+.1f}% daily change, mid={mid:.3f}",
            ))

        # --- Exit: momentum reversed ---
        elif position and position.side == "BUY":
            if price_change <= -reversal_pct:
                bid = ctx.best_bid or mid
                signals.append(Signal(
                    token_id=position.token_id,
                    side="SELL",
                    price=bid,
                    size_usdc=position.total_cost_usdc,
                    rationale=f"Momentum reversed: {price_change:+.1f}%",
                ))

        return signals
