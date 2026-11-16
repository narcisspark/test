#!/usr/bin/env python3.11
"""Reference feed: Binance public best bid/ask + aggregate trades for a few majors (no credentials).

Used only for research on lead-lag between Binance and Upbit (docs/candidate-algorithms.md, family G).
Output: <out>/YYYYMMDD/HH.jsonl.gz with compact records stamped with local receive time `rx` (epoch ms).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from pathlib import Path

import websockets

from collect_market_data import HourlyWriter

log = logging.getLogger("binance-ref")
SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt", "solusdt", "dogeusdt"]


async def run(out: Path) -> None:
    w = HourlyWriter(out)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    streams = "/".join(f"{s}@bookTicker/{s}@aggTrade" for s in SYMBOLS)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    n = {"bt": 0, "tr": 0}
    last_bt: dict[str, int] = {}
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("connected")
                last = time.monotonic()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        log.warning("silent 15s; reconnect")
                        break
                    rx = int(time.time() * 1000)
                    d = json.loads(raw).get("data", {})
                    if "a" in d and "b" in d and "e" not in d:  # bookTicker (throttled to 10/s per symbol)
                        if rx - last_bt.get(d["s"], 0) < 100:
                            continue
                        last_bt[d["s"]] = rx
                        w.write({"t": "bt", "m": d["s"], "rx": rx, "b": float(d["b"]), "bq": float(d["B"]),
                                 "a": float(d["a"]), "aq": float(d["A"])})
                        n["bt"] += 1
                    elif d.get("e") == "aggTrade":
                        w.write({"t": "at", "m": d["s"], "rx": rx, "ts": d["T"], "p": float(d["p"]), "q": float(d["q"]),
                                 "s": "A" if d["m"] else "B"})  # m=True: buyer is maker => taker sold
                        n["tr"] += 1
                    if time.monotonic() - last > 300:
                        last = time.monotonic()
                        log.info("stats %s", n)
        except Exception as exc:
            log.warning("ws error: %s; retry in 5s", exc)
            await asyncio.sleep(5)
    w.close()
    log.info("stopped; %s", n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/binance_ref")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(Path(a.out)))
