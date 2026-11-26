"""
Market sessions — when an order decided at time T can be filled.

BVC and US orders fill at the opening price of the first session that starts strictly after
the decision, so a decision made after the open (e.g. a late-night manual run) never fills at
an opening price that was already known. Crypto trades 24/7 and fills immediately.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

# market → (exchange timezone, regular-session open)
SESSION_OPEN: dict[str, tuple[str, time]] = {
    "US": ("America/New_York", time(9, 30)),
    "BVC": ("America/Bogota", time(9, 30)),
}


def fills_at_open(market: str) -> bool:
    return market in SESSION_OPEN


def session_open(market: str, day: date) -> datetime:
    tz, t = SESSION_OPEN[market]
    return datetime.combine(day, t, tzinfo=ZoneInfo(tz))


def next_open_fill(market: str, decided_at: datetime, opens: pd.Series | None) -> tuple[date, float] | None:
    """(session day, opening price) of the first session opening after `decided_at`, if known."""
    if opens is None or opens.empty or not fills_at_open(market):
        return None
    for idx, price in opens.items():
        day = pd.Timestamp(idx).date()
        if session_open(market, day) > decided_at and price and price > 0:
            return day, float(price)
    return None


def next_session_open(market: str, after: datetime) -> datetime | None:
    """First regular-session open strictly after `after` (weekdays; holidays not modelled)."""
    if not fills_at_open(market):
        return None
    tz, _ = SESSION_OPEN[market]
    day = after.astimezone(ZoneInfo(tz)).date()
    for _ in range(8):
        if day.weekday() < 5 and (opened := session_open(market, day)) > after:
            return opened
        day += timedelta(days=1)
    return None
