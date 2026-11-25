"""
CLI entrypoint — `poly-bot` command.

  poly-bot run                 # scheduler (daily cycle) + dashboard on :8080
  poly-bot cycle --dry-run     # simulate today's cycle without executing anything
  poly-bot cycle               # run today's cycle now (executes + notifies)
  poly-bot prices [--region CO]# universe quotes and trend indicators
  poly-bot positions           # current paper portfolio
  poly-bot trades              # recent transactions
  poly-bot notify-test         # send a test notification to ntfy
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

if TYPE_CHECKING:
    from poly_bot.bot import Bot

from poly_bot.observability.logging import setup_logging  # noqa: E402

app = typer.Typer(name="poly-bot", help="Paper-trading investment bot (Colombia + global).",
                  add_completion=False)
console = Console()


def _bot() -> "Bot":
    from poly_bot.bot import Bot
    from poly_bot.config.settings import get_settings

    settings = get_settings()
    setup_logging(level=settings.log_level, fmt=settings.log_format)
    return Bot(settings)


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    color = "green" if x >= 0 else "red"
    return f"[{color}]{x * 100:+.1f}%[/{color}]"


@app.command()
def run(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(int(os.environ.get("PORT", 8080)), "--port", "-p"),
    no_web: bool = typer.Option(False, "--no-web", help="Disable web dashboard"),
) -> None:
    """Start the daily scheduler and the web dashboard."""
    bot = _bot()
    console.print(f"[bold]Invest Bot[/bold] — modo [green]{bot.mode}[/green] · "
                  f"estrategias: {', '.join(s.name for s in bot.strategies) or 'ninguna'} · "
                  f"universo: {len(bot.universe)} activos")
    if not no_web:
        console.print(f"Dashboard: [cyan]http://localhost:{port}[/cyan]")
    if not bot.notifier.enabled:
        console.print("[yellow]NTFY_TOPIC vacío → sin notificaciones al iPhone[/yellow]")

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        server = None
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: (asyncio.ensure_future(bot.stop()),
                                                  server and setattr(server, "should_exit", True)))
        tasks = [asyncio.create_task(bot.run())]
        if not no_web:
            import uvicorn

            from poly_bot.web.api import create_app

            await bot.start()  # API needs the DB before the first cycle finishes
            server = uvicorn.Server(uvicorn.Config(create_app(bot), host=host, port=port,
                                                   log_level="warning", loop="none"))
            tasks.append(asyncio.create_task(server.serve()))
        await asyncio.gather(*tasks)

    asyncio.run(_main())
    console.print("[green]Bot detenido.[/green]")


@app.command()
def cycle(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate; execute/persist nothing"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Don't send ntfy"),
) -> None:
    """Run today's cycle once and print the summary."""
    bot = _bot()

    async def _go() -> None:
        try:
            r = await bot.run_cycle(dry_run=dry_run, notify=not no_notify)
        finally:
            await bot._shutdown()
        console.print(f"\n[bold]{r.title}[/bold]\n{r.message}\n")
        if r.executed:
            t = Table("Tipo", "Ticker", "Cant.", "Precio", "Monto", "Comisión", "Razón")
            for tx in r.executed:
                t.add_row(tx.kind, tx.ticker or f"{tx.from_currency}→{tx.currency}",
                          f"{tx.quantity:g}" if tx.quantity else "", f"{tx.price:,.2f}",
                          f"{tx.gross:,.2f} {tx.currency}", f"{tx.fee:,.2f}", tx.rationale)
            console.print(t)
        for s, reason in r.rejected:
            console.print(f"[red]Rechazada[/red] {s.side} {s.ticker}: {reason}")
        if not dry_run:
            console.print(f"Notificado: {r.notified}")

    asyncio.run(_go())


