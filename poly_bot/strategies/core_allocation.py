"""
Core allocation — diversified fixed-weight portfolio with monthly rebalancing.

Rebalances when:
  - the strategy has never traded (initial allocation), or
  - it is the first cycle of a new calendar month, or
  - any target drifts more than `drift_band` (absolute weight) from its target.

Positions this strategy owns that are no longer in `targets` are sold.
`portfolio_share` < 1 leaves room for satellite strategies (e.g. llm_analyst).
Order sizes are differences between target and current value; tiny orders below the
risk manager's minimum are dropped there.
"""

from __future__ import annotations

from typing import Any

from poly_bot.strategies.base import DailyContext, Signal, Strategy


class CoreAllocationStrategy(Strategy):
    @property
    def name(self) -> str:
        return "core_allocation"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        raw = self._param("targets", {}) or {}
        self.targets: dict[str, float] = {str(k): float(v) for k, v in raw.items()}
        self.drift_band = float(self._param("drift_band", 0.05))
        self.cash_buffer = float(self._param("cash_buffer_pct", 0.01))
        # Fraction of the whole portfolio the core manages (rest = satellite sleeves)
        self.portfolio_share = float(self._param("portfolio_share", 1.0))
        if not 0 < self.portfolio_share <= 1:
            raise ValueError("core_allocation portfolio_share must be in (0, 1]")
        total = sum(self.targets.values())
        if not self.targets or abs(total - 1.0) > 1e-6:
            raise ValueError(f"core_allocation targets must sum to 1.0 (got {total:.4f})")

    def validate(self, universe: Any) -> None:
        missing = [t for t in self.targets if t not in universe]
        if missing:
            raise ValueError(f"core_allocation targets not in universe: {missing}")

    def rebalance_reason(self, ctx: DailyContext) -> str | None:
        if ctx.last_signal_date is None:
            return "asignación inicial"
        if (ctx.today.year, ctx.today.month) != (ctx.last_signal_date.year, ctx.last_signal_date.month):
            return "rebalanceo mensual"
        snap = ctx.portfolio
        for t, w in self.target_weights().items():
            drift = snap.weight(t) - w
            if abs(drift) > self.drift_band:
                return f"desviación de {t} ({drift:+.1%})"
        return None

    def target_weights(self) -> dict[str, float]:
        scale = self.portfolio_share * (1 - self.cash_buffer)
        return {t: w * scale for t, w in self.targets.items()}

    async def on_daily(self, ctx: DailyContext) -> list[Signal]:
        reason = self.rebalance_reason(ctx)
        if reason is None:
            return []

        snap = ctx.portfolio
        total = snap.total_value()
        if total <= 0:
            return []

        sells: list[Signal] = []
        buys: list[Signal] = []

        # Exit positions owned by this strategy that are no longer targeted
        for p in snap.positions:
            if p.strategy == self.name and p.ticker not in self.targets:
                sells.append(Signal(p.ticker, "SELL", quantity=p.quantity,
                                    rationale=f"{reason}: ya no está en la asignación"))

        for ticker, w in self.target_weights().items():
            quote = ctx.quotes.get(ticker)
            if quote is None:
                continue
            current_base = snap.value_of(ticker)
            diff_base = w * total - current_base
            diff_ccy = snap.to(diff_base, snap.base_currency, quote.currency)
            cur_w = current_base / total
            note = f"{reason}: {cur_w:.1%} → {w:.1%}"
            if diff_ccy > 0:
                buys.append(Signal(ticker, "BUY", value=diff_ccy, rationale=note))
            elif diff_ccy < 0:
                pos = snap.get_position(ticker)
                if pos is None:
                    continue
                qty = min(pos.quantity, -diff_ccy / quote.price)
                sells.append(Signal(ticker, "SELL", quantity=qty, rationale=note))

        # Largest first so the most important legs get cash first
        buys.sort(key=lambda s: -(snap.to(s.value or 0.0, ctx.quotes[s.ticker].currency)))
        return sells + buys
