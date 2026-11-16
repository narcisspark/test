#!/usr/bin/env python3.11
"""Local Upbit public market-data collector (read-only, no credentials).

Records trades and 1-per-second top-5 orderbook snapshots for the top-N KRW
markets by 24h turnover. Output: <out>/YYYYMMDD/HH.jsonl.gz (UTC hours), one
JSON object per line, each stamped with local receive time (rx, epoch ms).
The universe is re-selected hourly and logged to <out>/universe.jsonl.

Usage: python3.11 tools/collect_market_data.py --out data/market --top 30
Stop with Ctrl-C / SIGTERM (flushes cleanly).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import shutil
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets

REST = "https://api.upbit.com"
WS_URL = "wss://api.upbit.com/websocket/v1"
LEVELS = 5
log = logging.getLogger("collector")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rest_get(path: str, params: dict[str, str] | None = None):
    url = REST + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "trading-paper-collector/0.1"})
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def select_universe(top: int) -> list[dict]:
    rows = rest_get("/v1/market/all", {"is_details": "true"})
    markets = [r["market"] for r in rows if r["market"].startswith("KRW-") and r.get("market_warning", "NONE") == "NONE"]
    tickers = []
    for i in range(0, len(markets), 100):
        tickers += rest_get("/v1/ticker", {"markets": ",".join(markets[i:i + 100])})
        time.sleep(0.3)
    tickers.sort(key=lambda t: t["acc_trade_price_24h"], reverse=True)
    return [{"market": t["market"], "turnover_24h": t["acc_trade_price_24h"]} for t in tickers[:top]]


class HourlyWriter:
    def __init__(self, root: Path):
        self.root, self.key, self.fh = root, None, None
        self.last_flush = time.monotonic()
        self.lines = 0

    def write(self, rec: dict) -> None:
        now = datetime.now(timezone.utc)
        key = now.strftime("%Y%m%d/%H")
        if key != self.key:
            self.close()
            path = self.root / f"{key}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = gzip.open(path, "at", encoding="utf-8")  # append = new gzip member
            self.key = key
        self.fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.lines += 1
        if time.monotonic() - self.last_flush > 5:
            self.fh.flush()
            self.last_flush = time.monotonic()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


async def run(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    writer = HourlyWriter(out)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    stats = {"trade": 0, "ob": 0, "bad": 0}
    last_ob: dict[str, float] = {}

    while not stop.is_set():
        try:
            universe = select_universe(args.top)
        except Exception as exc:  # network/API hiccup: retry
            log.warning("universe selection failed: %s", exc)
            await asyncio.sleep(10)
            continue
        codes = [u["market"] for u in universe]
        with open(out / "universe.jsonl", "a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "universe": universe}) + "\n")
        log.info("universe (%d): %s", len(codes), ",".join(c[4:] for c in codes))
        deadline = time.monotonic() + args.reselect_seconds
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps([
                    {"ticket": "collector"},
                    {"type": "trade", "codes": codes},
                    {"type": "orderbook", "codes": codes},
                    {"format": "DEFAULT"},
                ]))
                last_stat = time.monotonic()
                while not stop.is_set() and time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        log.warning("no data for 15s; reconnecting")
                        break
                    rx = int(time.time() * 1000)
                    try:
                        m = json.loads(raw)
                        typ, code = m.get("type"), m.get("code")
                        if typ == "trade":
                            writer.write({"t": "trade", "m": code, "rx": rx, "ts": m["trade_timestamp"],
                                          "p": m["trade_price"], "v": m["trade_volume"], "s": m["ask_bid"][0],
                                          "id": m["sequential_id"]})
                            stats["trade"] += 1
                        elif typ == "orderbook":
                            if m.get("stream_type") == "SNAPSHOT" or rx - last_ob.get(code, 0) >= args.ob_interval_ms:
                                last_ob[code] = rx
                                units = m["orderbook_units"][:LEVELS]
                                writer.write({"t": "ob", "m": code, "rx": rx, "ts": m["timestamp"],
                                              "u": [[u["ask_price"], u["ask_size"], u["bid_price"], u["bid_size"]] for u in units],
                                              "ta": m["total_ask_size"], "tb": m["total_bid_size"]})
                                stats["ob"] += 1
                    except (KeyError, ValueError, TypeError):
                        stats["bad"] += 1
                    if time.monotonic() - last_stat > 60:
                        last_stat = time.monotonic()
                        free = shutil.disk_usage(out).free / 1e9
                        log.info("stats %s free_disk=%.1fGB", stats, free)
                        if free < args.min_free_gb:
                            log.error("disk below %.1fGB; stopping", args.min_free_gb)
                            stop.set()
        except Exception as exc:
            log.warning("ws error: %s; retry in 5s", exc)
            await asyncio.sleep(5)
    writer.close()
    log.info("stopped cleanly; final stats %s", stats)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/market")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--ob-interval-ms", type=int, default=1000)
    p.add_argument("--reselect-seconds", type=int, default=3600)
    p.add_argument("--min-free-gb", type=float, default=5.0)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
