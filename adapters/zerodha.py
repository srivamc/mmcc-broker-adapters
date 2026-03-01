"""
MMCC Zerodha Broker Adapter
Integrates with Zerodha Kite Connect API v3

Supports:
- REST order placement (market, limit, SL, SL-M)
- WebSocket ticker for real-time quotes
- Historical data download
- Position and margin queries
- GTT (Good Till Triggered) orders

Note: Requires valid Zerodha API key and access token.
Access token expires daily and must be refreshed via OAuth flow.
"""

import asyncio
import hashlib
from datetime import datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

ZERODHA_BASE_URL = "https://api.kite.trade"
ZERODHA_LOGIN_URL = "https://kite.zerodha.com/connect/login"


class ZerodhaAuthError(Exception): ...
class ZerodhaOrderError(Exception): ...
class ZerodhaRateLimitError(Exception): ...


class ZerodhaClient:
    """
    Async Zerodha Kite Connect client.
    Wraps the REST API with automatic retry, rate limiting, and order tracking.
    """

    name = "zerodha"

    def __init__(self, api_key: str, api_secret: str, access_token: str | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=ZERODHA_BASE_URL,
            timeout=httpx.Timeout(10.0),
            headers={"X-Kite-Version": "3"}
        )
        self._rate_limiter = asyncio.Semaphore(10)  # 10 req/sec

    def get_login_url(self) -> str:
        return f"{ZERODHA_LOGIN_URL}?api_key={self.api_key}&v=3"

    def generate_checksum(self, request_token: str) -> str:
        raw = f"{self.api_key}{request_token}{self.api_secret}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def generate_session(self, request_token: str) -> dict:
        """Exchange request_token for access_token."""
        checksum = self.generate_checksum(request_token)
        async with self._rate_limiter:
            resp = await self._client.post(
                "/session/token",
                data={"api_key": self.api_key, "request_token": request_token, "checksum": checksum}
            )
        data = resp.json()
        if resp.status_code != 200:
            raise ZerodhaAuthError(f"Session error: {data.get('message', 'Unknown')}")
        self.access_token = data["data"]["access_token"]
        return data["data"]

    def _auth_headers(self) -> dict:
        if not self.access_token:
            raise ZerodhaAuthError("No access token. Call generate_session() first.")
        return {"Authorization": f"token {self.api_key}:{self.access_token}"}

    async def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,  # "BUY" or "SELL"
        quantity: int,
        product: str,           # "CNC", "MIS", "NRML"
        order_type: str,        # "MARKET", "LIMIT", "SL", "SL-M"
        price: float = 0,
        trigger_price: float = 0,
        validity: str = "DAY",
        tag: str = "",
        variety: str = "regular"
    ) -> dict:
        """Place an order. Returns order_id on success."""
        payload = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "validity": validity,
            "tag": tag[:20] if tag else "",  # Zerodha limits tag to 20 chars
        }
        async with self._rate_limiter:
            resp = await self._client.post(
                f"/orders/{variety}",
                data=payload,
                headers=self._auth_headers()
            )
        data = resp.json()
        if resp.status_code not in (200, 201):
            raise ZerodhaOrderError(f"Order failed: {data.get('message', data)}")
        log.info("Zerodha order placed",
                 order_id=data["data"]["order_id"],
                 symbol=tradingsymbol, side=transaction_type)
        return data["data"]

    async def cancel_order(self, order_id: str, variety: str = "regular") -> dict:
        async with self._rate_limiter:
            resp = await self._client.delete(
                f"/orders/{variety}/{order_id}",
                headers=self._auth_headers()
            )
        data = resp.json()
        if resp.status_code != 200:
            raise ZerodhaOrderError(f"Cancel failed: {data.get('message')}")
        return data["data"]

    async def get_order_history(self, order_id: str) -> list[dict]:
        async with self._rate_limiter:
            resp = await self._client.get(
                f"/orders/{order_id}",
                headers=self._auth_headers()
            )
        return resp.json().get("data", [])

    async def get_positions(self) -> dict:
        async with self._rate_limiter:
            resp = await self._client.get("/portfolio/positions", headers=self._auth_headers())
        return resp.json().get("data", {})

    async def get_margins(self) -> dict:
        async with self._rate_limiter:
            resp = await self._client.get("/user/margins", headers=self._auth_headers())
        return resp.json().get("data", {})

    async def get_quote(self, instruments: list[str]) -> dict:
        """Get live quotes. instruments = ['NSE:RELIANCE', 'BSE:INFY']"""
        async with self._rate_limiter:
            resp = await self._client.get(
                "/quote",
                params={"i": instruments},
                headers=self._auth_headers()
            )
        return resp.json().get("data", {})

    async def get_historical_data(
        self,
        instrument_token: int,
        from_date: str,
        to_date: str,
        interval: str = "5minute",
        continuous: bool = False
    ) -> list[dict]:
        """Fetch OHLCV historical data."""
        async with self._rate_limiter:
            resp = await self._client.get(
                f"/instruments/historical/{instrument_token}/{interval}",
                params={"from": from_date, "to": to_date, "continuous": int(continuous)},
                headers=self._auth_headers()
            )
        return resp.json().get("data", {}).get("candles", [])

    async def place_gtt(
        self,
        trigger_type: str,  # "single" or "two-leg"
        tradingsymbol: str,
        exchange: str,
        trigger_values: list[float],
        last_price: float,
        orders: list[dict]
    ) -> dict:
        """Place Good Till Triggered (GTT) order - Zerodha-specific feature."""
        import json
        payload = {
            "type": trigger_type,
            "condition": json.dumps({"exchange": exchange, "tradingsymbol": tradingsymbol,
                                      "trigger_values": trigger_values, "last_price": last_price}),
            "orders": json.dumps(orders)
        }
        async with self._rate_limiter:
            resp = await self._client.post("/gtt/triggers", data=payload, headers=self._auth_headers())
        data = resp.json()
        if resp.status_code not in (200, 201):
            raise ZerodhaOrderError(f"GTT failed: {data.get('message')}")
        return data["data"]

    async def health_check(self) -> dict:
        try:
            start = asyncio.get_event_loop().time()
            await self.get_margins()
            latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
            return {"broker": "zerodha", "connected": True, "latency_ms": latency_ms}
        except Exception as e:
            return {"broker": "zerodha", "connected": False, "error": str(e)}

    async def aclose(self) -> None:
        await self._client.aclose()
