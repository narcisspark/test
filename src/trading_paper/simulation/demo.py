from __future__ import annotations

import csv
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.models import Bar

KST = ZoneInfo("Asia/Seoul")
DEMO_SYMBOLS = (
    ("KRW-BTC", "KRW", 50000000.0, "trend"),
    ("KRW-ETH", "KRW", 2500000.0, "reversion"),
    ("KRW-XRP", "KRW", 800.0, "opening_breakout"),
)


def generate_demo_bars() -> list[Bar]:
    """Deterministic, deliberately engineered bars for demonstrating the mechanics only."""
    days = [date(2026, 10, 5) + timedelta(days=offset) for offset in range(5)]
    days = [day for day in days if day.weekday() < 5]
    bars: list[Bar] = []
    for day_number, session in enumerate(days):
        for symbol_number, (symbol, market, base, pattern) in enumerate(DEMO_SYMBOLS):
            previous_close = base * (1 + day_number * 0.001)
            for minute in range(90):
                if pattern == "trend":
                    if minute < 15:
                        offset = 0.00016 * math.sin(minute * 1.3)
                    elif minute < 20:
                        offset = 0.00008 * math.sin(minute)
                    else:
                        offset = 0.00035 * (minute - 19) + 0.00003 * math.sin(minute * 0.9)
                    volume = 4500 + (minute % 7) * 130
                    if minute >= 20:
                        volume *= 2.2
                elif pattern == "reversion":
                    if minute < 20:
                        offset = 0.00012 * math.sin(minute * 0.8)
                    elif minute < 38:
                        offset = -0.00062 * (minute - 19) + 0.00004 * math.sin(minute)
                    else:
                        offset = -0.01116 + 0.00065 * (minute - 37) + 0.00005 * math.sin(minute * 0.7)
                    volume = 6200 + (minute % 5) * 160
                    if 20 <= minute <= 42:
                        volume *= 1.35
                else:
                    if minute < 15:
                        offset = 0.00024 * math.sin(minute * 0.75)
                    elif minute < 20:
                        offset = -0.00028 * (minute - 14)
                    else:
                        offset = -0.0014 + 0.00031 * (minute - 19) + 0.000025 * math.sin(minute)
                    volume = 5200 + (minute % 6) * 180
                    if minute >= 20:
                        volume *= 1.8

                close = base * (1 + day_number * 0.001 + offset)
                open_price = previous_close
                wiggle = base * (0.00012 + 0.000025 * (symbol_number + 1))
                high = max(open_price, close) + wiggle
                low = min(open_price, close) - wiggle
                stamp = datetime.combine(session, time(0, 0), tzinfo=KST) + timedelta(minutes=minute)
                bars.append(Bar(stamp, symbol, market, open_price, high, low, close, volume))
                previous_close = close
    bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
    return bars


def write_demo_csv(bars: list[Bar], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    indexes_by_symbol: dict[str, list[int]] = {}
    for index, bar in enumerate(bars):
        indexes_by_symbol.setdefault(bar.symbol, []).append(index)
    incomplete = {indexes[-1] for indexes in indexes_by_symbol.values() if indexes}
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", "market", "open", "high", "low", "close", "volume", "complete", "turnover_krw"))
        for index, bar in enumerate(bars):
            writer.writerow((
                bar.timestamp.isoformat(), bar.symbol, bar.market,
                f"{bar.open:.4f}", f"{bar.high:.4f}", f"{bar.low:.4f}",
                f"{bar.close:.4f}", f"{bar.volume:.2f}",
                "false" if index in incomplete else "true",
                f"{((bar.high + bar.low + bar.close) / 3.0 * bar.volume):.4f}",
            ))
