"""Download historical public Upbit minute candles to the replay CSV schema."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_paper.providers.upbit_public import PublicQuotationClient


CSV_FIELDS = (
    "timestamp", "symbol", "market", "open", "high", "low", "close", "volume", "complete", "turnover_krw",
)


def download_minute_history(
    market: str,
    start: datetime,
    end: datetime,
    destination: str | Path,
    *,
    client: PublicQuotationClient | None = None,
) -> dict[str, Any]:
    """Download [start, end) candles, paging backward at the client's safe rate."""
    if not market.startswith("KRW-") or not market[4:].isalnum():
        raise ValueError("Expected an uppercase KRW pair such as KRW-BTC")
    if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("start and end must include a timezone offset")
    start_utc = start.astimezone(timezone.utc)
    if start_utc.second or start_utc.microsecond:
        start_utc = start_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end_utc = end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if start_utc >= end_utc:
        raise ValueError("start must be earlier than end")

    quotation = client or PublicQuotationClient(requests_per_second=2.0)
    cursor = end_utc
    candles: dict[datetime, dict[str, Any]] = {}
    requests = 0
    while cursor > start_utc:
        page = quotation.minute_candles(market, count=200, to=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"))
        requests += 1
        if not page:
            break
        oldest: datetime | None = None
        for candle in page:
            stamp = datetime.fromisoformat(candle["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            if oldest is None or stamp < oldest:
                oldest = stamp
            if start_utc <= stamp < end_utc:
                candles[stamp] = candle
        if oldest is None or oldest >= cursor:
            raise RuntimeError("Historical candle paging did not move backward")
        cursor = oldest

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for stamp, candle in sorted(candles.items()):
            writer.writerow({
                "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                "symbol": market,
                "market": "KRW",
                "open": candle["opening_price"],
                "high": candle["high_price"],
                "low": candle["low_price"],
                "close": candle["trade_price"],
                "volume": candle["candle_acc_trade_volume"],
                "complete": "true",
                "turnover_krw": candle["candle_acc_trade_price"],
            })
    return {
        "market": market,
        "start_utc": start_utc.isoformat(),
        "end_utc_exclusive": end_utc.isoformat(),
        "candles": len(candles),
        "requests": requests,
        "csv": str(target),
    }