@app.command()
def prices(
    region: Optional[str] = typer.Option(None, "--region", "-r", help="CO, US, LATAM, GLOBAL…"),
    sort: str = typer.Option("ret_3m", "--sort", help="ret_1d|ret_1m|ret_3m|ret_1y"),
) -> None:
    """Show quotes and trend indicators for the universe."""
    bot = _bot()

    async def _go() -> None:
        md = await bot._provider.fetch(list(bot.universe))
        t = Table("Ticker", "Nombre", "Región", "Precio", "1d", "1m", "3m", "1a", "Tendencia", "Fecha")
        assets = [a for a in bot.universe if not region or a.region == region.upper()]
        assets.sort(key=lambda a: -(getattr(md.quotes[a.ticker].indicators, sort) or -9)
                    if a.ticker in md.quotes else 9)
        for a in assets:
            q = md.quotes.get(a.ticker)
            if q is None:
                t.add_row(a.ticker, a.name, a.region, "[red]sin datos[/red]", *[""] * 6)
                continue
            i = q.indicators
            t.add_row(a.ticker, a.name, a.region, f"{q.price:,.2f} {q.currency}",
                      _pct(i.ret_1d), _pct(i.ret_1m), _pct(i.ret_3m), _pct(i.ret_1y),
                      q.trend, str(q.as_of))
        console.print(t)
        console.print(f"USD/COP: {md.usdcop:,.2f} ({md.usdcop_as_of})")

    asyncio.run(_go())


@app.command()
def positions() -> None:
    """Show the current paper portfolio (prices from the last cycle)."""
    bot = _bot()

    async def _go() -> None:
        await bot.start()
        snap = bot.snapshot()
        await bot._shutdown()
        if snap is None:
            console.print("Sin precios todavía — corre `poly-bot cycle` primero.")
            return
        from poly_bot.portfolio.pnl import fmt_money

        t = Table("Ticker", "Cant.", "Costo prom.", "Precio", "Valor", "Peso", "P&L")
        for p in sorted(snap.positions, key=lambda p: -snap.weight(p.ticker)):
            t.add_row(p.ticker, f"{p.quantity:g}", f"{p.avg_cost:,.2f}", f"{p.last_price or 0:,.2f}",
                      fmt_money(p.market_value, p.currency), f"{snap.weight(p.ticker):.1%}",
                      _pct(p.unrealized_pct))
        console.print(t)
        console.print(f"Caja: {fmt_money(snap.cash.get('COP', 0), 'COP')} + "
                      f"{fmt_money(snap.cash.get('USD', 0), 'USD')} · TRM {snap.usdcop:,.2f}")
        console.print(f"Total: {fmt_money(snap.total_value('COP'), 'COP')} · "
                      f"{fmt_money(snap.total_value('USD'), 'USD')}")

    asyncio.run(_go())


@app.command()
def trades(limit: int = typer.Option(30, "--limit", "-n")) -> None:
    """Show recent transactions."""
    bot = _bot()

    async def _go() -> None:
        await bot.start()
        assert bot.store is not None
        rows = await bot.store.recent_transactions(limit)
        await bot._shutdown()
        t = Table("Fecha", "Tipo", "Ticker", "Cant.", "Precio", "Monto", "Comisión", "Estrategia")
        for r in rows:
            t.add_row(r["occurred_at"][:16], r["kind"], r["ticker"] or (r["from_currency"] or ""),
                      f"{r['quantity']:g}" if r["quantity"] else "", f"{r['price']:,.2f}",
                      f"{r['gross']:,.2f} {r['currency']}", f"{r['fee']:,.2f}", r["strategy"])
        console.print(t)

    asyncio.run(_go())


@app.command("notify-test")
def notify_test() -> None:
    """Send a test notification to ntfy."""
    bot = _bot()
    if not bot.notifier.enabled:
        console.print("[red]NTFY_TOPIC no está configurado en .env[/red]")
        raise typer.Exit(1)
    ok = asyncio.run(bot.notifier.send("Si ves esto, las notificaciones funcionan ✅",
                                       title="🧪 Invest bot: prueba", tags=["test_tube"]))
    console.print("Enviado ✅" if ok else "[red]Falló el envío[/red]")


if __name__ == "__main__":
    app()
