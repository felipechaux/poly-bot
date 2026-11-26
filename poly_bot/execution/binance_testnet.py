"""
Binance Spot Testnet — mirrors the bot's crypto orders on https://testnet.binance.vision.

Testnet money is fake, so this is still paper trading: it exercises a real exchange API
(signing, lot-size / min-notional filters, market-order fills) without risk. The local ledger
stays the source of truth; a testnet fill only replaces the simulated price/quantity/fee.

Keys: create them at testnet.binance.vision (GitHub login) → BINANCE_TESTNET_API_KEY /
BINANCE_TESTNET_API_SECRET in .env. Testnet balances are reset periodically by Binance.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://testnet.binance.vision"
QUOTE_ASSET = "USDT"


class BinanceError(Exception):
    pass


@dataclass(frozen=True)
class SymbolFilters:
    step_size: float
    min_qty: float
    min_notional: float


@dataclass(frozen=True)
class BinanceFill:
    order_id: int
    symbol: str
    quantity: float  # net base quantity received (BUY) / sold (SELL)
    avg_price: float
    quote_qty: float  # USDT paid (BUY) / received (SELL), before quote-asset commission
    fee: float  # in USDT


def to_symbol(ticker: str) -> str:
    """BTC-USD → BTCUSDT (testnet quotes crypto in USDT ≈ USD)."""
    base = ticker.upper().split("-")[0]
    return f"{base}{QUOTE_ASSET}"


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    decimals = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step + 1e-9) * step, decimals)


class BinanceTestnet:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL,
                 max_deviation_pct: float = 0.02, timeout_seconds: float = 15.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.max_deviation_pct = max_deviation_pct
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._filters: dict[str, SymbolFilters] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self._secret)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                       signed: bool = False) -> Any:
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10_000
            query = urlencode(params)
            params["signature"] = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        try:
            resp = await self._client.request(method, f"{self.base_url}{path}", params=params,
                                              headers=headers)
        except httpx.HTTPError as exc:
            raise BinanceError(f"sin conexión con Binance testnet: {exc}") from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = f"{body.get('code')}: {body.get('msg')}"
            except ValueError:
                msg = resp.text[:200]
            raise BinanceError(f"HTTP {resp.status_code} {msg}")
        return resp.json()

    # ------------------------------------------------------------------
    # Public data
    # ------------------------------------------------------------------

    async def price(self, symbol: str) -> float:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    async def filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self._filters:
            data = await self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
            symbols = data.get("symbols") or []
            if not symbols:
                raise BinanceError(f"{symbol} no existe en el testnet")
            f = {x["filterType"]: x for x in symbols[0].get("filters", [])}
            lot = f.get("LOT_SIZE", {})
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[symbol] = SymbolFilters(
                step_size=float(lot.get("stepSize", 0) or 0),
                min_qty=float(lot.get("minQty", 0) or 0),
                min_notional=float(notional.get("minNotional", 0) or 0),
            )
        return self._filters[symbol]

    # ------------------------------------------------------------------
    # Account / orders
    # ------------------------------------------------------------------

    async def balances(self) -> dict[str, float]:
        data = await self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) + float(b["locked"])
                for b in data.get("balances", []) if float(b["free"]) + float(b["locked"]) > 0}

    async def check_price(self, symbol: str, reference: float) -> float:
        """Testnet price, or BinanceError if it strays too far from the real market price.

        The testnet order book is thin and can drift from the real market; filling there would
        book fake P&L when the position is marked to the real price."""
        p = await self.price(symbol)
        if reference > 0 and abs(p / reference - 1) > self.max_deviation_pct:
            raise BinanceError(f"precio testnet {p:,.2f} se aleja {abs(p / reference - 1):.1%} "
                               f"del mercado ({reference:,.2f})")
        return p

    async def market_order(self, ticker: str, side: str, quantity: float,
                           reference_price: float) -> BinanceFill:
        symbol = to_symbol(ticker)
        f = await self.filters(symbol)
        px = await self.check_price(symbol, reference_price)
        qty = round_step(quantity, f.step_size)
        if qty < f.min_qty or qty <= 0:
            raise BinanceError(f"cantidad {quantity:g} < mínimo {f.min_qty:g} de {symbol}")
        if qty * px < f.min_notional:
            raise BinanceError(f"monto {qty * px:,.2f} < mínimo {f.min_notional:,.2f} USDT")
        data = await self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."), "newOrderRespType": "FULL",
        }, signed=True)
        fill = parse_fill(data, symbol)
        log.info("binance.filled", symbol=symbol, side=side, qty=fill.quantity,
                 price=round(fill.avg_price, 4), fee=round(fill.fee, 4), order_id=fill.order_id)
        return fill


def parse_fill(data: dict[str, Any], symbol: str) -> BinanceFill:
    executed = float(data.get("executedQty", 0) or 0)
    quote = float(data.get("cummulativeQuoteQty", 0) or 0)
    if executed <= 0 or quote <= 0:
        raise BinanceError(f"orden sin ejecutar (status {data.get('status')})")
    avg = quote / executed
    base_asset = symbol[: -len(QUOTE_ASSET)]
    fee_quote, fee_base, fee_other = 0.0, 0.0, 0.0
    for f in data.get("fills", []) or []:
        c, asset = float(f.get("commission", 0) or 0), f.get("commissionAsset")
        if asset == QUOTE_ASSET:
            fee_quote += c
        elif asset == base_asset:
            fee_base += c
        elif c > 0:
            fee_other += c * float(f.get("price", avg))  # e.g. BNB: rough USDT equivalent
    side = data.get("side", "BUY")
    # Commission taken in the base asset reduces what we actually hold after a BUY
    qty = executed - fee_base if side == "BUY" else executed
    return BinanceFill(order_id=int(data.get("orderId", 0)), symbol=symbol, quantity=qty,
                       avg_price=avg, quote_qty=quote,
                       fee=fee_quote + fee_base * avg + fee_other)
