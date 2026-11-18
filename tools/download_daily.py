#!/usr/bin/env python3.11
"""Download full Upbit public daily candle history (no credentials) to <out>/<market>.csv.

Usage: python3.11 tools/download_daily.py --out data/daily
Upbit returns at most 200 candles per request, newest first; we page backwards with `to`.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

UNIVERSE = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-ADA", "KRW-DOGE", "KRW-SOL", "KRW-LINK", "KRW-ETC"]
URL = "https://api.upbit.com/v1/candles/days"
FIELDS = ["date_utc", "open", "high", "low", "close", "volume", "turnover_krw"]


def fetch(market: str, to: str | None) -> list[dict]:
    q = {"market": market, "count": "200"}
    if to:
        q["to"] = to
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(q),
                                 headers={"Accept": "application/json", "User-Agent": "trading-paper-research/0.1"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:  # rate limit / transient network: back off and retry
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"failed to fetch {market} to={to}")


def download(market: str) -> list[dict]:
    rows: dict[str, dict] = {}
    to = None
    while True:
        batch = fetch(market, to)
        if not batch:
            break
        for c in batch:
            d = c["candle_date_time_utc"][:10]
            rows[d] = dict(date_utc=d, open=c["opening_price"], high=c["high_price"], low=c["low_price"],
                           close=c["trade_price"], volume=c["candle_acc_trade_volume"], turnover_krw=c["candle_acc_trade_price"])
        oldest = min(c["candle_date_time_utc"] for c in batch)
        if len(batch) < 200:
            break
        to = oldest.replace("T", " ")
        time.sleep(0.15)
    return [rows[d] for d in sorted(rows)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/daily")
    ap.add_argument("--markets", nargs="*", default=UNIVERSE)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for m in a.markets:
        rows = download(m)
        with open(out / f"{m}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
        print(f"{m}: {len(rows)} days {rows[0]['date_utc'] if rows else '-'} ~ {rows[-1]['date_utc'] if rows else '-'}", flush=True)


if __name__ == "__main__":
    main()
