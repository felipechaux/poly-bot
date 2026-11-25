"""Daily summary text (Spanish) for ntfy and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from poly_bot.market_data.models import Quote
from poly_bot.market_data.universe import Universe
from poly_bot.portfolio.models import PortfolioSnapshot
from poly_bot.portfolio.pnl import fmt_money, fmt_pct

MAX_TRADES_LISTED = 8


@dataclass
class SummaryInput:
    snap: PortfolioSnapshot
    quotes: dict[str, Quote]
    universe: Universe
    prev_snapshot: dict[str, Any] | None = None  # yesterday's row from `snapshots`
    first_snapshot: dict[str, Any] | None = None
    benchmarks_now: dict[str, float] = field(default_factory=dict)
    transactions_today: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def since_start_return(inp: SummaryInput) -> float | None:
    """Return vs deposits valued at the FX rate of the first snapshot."""
    first = inp.first_snapshot
    if not first:
        return None
    fx0 = first["usdcop"]
    base = inp.snap.base_currency
    invested0 = sum(
        amt if ccy == base else (amt * fx0 if ccy == "USD" else amt / fx0)
        for ccy, amt in inp.snap.deposits.items()
    )
    return inp.snap.total_value() / invested0 - 1 if invested0 > 0 else None


def benchmark_returns(inp: SummaryInput) -> dict[str, float]:
    """Benchmark buy-and-hold return since the first snapshot, in base currency."""
    first = inp.first_snapshot
    if not first:
        return {}
    out: dict[str, float] = {}
    for a in inp.universe.benchmarks:
        p0 = first["benchmarks"].get(a.ticker)
        p1 = inp.benchmarks_now.get(a.ticker)
        if not p0 or not p1:
            continue
        r = p1 / p0
        if a.currency == "USD" and inp.snap.base_currency == "COP":
            r *= inp.snap.usdcop / first["usdcop"]
        out[a.ticker] = r - 1
    return out


def build_daily_summary(inp: SummaryInput) -> tuple[str, str, list[str]]:
    snap = inp.snap
    total_cop = snap.total_value("COP")
    total_usd = snap.total_value("USD")

    day_ret = None
    if inp.prev_snapshot and inp.prev_snapshot["value_base"] > 0:
        day_ret = snap.total_value() / inp.prev_snapshot["value_base"] - 1

    lines: list[str] = []
    lines.append(f"💼 {fmt_money(total_cop, 'COP')} · {fmt_money(total_usd, 'USD')}")

    start_ret = since_start_return(inp)
    bench = benchmark_returns(inp)
    bench_txt = ", ".join(
        f"{t.replace('.CL', '')} {fmt_pct(r)}" for t, r in bench.items()
    )
    perf = f"📈 Hoy {fmt_pct(day_ret)} · Desde inicio {fmt_pct(start_ret)}"
    if bench_txt:
        perf += f" (vs {bench_txt})"
    lines.append(perf)
    lines.append(
        f"💵 TRM {snap.usdcop:,.2f} · Caja {fmt_money(snap.cash.get('COP', 0), 'COP')}"
        f" + {fmt_money(snap.cash.get('USD', 0), 'USD')}"
    )

    trades = [t for t in inp.transactions_today if t["kind"] in ("BUY", "SELL", "FX", "DIVIDEND")]
    if trades:
        lines.append(f"🔁 Movimientos ({len(trades)}):")
        for t in trades[:MAX_TRADES_LISTED]:
            if t["kind"] == "FX":
                lines.append(f"  • FX {t['from_currency']}→{t['currency']} "
                             f"{fmt_money(t['gross'], t['currency'])}")
            elif t["kind"] == "DIVIDEND":
                lines.append(f"  • Dividendo {t['ticker']} {fmt_money(t['gross'] - t['fee'], t['currency'])}")
            else:
                verb = "Compra" if t["kind"] == "BUY" else "Venta"
                lines.append(f"  • {verb} {t['ticker']} {t['quantity']:g} @ {t['price']:,.2f}"
                             f" = {fmt_money(t['gross'], t['currency'])}")
        if len(trades) > MAX_TRADES_LISTED:
            lines.append(f"  … y {len(trades) - MAX_TRADES_LISTED} más")

    held = [
        (p.ticker, inp.quotes[p.ticker].indicators.ret_1d)
        for p in snap.positions
        if p.ticker in inp.quotes and inp.quotes[p.ticker].indicators.ret_1d is not None
    ]
    if held:
        held.sort(key=lambda x: x[1] or 0.0)
        worst, best = held[0], held[-1]
        lines.append(f"🏆 Mejor {best[0]} {fmt_pct(best[1])} · 🔻 Peor {worst[0]} {fmt_pct(worst[1])}")

    # Watchlist: biggest movers today and strongest trends across the whole universe
    movers = [q for q in inp.quotes.values() if q.indicators.ret_1d is not None]
    movers.sort(key=lambda q: q.indicators.ret_1d or 0.0)
    if len(movers) >= 6:
        up = ", ".join(f"{q.ticker} {fmt_pct(q.indicators.ret_1d)}" for q in movers[-3:][::-1])
        down = ", ".join(f"{q.ticker} {fmt_pct(q.indicators.ret_1d)}" for q in movers[:3])
        lines.append(f"🔭 Suben: {up}")
        lines.append(f"   Bajan: {down}")
    trending = [q for q in inp.quotes.values() if q.trend == "alcista" and q.indicators.ret_3m is not None]
    trending.sort(key=lambda q: -(q.indicators.ret_3m or 0.0))
    if trending:
        lines.append("🚀 Tendencia alcista (3m): " + ", ".join(
            f"{q.ticker} {fmt_pct(q.indicators.ret_3m)}" for q in trending[:4]))

    lines.extend(inp.notes)
    lines.append("— Simulado (paper). No es asesoría financiera.")

    arrow = "📈" if (day_ret or 0) >= 0 else "📉"
    title = f"{arrow} Cartera {fmt_pct(day_ret)} hoy · {fmt_money(total_cop, 'COP')}"
    tags = ["chart_with_upwards_trend" if (day_ret or 0) >= 0 else "chart_with_downwards_trend"]
    return title, "\n".join(lines), tags
