"""
Backtester for the core allocation — same strategy, costs, risk checks and fill rule as live.

For each trading day D (a day with a US bar):
  1. The strategy decides with what it would know at 08:00 on D: the last close before D for
     every asset and the last USD/COP close before D.
  2. Orders fill at D's opening price (with slippage and commissions). Assets without a bar on
     D (e.g. a BVC holiday) skip that order; the strategy retries on its next rebalance.
  3. Dividends paid on D are credited (net of withholding) and the portfolio is marked at D's
     close for the equity curve.

Benchmarks are buy-and-hold of the initial capital, converted at the start, without costs, with
dividends reinvested (adjusted close).
LLM analysts cannot be backtested honestly (the model already "knows" what happened), so they
are measured live by the scorecard instead.

Caveats: yfinance BVC "opens" often repeat the close (then the fill is D's close — still after
the decision); USD/COP is Yahoo's rate, not the official TRM; no taxes; Sharpe uses rf = 0.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from poly_bot.execution.models import Transaction
from poly_bot.market_data.models import Quote
from poly_bot.market_data.provider import MarketData, clean_history
from poly_bot.observability.logging import get_logger
from poly_bot.portfolio.position_tracker import PositionTracker
from poly_bot.strategies.base import DailyContext
from poly_bot.strategies.core_allocation import CoreAllocationStrategy

if TYPE_CHECKING:
    from poly_bot.config.settings import Settings

log = get_logger(__name__)

RESULT_PATH = Path("data/backtest.json")
TRADING_DAYS = 252


@dataclass
class History:
    """Cleaned daily OHLC + dividends per ticker, and USD/COP closes."""

    frames: dict[str, pd.DataFrame]
    usdcop: pd.Series


@dataclass
class BacktestResult:
    params: dict[str, Any]
    metrics: dict[str, dict[str, Any]]
    curve: list[dict[str, Any]]
    yearly: list[dict[str, Any]]
    trades: int = 0
    fees_base: float = 0.0
    contribution: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"params": self.params, "metrics": self.metrics, "curve": self.curve,
                "yearly": self.yearly, "trades": self.trades, "fees_base": self.fees_base,
                "contribution": self.contribution,
                "notes": self.notes}


def _download(tickers: list[str], years: int) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    raw = yf.download(tickers, period=f"{years + 1}y", interval="1d", auto_adjust=False,
                      actions=True, group_by="ticker", progress=False, threads=True)
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = (raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw).dropna(how="all")
        except KeyError:
            continue
        if not df.empty and df["Close"].notna().any():
            out[t] = df
    return out


def load_history(settings: "Settings", tickers: list[str], years: int,
                 required: list[str] | None = None, retries: int = 2) -> History:
    """Download ~`years`+1 of daily bars from yfinance (synchronous).

    yfinance sometimes drops a ticker on a given request; missing ones are retried, and a
    still-missing `required` ticker (core target or FX) is an error rather than silent cash."""
    fx = settings.market_data.get("fx_ticker", "COP=X")
    wanted = tickers + [fx]
    raw = _download(wanted, years)
    for _ in range(retries):
        missing = [t for t in wanted if t not in raw]
        if not missing:
            break
        log.warning("backtest.retry_missing", tickers=missing)
        raw.update(_download(missing, years))
    must = set(required or []) | {fx}
    lost = sorted(t for t in must if t not in raw)
    if lost:
        raise RuntimeError(f"Sin historia de yfinance para {lost}; reintenta más tarde")
    markets = {a["ticker"]: a.get("market", "US") for a in settings.universe}
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        if t in raw:
            df = clean_history(raw[t], markets.get(t, "US"))
            if not df.empty:
                frames[t] = df
    return History(frames=frames, usdcop=raw[fx]["Close"].dropna().astype(float))


def _last_before(s: pd.Series, day: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    i = s.index.searchsorted(day, side="left") - 1
    if i < 0:
        return None
    return s.index[i], float(s.iloc[i])


def metrics_from_curve(values: pd.Series) -> dict[str, Any]:
    values = values.dropna()
    if len(values) < 2 or values.iloc[0] <= 0:
        return {}
    rets = values.pct_change().dropna()
    years = (values.index[-1] - values.index[0]).days / 365.25
    total = float(values.iloc[-1] / values.iloc[0] - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else None
    vol = float(rets.std() * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else None
    peak = values.cummax()
    dd = values / peak - 1
    trough = dd.idxmin()
    peak_day = values[:trough].idxmax()
    return {
        "start": str(values.index[0].date()), "end": str(values.index[-1].date()),
        "start_value": float(values.iloc[0]), "end_value": float(values.iloc[-1]),
        "total_return": total, "cagr": cagr, "vol": vol,
        "sharpe": (float(rets.mean() * TRADING_DAYS) / vol) if vol else None,
        "max_drawdown": float(dd.min()),
        "max_dd_peak": str(peak_day.date()), "max_dd_trough": str(trough.date()),
    }


async def run_backtest(settings: "Settings", years: int = 10, share: float = 1.0,
                       history: History | None = None) -> BacktestResult:
    from poly_bot.bot import Bot, CycleResult  # bot reuses the live fill/FX/risk logic

    core_cfg = dict(settings.strategies.get("core_allocation") or {})
    params = {**(core_cfg.get("params") or {}), "portfolio_share": share}
    strategy = CoreAllocationStrategy({**core_cfg, "params": params})
    bench = [a["ticker"] for a in settings.universe if a.get("benchmark")]
    tickers = sorted(set(strategy.targets) | set(bench))
    if history is None:
        history = await asyncio.to_thread(load_history, settings, tickers, years,
                                          sorted(strategy.targets))

    # Historical fills use the fixed-slippage model: today's live order book (crypto_fill:
    # orderbook) or the testnet would leak the present into past fills.
    execution = {**(settings.execution or {}), "crypto_fill": "model",
                 "binance_testnet": {"enabled": False}}
    bt_settings = settings.model_copy(update={"strategies": {}, "db_path": ":memory:",
                                              "execution": execution,
                                              "schedule": {**settings.schedule, "scans": []}})
    bot = Bot(bt_settings, provider=None)
    tracker = PositionTracker(base_currency=settings.base_currency)
    for ccy, amount in settings.initial_cash.items():
        if amount > 0:
            tracker.apply(Transaction(kind="DEPOSIT", currency=ccy, gross=amount), quiet=True)  # type: ignore[arg-type]

    frames, fx = history.frames, history.usdcop
    universe_markets = {a["ticker"]: a.get("market", "US") for a in settings.universe}
    us = [t for t in strategy.targets if universe_markets.get(t) == "US" and t in frames]
    if not us:
        raise RuntimeError("El backtest necesita al menos un activo de EE. UU. para el calendario")
    anchor = "VT" if "VT" in us else us[0]  # US trading days drive the calendar
    end = frames[anchor].index[-1]
    start = end - pd.DateOffset(years=years)
    days = [d for d in frames[anchor].index if d >= start]
    notes = []
    for t in strategy.targets:
        if t not in frames:
            notes.append(f"{t}: sin datos; su peso queda en caja")
        elif frames[t].index[0] > days[0]:
            notes.append(f"{t}: datos desde {frames[t].index[0].date()}; antes su peso queda en caja")
    universe = bot.universe
    withholding = bot.executor.dividend_withholding

    flows: dict[str, float] = {}  # per-ticker P&L in COP: −buys + sells + net dividends (+ final value)
    last_signal: date | None = None
    curve_rows: list[tuple[pd.Timestamp, float, float]] = []
    trades, fees_base = 0, 0.0
    bt_today: date = days[0].date()

    for day in days:
        bt_today = day.date()
        fx_prev = _last_before(fx, day)
        if fx_prev is None:
            continue
        usdcop = fx_prev[1]
        quotes: dict[str, Quote] = {}
        for t, df in frames.items():
            prev = _last_before(df["Close"], day)
            asset = universe.get(t)
            if prev is None or asset is None:
                continue
            quotes[t] = Quote(ticker=t, price=prev[1], currency=asset.currency, as_of=prev[0].date())
        tracker.update_prices(quotes, usdcop)

        ctx = DailyContext(today=bt_today, quotes=quotes, usdcop=usdcop, portfolio=tracker.snapshot(),
                           universe=universe, last_signal_date=last_signal)
        signals = await strategy.on_daily(ctx)
        if signals:
            last_signal = bt_today
            fills: dict[str, Quote] = {}
            for t, q in quotes.items():
                df = frames[t]
                if day in df.index:
                    o = df.at[day, "Open"]
                    price = float(o) if pd.notna(o) and o > 0 else float(df.at[day, "Close"])
                    fills[t] = q.model_copy(update={"price": price, "as_of": bt_today})
            tradable = [s for s in signals if s.ticker in fills]
            md = MarketData(quotes=fills, dividends={}, usdcop=usdcop, usdcop_as_of=fx_prev[0].date(),
                            closes={})
            result = CycleResult(day=bt_today, dry_run=True)
            await bot._fill_now(strategy.name, tradable, tracker, md, bt_today, result, dry_run=True)
            for tx in result.executed:
                if tx.kind in ("BUY", "SELL"):
                    trades += 1
                    snap_now = tracker.snapshot()
                    fees_base += snap_now.to(tx.fee, tx.currency)
                    cash = snap_now.to(tx.gross + tx.fee if tx.kind == "BUY" else tx.gross - tx.fee,
                                       tx.currency, "COP")
                    flows[tx.ticker] = flows.get(tx.ticker, 0.0) + (-cash if tx.kind == "BUY" else cash)

        # Dividends paid today on held positions
        for pos in list(tracker.snapshot().positions):
            df = frames.get(pos.ticker)
            if df is None or "Dividends" not in df or day not in df.index:
                continue
            amount = float(df.at[day, "Dividends"] or 0)
            asset = universe.get(pos.ticker)
            if amount > 0 and asset is not None:
                gross = pos.quantity * amount
                net = gross * (1 - withholding.get(asset.market, 0.0))
                flows[pos.ticker] = flows.get(pos.ticker, 0.0) + tracker.snapshot().to(net, asset.currency, "COP")
                tracker.apply(Transaction(kind="DIVIDEND", currency=asset.currency, ticker=pos.ticker,
                                          quantity=pos.quantity, price=amount, gross=gross,
                                          fee=gross * withholding.get(asset.market, 0.0),
                                          strategy=pos.strategy,
                                          occurred_at=datetime.combine(bt_today, datetime.min.time(),
                                                                       tzinfo=timezone.utc)),
                              quiet=True)

        # Mark to market at today's close
        close_quotes = {t: q.model_copy(update={"price": float(frames[t].at[day, "Close"])})
                        for t, q in quotes.items() if day in frames[t].index}
        fx_today = float(fx[fx.index <= day].iloc[-1])
        tracker.update_prices({**quotes, **close_quotes}, fx_today)
        snap = tracker.snapshot()
        curve_rows.append((day, snap.total_value("COP"), snap.total_value("USD")))

    final = tracker.snapshot()
    for p in final.positions:
        flows[p.ticker] = flows.get(p.ticker, 0.0) + final.value_of(p.ticker, "COP")
    start_total = final.to(sum(final.to(v, k, "COP") for k, v in settings.initial_cash.items()), "COP", "COP")
    contribution: list[dict[str, Any]] = [
        {"ticker": t, "pnl_cop": v, "share_of_start": v / start_total if start_total else None}
        for t, v in sorted(flows.items(), key=lambda kv: -kv[1])]

    curve = pd.DataFrame(curve_rows, columns=["day", "cop", "usd"]).set_index("day")
    start_cop, start_usd = curve["cop"].iloc[0], curve["usd"].iloc[0]
    series: dict[str, pd.Series] = {"core": curve["cop"]}
    for b in bench:
        df = frames.get(b)
        asset = universe.get(b)
        if df is None or asset is None:
            continue
        col = "Adj Close" if "Adj Close" in df else "Close"  # dividends reinvested
        close = df[col].astype(float).reindex(curve.index).ffill()
        fx_al = fx.reindex(curve.index).ffill()
        first = close.first_valid_index()
        if first is None:
            continue
        if asset.currency == "COP":
            series[b] = close / close[first] * start_cop
        else:
            series[b] = close / close[first] * start_usd * fx_al
        if first > curve.index[0]:
            notes.append(f"Benchmark {b}: datos desde {first.date()}")

    metrics = {k: metrics_from_curve(v) for k, v in series.items()}
    metrics["core_usd"] = metrics_from_curve(curve["usd"])
    weekly = pd.DataFrame(series).resample("W-FRI").last().dropna(how="all")
    curve_out = [{"day": str(i.date()), **{k: (None if pd.isna(v) else round(float(v)))
                                            for k, v in row.items()}}
                 for i, row in weekly.iterrows()]
    yearly = []
    for year, grp in pd.DataFrame(series).groupby(pd.DataFrame(series).index.year):
        row: dict[str, Any] = {"year": int(year)}
        for k in series:
            col = grp[k].dropna()
            prev = pd.DataFrame(series)[k][pd.DataFrame(series).index < grp.index[0]].dropna()
            base = prev.iloc[-1] if not prev.empty else (col.iloc[0] if not col.empty else None)
            row[k] = (float(col.iloc[-1] / base - 1) if base and not col.empty else None)
        yearly.append(row)

    notes.append("BVC: yfinance suele repetir el cierre como apertura; en esos días se llena al "
                 "cierre del día de ejecución (después de la decisión).")
    notes.append("Sin impuestos. TRM de Yahoo (COP=X). Sharpe con tasa libre de riesgo 0.")
    res = BacktestResult(
        params={"years": years, "share": share, "targets": strategy.targets,
                "drift_band": strategy.drift_band, "initial_cash": settings.initial_cash,
                "fill": "apertura del día siguiente a la decisión",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_day": str(bt_today)},
        metrics=metrics, curve=curve_out, yearly=yearly, trades=trades,
        fees_base=fees_base, notes=notes, contribution=contribution,
    )
    log.info("backtest.done", years=years, trades=trades,
             cagr=metrics.get("core", {}).get("cagr"), max_dd=metrics.get("core", {}).get("max_drawdown"))
    return res


def save_result(res: BacktestResult, path: Path = RESULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res.as_dict(), default=str, ensure_ascii=False))


def load_result(path: Path = RESULT_PATH) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except (OSError, json.JSONDecodeError):
        return None
