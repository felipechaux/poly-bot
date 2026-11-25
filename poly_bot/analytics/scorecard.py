"""
Analyst scorecard — is an LLM analyst picking better than chance?

Each scored recommendation is compared with an equal-weight basket of the tickers the analyst
could have picked (universe minus core) over the same window: excess = idea return − basket
return (sign flipped for SELL ideas). The traffic light ("semáforo") is a one-sample t-test on
the excess returns at the main horizon:

  gris      fewer than `min_n` scored ideas — not enough evidence either way
  verde     mean excess > 0 and t ≥ t_threshold
  rojo      mean excess < 0 and t ≤ −t_threshold
  amarillo  anything in between

Caveat: windows of ideas made on consecutive days overlap, so the t-statistic overstates
confidence; the default threshold (2.0) and minimum sample (20) are deliberately conservative.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from poly_bot.execution.models import Transaction
    from poly_bot.portfolio.models import PortfolioSnapshot


def close_on_or_before(series: pd.Series | None, day: date) -> float | None:
    if series is None or series.empty:
        return None
    s = series[series.index <= pd.Timestamp(day)]
    return float(s.iloc[-1]) if not s.empty else None


def basket_return(closes: dict[str, pd.Series], tickers: list[str], start: date) -> float | None:
    """Equal-weight return from the close on/before `start` to the latest close."""
    rets = []
    for t in tickers:
        s = closes.get(t)
        p0 = close_on_or_before(s, start)
        if p0 and s is not None and not s.empty:
            rets.append(float(s.iloc[-1]) / p0 - 1)
    return sum(rets) / len(rets) if rets else None


@dataclass
class Semaforo:
    color: str  # gris | verde | amarillo | rojo
    n: int
    mean_excess: float | None
    t_stat: float | None
    horizon_days: int
    label: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def semaforo(excess: list[float], horizon_days: int = 7, min_n: int = 20,
             t_threshold: float = 2.0) -> Semaforo:
    n = len(excess)
    if n == 0:
        return Semaforo("gris", 0, None, None, horizon_days, "sin ideas calificadas aún")
    mean = sum(excess) / n
    if n < 2:
        t = None
    else:
        var = sum((x - mean) ** 2 for x in excess) / (n - 1)
        t = mean / math.sqrt(var / n) if var > 0 else (math.inf if mean > 0 else -math.inf if mean < 0 else 0.0)
    if n < min_n:
        return Semaforo("gris", n, mean, t, horizon_days, f"faltan datos ({n}/{min_n} ideas)")
    assert t is not None
    if mean > 0 and t >= t_threshold:
        return Semaforo("verde", n, mean, t, horizon_days, "le gana al azar con evidencia")
    if mean < 0 and t <= -t_threshold:
        return Semaforo("rojo", n, mean, t, horizon_days, "pierde contra el azar con evidencia")
    return Semaforo("amarillo", n, mean, t, horizon_days, "sin ventaja clara")


def semaforo_for(outcomes: list[dict[str, Any]], strategy: str, horizon_days: int = 7,
                 min_n: int = 20, t_threshold: float = 2.0) -> Semaforo:
    excess = [float(o["payload"]["excess"]) for o in outcomes
              if o["strategy"] == strategy and o["payload"].get("horizon_days") == horizon_days
              and o["payload"].get("excess") is not None]
    return semaforo(excess, horizon_days, min_n, t_threshold)


def sleeve_stats(strategy: str, snap: "PortfolioSnapshot", txs: list["Transaction"],
                 sleeve_pct: float) -> dict[str, Any]:
    """Sleeve P&L in base currency: unrealized + realized + net dividends, vs its cap."""
    pos = [p for p in snap.positions if p.strategy == strategy]
    unrealized = sum(snap.to(p.unrealized_pnl, p.currency) for p in pos)
    realized = sum(snap.to(t.realized_pnl or 0.0, t.currency) for t in txs
                   if t.strategy == strategy and t.kind == "SELL")
    dividends = sum(snap.to(t.gross - t.fee, t.currency) for t in txs
                    if t.strategy == strategy and t.kind == "DIVIDEND")
    cap = sleeve_pct * snap.total_value()
    pnl = unrealized + realized + dividends
    return {"cap": cap, "value": sum(snap.value_of(p.ticker) for p in pos),
            "positions": [p.ticker for p in pos], "pnl": pnl,
            "pnl_pct": pnl / cap if cap else None}


def guard_reason(sleeve: dict[str, Any], light: Semaforo, max_drawdown_pct: float) -> str | None:
    """Why an analyst must stop opening positions (None = may trade)."""
    pnl_pct = sleeve.get("pnl_pct")
    if pnl_pct is not None and pnl_pct <= -max_drawdown_pct:
        return f"pérdida de {pnl_pct:.1%} de su cartera (límite −{max_drawdown_pct:.0%})"
    if light.color == "rojo":
        return f"semáforo rojo ({light.n} ideas, exceso medio {light.mean_excess:+.1%})"
    return None
