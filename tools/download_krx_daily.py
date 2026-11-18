#!/usr/bin/env python3.11
"""Download long daily history for a few KRX-listed ETFs to <out>/<code>.csv (no credentials).

Source 1: Yahoo Finance chart API (dividend-adjusted `adjclose`). Source 2 (fallback): Naver chart XML
(price only, distributions NOT included). The source used is written into each CSV's `source` column.
Both are unofficial public endpoints: personal research only.

Usage: python3.11 tools/download_krx_daily.py --out data/krx
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ASSETS = {"069500": "KODEX 200", "229200": "KODEX KOSDAQ150", "114260": "KODEX 국고채3년"}
UA = {"User-Agent": "Mozilla/5.0 (research script)", "Accept": "*/*"}


def _get(url: str) -> bytes:
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return r.read()
        except Exception as exc:  # transient / rate limit
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}: {last}")


def from_yahoo(code: str) -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS"
           f"?period1=0&period2={int(time.time())}&interval=1d&events=div")
    res = json.loads(_get(url))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
    if not adj:
        raise RuntimeError("no adjclose")
    rows = []
    for i, ts in enumerate(res["timestamp"]):
        c, a = q["close"][i], adj[i]
        if c is None or a is None:
            continue
        d = datetime.fromtimestamp(ts + 9 * 3600, tz=timezone.utc).date().isoformat()  # KST trading date
        rows.append(dict(date=d, close=float(c), adj_close=float(a), volume=q["volume"][i] or 0, source="yahoo_adjclose"))
    return rows


def from_naver(code: str) -> list[dict]:
    xml = _get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=8000&requestType=0").decode("euc-kr", "replace")
    rows = []
    for d, o, h, l, c, v in re.findall(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"', xml):
        rows.append(dict(date=f"{d[:4]}-{d[4:6]}-{d[6:]}", close=float(c), adj_close=float(c), volume=int(v), source="naver_price_only"))
    if not rows:
        raise RuntimeError("no rows from naver")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/krx")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for code, name in ASSETS.items():
        rows, errs = None, []
        for fn in (from_yahoo, from_naver):
            try:
                rows = fn(code); break
            except Exception as exc:
                errs.append(f"{fn.__name__}: {exc}")
        if not rows:
            raise SystemExit(f"{code} {name}: all sources failed: {errs}")
        rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
        with open(out / f"{code}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"{code} {name}: {len(rows)} days {rows[0]['date']} ~ {rows[-1]['date']} source={rows[0]['source']}"
              + (f" (fallback; {errs})" if errs else ""), flush=True)


if __name__ == "__main__":
    main()
