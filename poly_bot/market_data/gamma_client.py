"""
Async HTTP client for the Polymarket Gamma API.
Handles market metadata, discovery, and filtering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from poly_bot.market_data.models import Market, Token
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"


class GammaClient:
    """Async client for gamma-api.polymarket.com."""

    def __init__(self, host: str = GAMMA_HOST, timeout: float = 10.0) -> None:
        self._host = host.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._host,
            timeout=timeout,
            headers={"User-Agent": "poly-bot/0.1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_markets(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
        **filters: Any,
    ) -> list[Market]:
        """Fetch markets from Gamma API with optional filters."""
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            **filters,
        }
        try:
            resp = await self._client.get("/markets", params=params)
            resp.raise_for_status()
            data: list[dict[str, Any]] = resp.json()
            markets = [self._parse_market(m) for m in data if isinstance(m, dict)]
            log.debug("gamma.markets_fetched", count=len(markets))
            return markets
        except httpx.HTTPError as exc:
            log.error("gamma.fetch_failed", error=str(exc))
            return []

    async def get_market(self, condition_id: str) -> Market | None:
        """Fetch a single market by condition ID."""
        try:
            resp = await self._client.get(f"/markets/{condition_id}")
            resp.raise_for_status()
            return self._parse_market(resp.json())
        except httpx.HTTPError as exc:
            log.error("gamma.market_fetch_failed", condition_id=condition_id, error=str(exc))
            return None

    def _parse_market(self, data: dict[str, Any]) -> Market:
        # Parse outcome prices list first so we can enrich tokens
        outcome_prices: list[float] = []
        raw_prices = data.get("outcomePrices", data.get("outcome_prices", "[]"))
        if isinstance(raw_prices, str):
            try:
                import json
                outcome_prices = [float(p) for p in json.loads(raw_prices)]
            except Exception:
                pass
        elif isinstance(raw_prices, list):
            outcome_prices = [float(p) for p in raw_prices]

        # Build a outcome-name → price map: YES → index 0, NO → index 1
        _outcome_price_map = {}
        if len(outcome_prices) >= 2:
            _outcome_price_map = {"yes": outcome_prices[0], "no": outcome_prices[1]}
        elif len(outcome_prices) == 1:
            _outcome_price_map = {"yes": outcome_prices[0]}

        # Market-level price fallbacks that don't require CLOB API
        best_bid = float(data.get("bestBid", data.get("best_bid", 0.0)) or 0.0)
        best_ask = float(data.get("bestAsk", data.get("best_ask", 0.0)) or 0.0)
        last_trade = float(data.get("lastTradePrice", data.get("last_trade_price", 0.0)) or 0.0)
        mid_from_gamma = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
        yes_price_fallback = (
            _outcome_price_map.get("yes", 0.0)
            or mid_from_gamma
            or last_trade
        )

        tokens: list[Token] = []
        for t in data.get("tokens", []) or []:
            outcome = str(t.get("outcome", ""))
            raw_price = float(t.get("price", 0.0))
            # Fall back to outcomePrices → market mid → last trade price
            if raw_price == 0.0:
                if outcome.lower() == "yes":
                    raw_price = yes_price_fallback
                elif outcome.lower() == "no":
                    raw_price = _outcome_price_map.get("no", 0.0) or (1.0 - yes_price_fallback if yes_price_fallback else 0.0)
            tokens.append(
                Token(
                    token_id=str(t.get("token_id", t.get("tokenId", ""))),
                    outcome=outcome,
                    price=raw_price,
                )
            )

        def _parse_dt(val: Any) -> datetime | None:
            if not val:
                return None
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except Exception:
                return None

        return Market(
            condition_id=str(data.get("conditionId", data.get("condition_id", ""))),
            question_id=str(data.get("questionId", data.get("question_id", ""))),
            question=str(data.get("question", "")),
            description=str(data.get("description", "")),
            market_slug=str(data.get("marketSlug", data.get("market_slug", ""))),
            category=str(data.get("category", data.get("groupItemTitle", ""))),
            active=bool(data.get("active", True)),
            closed=bool(data.get("closed", False)),
            accepting_orders=bool(data.get("acceptingOrders", data.get("accepting_orders", True))),
            volume=float(data.get("volume", 0.0) or 0.0),
            liquidity=float(data.get("liquidity", 0.0) or 0.0),
            best_bid=float(data.get("bestBid", data.get("best_bid", 0.0)) or 0.0),
            best_ask=float(data.get("bestAsk", data.get("best_ask", 0.0)) or 0.0),
            last_trade_price=float(
                data.get("lastTradePrice", data.get("last_trade_price", 0.0)) or 0.0
            ),
            outcome_prices=outcome_prices,
            tokens=tokens,
            end_date_iso=data.get("endDateIso", data.get("end_date_iso")),
            created_at=_parse_dt(data.get("createdAt", data.get("created_at"))),
            updated_at=_parse_dt(data.get("updatedAt", data.get("updated_at"))),
            one_hour_price_change=float(
                data.get("oneDayPriceChange", data.get("one_hour_price_change", 0.0)) or 0.0
            ),
            one_day_price_change=float(
                data.get("oneDayPriceChange", data.get("one_day_price_change", 0.0)) or 0.0
            ),
        )
