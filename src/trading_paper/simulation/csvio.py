from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.models import Bar

KST = ZoneInfo("Asia/Seoul")
REQUIRED_COLUMNS = {"timestamp", "symbol", "market", "open", "high", "low", "close", "volume", "complete"}


def load_bars(path: str | Path) -> list[Bar]:
    source = Path(path)
    with source.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fields
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")
        bars: list[Bar] = []
        seen: set[tuple[str, datetime]] = set()
        for line_number, row in enumerate(reader, start=2):
            if not _is_complete(row["complete"]):
                continue
            try:
                timestamp = _parse_timestamp(row["timestamp"])
                symbol = row["symbol"].strip()
                market = row["market"].strip().upper()
                values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
                turnover = float(row["turnover_krw"]) if row.get("turnover_krw", "").strip() else None
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid CSV row {line_number}: {exc}") from exc
            if not symbol:
                raise ValueError(f"CSV row {line_number}: symbol is empty")
            if market not in {"KOSPI", "KOSDAQ", "KRW"}:
                raise ValueError(f"CSV row {line_number}: market must be KOSPI, KOSDAQ, or KRW")
            if market == "KRW" and not symbol.startswith("KRW-"):
                raise ValueError(f"CSV row {line_number}: KRW crypto symbol must look like KRW-BTC")
            if not all(value >= 0 for value in values.values()) or min(values[k] for k in ("open", "high", "low", "close")) <= 0:
                raise ValueError(f"CSV row {line_number}: prices must be positive and volume non-negative")
            if values["high"] < max(values["open"], values["close"], values["low"]):
                raise ValueError(f"CSV row {line_number}: high is inconsistent with OHLC")
            if values["low"] > min(values["open"], values["close"], values["high"]):
                raise ValueError(f"CSV row {line_number}: low is inconsistent with OHLC")
            key = (symbol, timestamp)
            if key in seen:
                raise ValueError(f"CSV row {line_number}: duplicate symbol/timestamp {symbol} {timestamp.isoformat()}")
            seen.add(key)
            bars.append(Bar(timestamp=timestamp, symbol=symbol, market=market, turnover_krw=turnover, **values))
    bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
    if not bars:
        raise ValueError("CSV contains no complete bars")
    return bars


def _parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include UTC offset or Z")
    return parsed.astimezone(KST)


def _is_complete(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"complete must be true/false, got {value!r}")
