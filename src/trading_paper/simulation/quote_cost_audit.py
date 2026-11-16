"""Offline snapshot-cost audit for previously recorded public BTC orderbooks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
from typing import Any

from trading_paper.simulation.orderflow_features import Features, buy_fill, sell_fill

FEE_RATE = Decimal("0.0005")


def snapshot_cost_bps(book: list[dict], budget: Decimal) -> float | None:
    """Buy and immediately sell against the same visible book, with modeled fees."""
    buy = buy_fill(book, budget, fee_rate=FEE_RATE, slippage_rate=Decimal(0))
    if buy is None:
        return None
    sell = sell_fill(book, buy["qty"], fee_rate=FEE_RATE, slippage_rate=Decimal(0))
    if sell is None:
        return None
    return float((buy["total"] - sell["total"]) / buy["total"] * Decimal(10_000))


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p10": None, "p50": None, "p90": None, "p99": None}
    values.sort()

    def percentile(p: float) -> float:
        position = (len(values) - 1) * p
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "p10": percentile(0.10),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "p99": percentile(0.99),
    }


def audit(paths: list[Path], budgets: tuple[int, ...] = (500_000, 2_000_000)) -> dict[str, Any]:
    features = Features()
    spreads: list[float] = []
    costs: dict[int, list[float]] = {budget: [] for budget in budgets}
    insufficient: dict[int, int] = {budget: 0 for budget in budgets}
    counts = {"events": 0, "orderbook_events": 0, "invalid_or_stale_books": 0, "sampled_books": 0}
    last_second: int | None = None
    first_at = last_at = None

    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as source:
            for line in source:
                event = json.loads(line)
                counts["events"] += 1
                if event.get("kind") == "connection" and not event.get("connected"):
                    features = Features()
                    continue
                payload = event.get("payload", {})
                if event.get("kind") != "websocket" or payload.get("type") != "orderbook":
                    continue
                if payload.get("code") != "KRW-BTC":
                    continue
                counts["orderbook_events"] += 1
                now = datetime.fromisoformat(event["received_at"]).timestamp()
                invalid_before = features.invalid
                features.on_event(payload, now)
                if features.invalid != invalid_before or features.book_at != now:
                    counts["invalid_or_stale_books"] += 1
                    continue
                second = int(now)
                if last_second is not None and second <= last_second:
                    continue
                last_second = second
                book = features.book
                bid = float(book[0]["bid_price"])
                ask = float(book[0]["ask_price"])
                spreads.append((ask - bid) / ((ask + bid) / 2) * 10_000)
                counts["sampled_books"] += 1
                stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
                first_at = first_at or stamp
                last_at = stamp
                for budget in budgets:
                    cost = snapshot_cost_bps(book, Decimal(budget))
                    if cost is None:
                        insufficient[budget] += 1
                    else:
                        costs[budget].append(cost)

    return {
        "classification": "HISTORICAL QUOTE SNAPSHOT COST AUDIT — NOT A FILL OR PNL BACKTEST",
        "source_files": [str(path) for path in sorted(paths)],
        "market": "KRW-BTC",
        "first_received_utc": first_at,
        "last_received_utc": last_at,
        "counts": counts,
        "assumptions": {
            "sampling": "first valid BTC orderbook in each received second",
            "book_depth": "consume up to five observed price levels on both sides",
            "fees": "assumed 0.05% per side",
            "additional_slippage": "0; sensitivity and execution latency not represented in this snapshot",
            "interpretation": "same snapshot for both sides; no queue, cancellation, delay, or future price change",
        },
        "quoted_spread_bps": _summary(spreads),
        "round_trip_cost_bps": {
            str(budget): {**_summary(costs[budget]), "insufficient_visible_depth": insufficient[budget]}
            for budget in budgets
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    paths = sorted(Path(args.events_dir).glob("*.jsonl.gz"))
    if not paths:
        raise SystemExit("No sealed event logs found")
    result = audit(paths)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in (
        "first_received_utc", "last_received_utc", "counts", "quoted_spread_bps", "round_trip_cost_bps"
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
