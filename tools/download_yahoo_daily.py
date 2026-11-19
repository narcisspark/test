#!/usr/bin/env python3.11
"""Download full daily history for Yahoo Finance symbols to <out>/<symbol>.csv (no credentials).

Columns: date, close, adj_close (dividend-adjusted when Yahoo provides it, else = close), source.
Unofficial public endpoint: personal research only.

Usage: python3.11 tools/download_yahoo_daily.py --out data/us --symbols SPY ^IRX ^GSPC
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

UA = {"User-Agent": "Mozilla/5.0 (research script)", "Accept": "*/*"}


def fetch(symbol: str) -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?period1=-2208988800&period2={int(time.time())}&interval=1d&events=div%7Csplit")
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                res = json.loads(r.read())["chart"]["result"][0]
            break
        except Exception as exc:
            last = exc; time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"{symbol}: {last}")
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    offset = res.get("meta", {}).get("gmtoffset", 0)
    rows = []
    for i, ts in enumerate(res["timestamp"]):
        c = q["close"][i]
        if c is None:
            continue
        a = adj[i] if adj and adj[i] is not None else c
        d = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts + offset)).date().isoformat()
        rows.append(dict(date=d, close=float(c), adj_close=float(a), source="yahoo_adjclose" if adj else "yahoo_close"))
    return sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/us")
    ap.add_argument("--symbols", nargs="+", default=["SPY", "^IRX", "^GSPC"])
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for s in a.symbols:
        rows = fetch(s)
        with open(out / f"{s.replace('^', '_')}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"{s}: {len(rows)} days {rows[0]['date']} ~ {rows[-1]['date']} source={rows[0]['source']}", flush=True)


if __name__ == "__main__":
    main()
