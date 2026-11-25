"""Investable universe loaded from config."""

from __future__ import annotations

from typing import Any, Iterable

from poly_bot.market_data.models import Asset


class Universe:
    def __init__(self, assets: Iterable[Asset]) -> None:
        self._assets: dict[str, Asset] = {}
        for a in assets:
            if a.ticker in self._assets:
                raise ValueError(f"Duplicate ticker in universe: {a.ticker}")
            self._assets[a.ticker] = a

    @classmethod
    def from_config(cls, rows: list[dict[str, Any]]) -> "Universe":
        return cls(Asset(**row) for row in rows)

    def __contains__(self, ticker: object) -> bool:
        return ticker in self._assets

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._assets.values())

    def __len__(self) -> int:
        return len(self._assets)

    def get(self, ticker: str) -> Asset | None:
        return self._assets.get(ticker)

    def require(self, ticker: str) -> Asset:
        asset = self._assets.get(ticker)
        if asset is None:
            raise KeyError(f"Ticker not in universe: {ticker}")
        return asset

    @property
    def tickers(self) -> list[str]:
        return list(self._assets)

    @property
    def benchmarks(self) -> list[Asset]:
        return [a for a in self._assets.values() if a.benchmark]
