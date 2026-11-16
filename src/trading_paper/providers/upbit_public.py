"""Strictly allowlisted, unauthenticated Upbit Quotation client."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import websockets

REST_ORIGIN = "https://api.upbit.com"
PUBLIC_WS_URL = "wss://api.upbit.com/websocket/v1"
REST_PATHS = {"/v1/market/all", "/v1/candles/minutes/1", "/v1/orderbook"}
WS_TYPES = {"ticker", "trade", "orderbook", "candle.1m"}


class PublicQuotationClient:
    """No credentials are accepted, loaded from the environment, or sent."""

    def __init__(self, timeout_seconds: float = 10.0, requests_per_second: float = 2.0):
        self.timeout_seconds = timeout_seconds
        self._minimum_interval = 1.0 / requests_per_second
        self._last_request = 0.0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if path not in REST_PATHS:
            raise ValueError(f"Quotation REST path not allowlisted: {path}")
        if path == "/v1/candles/minutes/1":
            # fixed 1-minute candle endpoint only; caller controls query values.
            if set(params or {}) - {"market", "count", "to"}:
                raise ValueError("Unsupported candle query parameter")
        elif path == "/v1/orderbook":
            if set(params or {}) != {"markets"}:
                raise ValueError("Unsupported public orderbook query parameter")
        elif set(params or {}) - {"is_details"}:
            raise ValueError("Unsupported market-list query parameter")
        delay = self._minimum_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        query = urllib.parse.urlencode(params or {})
        url = f"{REST_ORIGIN}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "trading-paper-system/0.1"}, method="GET")
        self._last_request = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("Upbit Quotation REST returned HTTP 429; paused instead of retrying aggressively") from exc
            raise RuntimeError(f"Upbit Quotation REST returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upbit Quotation REST connection failed: {exc.reason}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Upbit Quotation REST returned invalid JSON") from exc

    def list_krw_pairs(self) -> list[str]:
        rows = self.get("/v1/market/all", {"is_details": "true"})
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected market-list response shape")
        return sorted(
            row["market"] for row in rows
            if isinstance(row, dict)
            and row.get("market", "").startswith("KRW-")
            and row.get("market_warning", "NONE") != "CAUTION"
        )

    def minute_candles(self, market: str, count: int = 200, *, to: str | None = None) -> list[dict[str, Any]]:
        if not market.startswith("KRW-") or not market[4:].isalnum() or not 1 <= count <= 200:
            raise ValueError("Expected a KRW pair and count between 1 and 200")
        params = {"market": market, "count": str(count)}
        if to is not None:
            params["to"] = to
        data = self.get("/v1/candles/minutes/1", params)
        if not isinstance(data, list):
            raise RuntimeError("Unexpected candle response shape")
        return data

    def orderbook(self, market: str) -> dict[str, Any]:
        if not market.startswith("KRW-") or not market[4:].isalnum():
            raise ValueError("Expected a KRW pair")
        rows = self.get("/v1/orderbook", {"markets": market})
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("market") != market:
            raise RuntimeError("Unexpected public orderbook response")
        return {**rows[0], "type": "orderbook", "code": market}

    async def stream(self, markets: list[str]) -> AsyncIterator[tuple[dict[str, Any], datetime]]:
        if not markets or len(markets) > 100 or any(not market.startswith("KRW-") or not market[4:].isalnum() for market in markets):
            raise ValueError("Only 1-100 uppercase KRW pair codes are accepted")
        request = [
            {"ticket": "paper-" + str(time.time_ns())},
            *({"type": kind, "codes": markets, "is_only_realtime": True} for kind in ("ticker", "trade", "orderbook", "candle.1m")),
            {"format": "DEFAULT"},
        ]
        async with websockets.connect(
            PUBLIC_WS_URL,
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            ping_interval=30,
            ping_timeout=15,
            max_size=4 * 1024 * 1024,
        ) as socket:
            await socket.send(json.dumps(request, separators=(",", ":")))
            async for message in socket:
                received_at = datetime.now(timezone.utc)
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                payload = json.loads(message)
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(f"Upbit WebSocket error: {payload['error']}")
                if isinstance(payload, dict) and payload.get("type") in WS_TYPES:
                    yield payload, received_at
