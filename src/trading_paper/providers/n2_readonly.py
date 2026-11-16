"""Read-only NH Investment & Securities N2 market data access.

This adapter intentionally exposes quotation methods only. It has no account,
order, cancel, or replace operation. Credentials and endpoint configuration are
loaded by the runtime from a secure store before the official SDK is imported.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from nhplug import call


CURRENT_PRICE_PATH = "/krstock/quote/v1/currentPrice"
CURRENT_DAILY_PATH = "/krstock/quote/v1/currentDaily"
PERIOD_PATH = "/krstock/quote/v1/period"
ALLOWED_MARKETS = {"KRX"}


def current_price(symbol: str, market: str = "KRX") -> dict[str, Any]:
    """Fetch one N2 current-price response through the official NH SDK."""
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("Expected a six-digit Korean stock code")
    if market not in ALLOWED_MARKETS:
        raise ValueError("Only the KRX market is enabled in this paper adapter")

    response = call(CURRENT_PRICE_PATH, {"iem_cd": symbol, "market_cd": market})
    if not isinstance(response, dict):
        raise RuntimeError("N2 returned an unexpected current-price response")
    output = response.get("Output_0")
    if not isinstance(output, dict):
        raise RuntimeError("N2 current-price response has no Output_0 object")
    try:
        price = Decimal(str(output.get("stck_prpr", "")))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError("N2 current-price response has no valid price") from exc
    if not price.is_finite() or price <= 0:
        raise RuntimeError("N2 current-price response has no valid positive price")
    return response


def current_daily(symbol: str, market: str = "KRX", count: int = 60) -> dict[str, Any]:
    """Fetch official daily bars; returns the SDK response for caller-side validation."""
    _validate_request(symbol, market)
    if not 1 <= count <= 999:
        raise ValueError("Daily bar count must be between 1 and 999")
    response = call(CURRENT_DAILY_PATH, {
        "iem_cd": symbol,
        "market_cd": market,
        "array_cnt": str(count),
        "view_main_yn": "Y",
    })
    if not isinstance(response, dict):
        raise RuntimeError("N2 returned an unexpected daily-price response")
    return response


def period_five_minute(symbol: str, market: str = "KRX") -> dict[str, Any]:
    """Fetch KRX 5-minute period data through the documented read-only endpoint.

    N2's schema exposes bars in Output_1 but does not define whether the latest
    bar is complete or how the HHmmSS field labels its interval. Consumers must
    not create entry signals from these rows until those semantics are verified
    with an in-session sample.
    """
    _validate_request(symbol, market)
    response = call(PERIOD_PATH, {
        "market_cd": market,
        "iem_cd": symbol,
        "gubun": "5",                 # documented as minute periodicity
        "xtick": "005",               # three-character interval field
        "today_cls_code": "1",        # documented as today's bars only
        "fake_tick": "1",             # documented as real bars only
        "sur_flag": "0",              # no backtest/replay mode
        "out1_scale_change": "0",     # preserve provider units
        "out2_scale_change": "0",
        "view_main_yn": "Y",          # KRX regular session only
    })
    if not isinstance(response, dict):
        raise RuntimeError("N2 returned an unexpected period-price response")
    return response


def _validate_request(symbol: str, market: str) -> None:
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("Expected a six-digit Korean stock code")
    if market not in ALLOWED_MARKETS:
        raise ValueError("Only the KRX market is enabled in this paper adapter")
