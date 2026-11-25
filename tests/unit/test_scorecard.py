"""Scorecard: basket baseline, semáforo thresholds, guard reasons."""

from __future__ import annotations

from datetime import date

import pandas as pd

from poly_bot.analytics.scorecard import basket_return, guard_reason, semaforo


def test_basket_return_uses_close_on_or_before_start():
    idx = pd.to_datetime(["2026-11-20", "2026-11-23", "2026-11-30"])
    closes = {"A": pd.Series([10.0, 11.0, 12.1], index=idx), "B": pd.Series([5.0, 5.0, 4.5], index=idx)}
    r = basket_return(closes, ["A", "B", "MISSING"], date(2026, 11, 24))  # → 11-23 closes
    assert abs(r - ((12.1 / 11 - 1) + (4.5 / 5 - 1)) / 2) < 1e-12


def test_semaforo_colors():
    assert semaforo([]).color == "gris"
    assert semaforo([0.05] * 5).color == "gris"  # too few ideas, however good
    good = [0.02 + 0.001 * (i % 5) for i in range(25)]
    assert semaforo(good).color == "verde"
    assert semaforo([-x for x in good]).color == "rojo"
    noisy = [0.05 if i % 2 else -0.049 for i in range(30)]
    assert semaforo(noisy).color == "amarillo"


def test_guard_reason():
    green = semaforo([])
    assert guard_reason({"pnl_pct": -0.05}, green, 0.10) is None
    assert "pérdida" in guard_reason({"pnl_pct": -0.12}, green, 0.10)
    red = semaforo([-0.03 - 0.001 * (i % 3) for i in range(25)])
    assert "semáforo rojo" in guard_reason({"pnl_pct": 0.0}, red, 0.10)
