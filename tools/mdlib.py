"""Loading + per-second feature grids for data written by collect_market_data.py."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np

ORDER_KRW = 300_000  # notional used to walk the book for fills
FEE = 0.0005  # per side, simulation assumption (verify against the real account)


def list_hours(root: Path) -> list[Path]:
    return sorted(root.glob("[0-9]*/[0-9][0-9].jsonl.gz"))


def unit_key(path: Path, unit: str) -> str:
    return f"{path.parent.name}" if unit == "day" else f"{path.parent.name}-{path.stem.split('.')[0]}"


def read_records(paths: list[Path]):
    """Yield dict records; tolerate the truncated tail of a gzip file still being written."""
    for p in paths:
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except (EOFError, OSError):
            continue


def _walk(prices: np.ndarray, sizes: np.ndarray, notional: float) -> np.ndarray:
    """Average fill price for `notional` KRW walking 5 levels; NaN if depth is insufficient."""
    remaining = np.full(prices.shape[0], notional, dtype=np.float64)
    qty = np.zeros(prices.shape[0])
    for k in range(prices.shape[1]):
        lvl = prices[:, k] * sizes[:, k]
        take = np.minimum(remaining, lvl)
        qty += np.divide(take, prices[:, k], out=np.zeros_like(take), where=prices[:, k] > 0)
        remaining -= take
    out = np.where(remaining <= 1e-6, notional / np.where(qty > 0, qty, np.nan), np.nan)
    return out


def build_grid(paths: list[Path], markets: list[str] | None = None, max_stale: int = 5) -> dict[str, dict]:
    """Per market: dict of float32 arrays on a 1-second grid starting at `t0` (epoch sec)."""
    ob: dict[str, list] = {}
    tr: dict[str, list] = {}
    tmin, tmax = None, None
    any_secs: set[int] = set()
    for r in read_records(paths):
        m = r["m"]
        if markets and m not in markets:
            continue
        sec = r["rx"] // 1000
        any_secs.add(sec)
        tmin = sec if tmin is None else min(tmin, sec)
        tmax = sec if tmax is None else max(tmax, sec)
        if r["t"] == "ob":
            ob.setdefault(m, []).append((sec, r["u"]))
        else:
            tr.setdefault(m, []).append((sec, r["p"] * r["v"], r["s"]))
    if tmin is None:
        return {}
    n = int(tmax - tmin + 1)
    # Upbit only pushes an orderbook when it changes, so a quiet book is NOT stale. Mark a second
    # invalid only when the whole feed was silent (collector/network outage > max_stale seconds).
    alive = np.zeros(n, dtype=bool)
    alive[np.fromiter((x - tmin for x in any_secs), dtype=np.int64)] = True
    last_alive = np.maximum.accumulate(np.where(alive, np.arange(n), -1))
    feed_ok = (np.arange(n) - last_alive) <= max_stale
    grid: dict[str, dict] = {}
    for m, rows in ob.items():
        if len(rows) < 100:
            continue
        ap = np.full((n, 5), np.nan); a_s = np.zeros((n, 5)); bp = np.full((n, 5), np.nan); b_s = np.zeros((n, 5))
        seen = np.zeros(n, dtype=bool)
        for sec, units in rows:  # last snapshot within each second wins
            i = int(sec - tmin)
            seen[i] = True
            for k, (a_price, a_size, b_price, b_size) in enumerate(units[:5]):
                ap[i, k], a_s[i, k], bp[i, k], b_s[i, k] = a_price, a_size, b_price, b_size
        # forward-fill up to max_stale seconds; beyond that the market is "stale" (NaN => no trade)
        idx = np.where(seen, np.arange(n), -1)
        np.maximum.accumulate(idx, out=idx)
        age = np.arange(n) - idx
        ok = (idx >= 0) & feed_ok
        src = np.where(ok, idx, 0)
        ap, a_s, bp, b_s = ap[src], a_s[src], bp[src], b_s[src]
        ap[~ok] = np.nan; bp[~ok] = np.nan
        ap = np.nan_to_num(ap, nan=0.0); bp = np.nan_to_num(bp, nan=0.0)
        ask_fill = _walk(ap, a_s, ORDER_KRW)
        bid_fill = _walk(bp, b_s, ORDER_KRW)
        ask_ntl = (ap * a_s).sum(1); bid_ntl = (bp * b_s).sum(1)
        imb = np.divide(bid_ntl - ask_ntl, bid_ntl + ask_ntl, out=np.zeros(n), where=(bid_ntl + ask_ntl) > 0)
        mid = np.where(ok & (ap[:, 0] > 0) & (bp[:, 0] > 0), (ap[:, 0] + bp[:, 0]) / 2, np.nan)
        spread_bp = (ap[:, 0] - bp[:, 0]) / mid * 1e4
        buy = np.zeros(n); sell = np.zeros(n)
        for sec, ntl, side in tr.get(m, []):
            (buy if side == "B" else sell)[int(sec - tmin)] += ntl

        def roll(x: np.ndarray, w: int) -> np.ndarray:
            c = np.cumsum(x)
            out = c.copy(); out[w:] = c[w:] - c[:-w]
            return out

        def flow(w: int) -> np.ndarray:
            b, s = roll(buy, w), roll(sell, w)
            return np.divide(b - s, b + s, out=np.zeros(n), where=(b + s) > 0)

        def ret(w: int) -> np.ndarray:
            prev = np.full(n, np.nan); prev[w:] = mid[:-w]
            return mid / prev - 1

        grid[m] = {k: v.astype(np.float32) for k, v in {
            "ask_fill": ask_fill, "bid_fill": bid_fill, "mid": mid, "imb": imb, "spread_bp": spread_bp,
            "flow10": flow(10), "flow30": flow(30), "ret10": ret(10), "ret30": ret(30), "ret60": ret(60), "ret300": ret(300),
            "turn10": roll(buy + sell, 10),
            # volume burst: last-10s turnover vs the average 10s turnover over the preceding 10 minutes
            "burst": np.divide(roll(buy + sell, 10), np.maximum(np.concatenate((np.zeros(600), roll(buy + sell, 600)[:-600])) / 60.0, 1.0)),
        }.items()}
        grid[m]["t0"] = tmin
    return grid
