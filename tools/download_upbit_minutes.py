#!/usr/bin/env python3.11
"""Download Upbit public minute candles (no credentials) to <out>/<market>.csv, oldest first.

Usage: python3.11 tools/download_upbit_minutes.py --unit 5 --days 365 --out data/upbit5m
Upbit returns at most 200 candles per request, newest first; we page backwards with `to` (UTC).
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from download_daily import UNIVERSE

FIELDS = ["time_utc", "open", "high", "low", "close", "turnover_krw"]


def fetch(market: str, unit: int, to: str | None) -> list[dict]:
    q = {"market": market, "count": "200"}
    if to:
        q["to"] = to
    url = f"https://api.upbit.com/v1/candles/minutes/{unit}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "trading-paper-research/0.1"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"failed {market} to={to}")


def download(market: str, unit: int, days: int) -> list[dict]:
    stop = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows: dict[str, dict] = {}
    to = None
    while True:
        batch = fetch(market, unit, to)
        if not batch:
            break
        for c in batch:
            t = c["candle_date_time_utc"]
            rows[t] = dict(time_utc=t, open=c["opening_price"], high=c["high_price"], low=c["low_price"],
                           close=c["trade_price"], turnover_krw=c["candle_acc_trade_price"])
        oldest = min(c["candle_date_time_utc"] for c in batch)
        if oldest <= stop or len(batch) < 200:
            break
        to = oldest.replace("T", " ")
        time.sleep(0.12)
    return [rows[t] for t in sorted(rows) if t >= stop]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", type=int, default=5)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="data/upbit5m")
    ap.add_argument("--markets", nargs="*", default=UNIVERSE)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for m in a.markets:
        rows = download(m, a.unit, a.days)
        with open(out / f"{m}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
        print(f"{m}: {len(rows)} bars {rows[0]['time_utc'] if rows else '-'} ~ {rows[-1]['time_utc'] if rows else '-'}", flush=True)


if __name__ == "__main__":
    main()
