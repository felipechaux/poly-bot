"""Formatting helpers for money in COP / USD."""

from __future__ import annotations


def fmt_money(amount: float, ccy: str) -> str:
    if ccy == "COP":
        # Colombian style: $ 12.345.678
        return "$ " + f"{amount:,.0f}".replace(",", ".") + " COP"
    return f"US$ {amount:,.2f}"


def fmt_pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"
