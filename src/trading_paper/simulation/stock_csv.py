"""CSV input for explicitly complete KRX stock-bar replay data."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_paper.simulation.stock_engine import LLMDecision, StockBar

REQUIRED = {"timestamp", "symbol", "market", "open", "high", "low", "close", "volume", "complete"}


def load_stock_bars(path: str | Path) -> list[StockBar]:
    with Path(path).open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fields = set(reader.fieldnames or ())
        if not REQUIRED.issubset(fields):
            missing = ", ".join(sorted(REQUIRED - fields))
            raise ValueError(f"Stock bar CSV missing columns: {missing}")
        bars: list[StockBar] = []
        seen: set[tuple[str, str]] = set()
        for row_number, row in enumerate(reader, start=2):
            timestamp_text = (row.get("timestamp") or "").strip()
            symbol = (row.get("symbol") or "").strip()
            key = (symbol, timestamp_text)
            if key in seen:
                raise ValueError(f"Duplicate stock bar at CSV row {row_number}")
            seen.add(key)
            complete_text = (row.get("complete") or "").strip().lower()
            if complete_text not in {"true", "false", "1", "0"}:
                raise ValueError(f"Invalid complete flag at CSV row {row_number}")
            turnover_text = (row.get("turnover_krw") or "").strip()
            bars.append(StockBar(
                timestamp=datetime.fromisoformat(timestamp_text),
                symbol=symbol,
                market=(row.get("market") or "").strip(),
                open=Decimal((row.get("open") or "").strip()),
                high=Decimal((row.get("high") or "").strip()),
                low=Decimal((row.get("low") or "").strip()),
                close=Decimal((row.get("close") or "").strip()),
                volume=Decimal((row.get("volume") or "").strip()),
                turnover_krw=Decimal(turnover_text) if turnover_text else None,
                complete=complete_text in {"true", "1"},
            ))
    return sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol))


def load_llm_decisions(path: str | Path | None) -> dict[str, LLMDecision]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("LLM decisions file must contain a JSON array")
    decisions: dict[str, LLMDecision] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each LLM decision must be a JSON object")
        snapshot_id = item.get("snapshot_id")
        symbol = item.get("symbol")
        if not isinstance(snapshot_id, str) or not isinstance(symbol, str):
            raise ValueError("LLM decision needs snapshot_id and symbol")
        if snapshot_id in decisions:
            raise ValueError("Duplicate LLM snapshot_id")
        decisions[snapshot_id] = LLMDecision.from_payload(item, snapshot_id, symbol)
    return decisions
