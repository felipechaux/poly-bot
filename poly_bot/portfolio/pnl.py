"""P&L reporting utilities."""

from __future__ import annotations

from poly_bot.portfolio.models import PortfolioSnapshot


def format_pnl_summary(snap: PortfolioSnapshot) -> dict[str, str]:
    """Format a portfolio snapshot into display-ready strings."""
    realized = snap.total_realized_pnl
    unrealized = snap.total_unrealized_pnl
    total = snap.total_pnl

    def sign(v: float) -> str:
        return "+" if v >= 0 else ""

    return {
        "cash": f"${snap.cash_usdc:,.2f}",
        "positions": f"${snap.total_position_value:,.2f}",
        "total_value": f"${snap.total_value:,.2f}",
        "realized_pnl": f"{sign(realized)}${realized:,.2f}",
        "unrealized_pnl": f"{sign(unrealized)}${unrealized:,.2f}",
        "total_pnl": f"{sign(total)}${total:,.2f}",
        "win_rate": f"{snap.win_rate:.1%}",
        "trades": str(snap.trade_count),
        "fees_paid": f"${snap.total_fees_paid:,.4f}",
    }
