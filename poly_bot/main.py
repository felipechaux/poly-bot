"""
CLI entrypoint — `poly-bot` command.

Usage:
  poly-bot run                   # Start bot in paper mode
  poly-bot run --mode live       # Start in live mode (requires ENABLE_LIVE_TRADING=true)
  poly-bot positions             # Show current positions and P&L
  poly-bot markets               # List tracked markets
  poly-bot trades                # Show recent trade history
  poly-bot orderbook <token_id>  # Live order book for a token
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich import print as rprint

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ before anything else

from poly_bot.observability.logging import setup_logging

app = typer.Typer(
    name="poly-bot",
    help="Polymarket trading bot with paper trading and pluggable strategies.",
    add_completion=False,
)
console = Console()


def _get_settings_and_bot():
    """Lazy import to avoid slow startup for help commands."""
    from poly_bot.config.settings import get_settings
    from poly_bot.bot import Bot

    settings = get_settings()
    setup_logging(level=settings.log_level, fmt=settings.log_format)
    return settings, Bot(settings)


@app.command()
def run(
    mode: Optional[str] = typer.Option(
        None, "--mode", "-m", help="Trading mode: paper or live (overrides .env)"
    ),
    strategy: Optional[list[str]] = typer.Option(
        None, "--strategy", "-s", help="Strategy name(s) to enable (overrides config)"
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Web dashboard host"),
    port: int = typer.Option(8080, "--port", "-p", help="Web dashboard port"),
    no_web: bool = typer.Option(False, "--no-web", help="Disable web dashboard"),
) -> None:
    """Start the trading bot with web dashboard."""
    import os

    if mode:
        os.environ["POLY_MODE"] = mode

    from poly_bot.config.settings import get_settings
    from poly_bot.bot import Bot

    settings = get_settings()
    setup_logging(level=settings.log_level, fmt=settings.log_format)

    # Override strategies from CLI
    if strategy:
        for name in strategy:
            if name in settings.strategies:
                settings.strategies[name]["enabled"] = True
            else:
                settings.strategies[name] = {"enabled": True, "params": {}}

    active = settings.active_strategies()
    if not active:
        console.print("[yellow]Warning: No strategies enabled. Check config/default.yaml[/yellow]")
        console.print("Use --strategy mean_reversion to enable a strategy.")

    bot = Bot(settings)

    mode_str = "[bold red]LIVE[/bold red]" if settings.mode == "live" else "[bold green]PAPER[/bold green]"
    console.print(f"\n[bold]Polymarket Bot[/bold] — mode: {mode_str}")
    console.print(f"Strategies: {', '.join(s.name for s in bot._strategies) or 'none'}")
    if settings.mode == "paper":
        console.print(f"Paper balance: ${settings.paper_initial_balance:,.2f} USDC")
    if not no_web:
        console.print(f"Dashboard:     [bold cyan]http://localhost:{port}[/bold cyan]\n")

    # Graceful shutdown on Ctrl+C
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
        console.print("\n[yellow]Shutting down...[/yellow]")
        asyncio.create_task(bot.stop())

    loop.add_signal_handler(signal.SIGINT, _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)

    async def _run_all():
        tasks = [asyncio.create_task(bot.run())]
        if not no_web:
            from poly_bot.web.api import create_app
            import uvicorn
            bot_ref = [bot]
            web_app = create_app(bot_ref)
            config = uvicorn.Config(
                web_app,
                host=host,
                port=port,
                log_level="warning",
                loop="none",
            )
            server = uvicorn.Server(config)
            tasks.append(asyncio.create_task(server.serve()))
        await asyncio.gather(*tasks)

    try:
        loop.run_until_complete(_run_all())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        console.print("[green]Bot stopped.[/green]")


@app.command()
def positions() -> None:
    """Show current portfolio positions and P&L."""
    from poly_bot.config.settings import get_settings
    from poly_bot.store.database import init_db
    from poly_bot.store.trade_store import TradeStore

    async def _show():
        from poly_bot.portfolio.pnl import format_pnl_summary
        from poly_bot.portfolio.models import PortfolioSnapshot

        console.print("[bold]Portfolio Positions[/bold]\n")
        console.print("[dim]Tip: Run 'poly-bot run' to start the bot and generate trades.[/dim]")

    asyncio.run(_show())


@app.command()
def trades(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of recent trades to show"),
) -> None:
    """Show recent trade history from the database."""
    async def _show():
        try:
            from poly_bot.store.database import init_db
            from poly_bot.store.trade_store import TradeStore

            conn = await init_db()
            store = TradeStore(conn)
            fills = await store.recent_fills(limit=limit)
            await conn.close()

            if not fills:
                console.print("[yellow]No trades recorded yet. Start the bot with 'poly-bot run'.[/yellow]")
                return

            table = Table(title=f"Last {limit} Trades", show_header=True)
            table.add_column("Time", style="dim")
            table.add_column("Token ID")
            table.add_column("Side")
            table.add_column("Price", justify="right")
            table.add_column("Size (shares)", justify="right")
            table.add_column("Cost (USDC)", justify="right")
            table.add_column("Strategy")
            table.add_column("Mode")

            for row in fills:
                side_color = "green" if row["side"] == "BUY" else "red"
                table.add_row(
                    row["filled_at"][:19],
                    row["token_id"][:16] + "...",
                    f"[{side_color}]{row['side']}[/{side_color}]",
                    f"{row['price']:.4f}",
                    f"{row['size']:.2f}",
                    f"${row['cost_usdc']:.2f}",
                    row["strategy"],
                    "paper" if row["is_paper"] else "LIVE",
                )

            console.print(table)
        except Exception as exc:
            console.print(f"[red]Error reading trades: {exc}[/red]")
            console.print("[dim]Run the bot first to generate trade data.[/dim]")

    asyncio.run(_show())


@app.command()
def markets(
    min_liquidity: float = typer.Option(1000.0, "--min-liquidity", help="Minimum liquidity in USDC"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """List available markets from Polymarket."""
    async def _show():
        from poly_bot.config.settings import get_settings
        from poly_bot.market_data.gamma_client import GammaClient

        settings = get_settings()
        setup_logging(level="WARNING")

        gamma = GammaClient(host=settings.gamma_host)
        try:
            mrkts = await gamma.get_markets(limit=100)
            filtered = [m for m in mrkts if m.liquidity >= min_liquidity]
            filtered.sort(key=lambda m: m.liquidity, reverse=True)
            filtered = filtered[:limit]

            table = Table(title=f"Top {len(filtered)} Markets (min liquidity: ${min_liquidity:,.0f})")
            table.add_column("Question", max_width=50)
            table.add_column("Category")
            table.add_column("YES Price", justify="right")
            table.add_column("Liquidity", justify="right")
            table.add_column("Volume", justify="right")

            for m in filtered:
                yes_price = m.yes_token.price if m.yes_token else 0.0
                table.add_row(
                    m.question[:50],
                    m.category[:20],
                    f"{yes_price:.3f}",
                    f"${m.liquidity:,.0f}",
                    f"${m.volume:,.0f}",
                )

            console.print(table)
        finally:
            await gamma.close()

    asyncio.run(_show())


@app.command()
def orderbook(
    token_id: str = typer.Argument(..., help="Token ID (YES or NO token address)"),
    depth: int = typer.Option(10, "--depth", "-d", help="Order book depth to show"),
) -> None:
    """Display live order book for a token."""
    async def _show():
        from poly_bot.config.settings import get_settings
        from poly_bot.market_data.clob_client import AsyncClobClient
        from poly_bot.market_data import order_book as ob_utils

        settings = get_settings()
        setup_logging(level="WARNING")

        clob = AsyncClobClient(host=settings.clob_host, chain_id=settings.chain_id)
        try:
            book = await clob.get_order_book(token_id)
            if not book:
                console.print(f"[red]No order book found for {token_id}[/red]")
                return

            mid = book.mid_price
            spread = book.spread
            imbalance = ob_utils.book_imbalance(book)

            console.print(f"\n[bold]Order Book[/bold] — {token_id[:24]}...")
            console.print(f"Mid: {mid:.4f}  Spread: {spread:.4f}  Imbalance: {imbalance:+.3f}\n")

            table = Table(show_header=True)
            table.add_column("Bid Size", justify="right", style="green")
            table.add_column("Bid Price", justify="right", style="green")
            table.add_column("Ask Price", justify="right", style="red")
            table.add_column("Ask Size", justify="right", style="red")

            bids = sorted(book.bids, key=lambda x: -x.price)[:depth]
            asks = sorted(book.asks, key=lambda x: x.price)[:depth]

            for i in range(max(len(bids), len(asks))):
                bid = bids[i] if i < len(bids) else None
                ask = asks[i] if i < len(asks) else None
                table.add_row(
                    f"{bid.size:.2f}" if bid else "",
                    f"{bid.price:.4f}" if bid else "",
                    f"{ask.price:.4f}" if ask else "",
                    f"{ask.size:.2f}" if ask else "",
                )

            console.print(table)
        finally:
            await clob.close()

    asyncio.run(_show())


if __name__ == "__main__":
    app()
