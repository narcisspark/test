#!/usr/bin/env python3.11
"""Walk-forward test of two crypto-specific candidates pre-registered in docs/candidate-algorithms.md
(section "추가 등록 2026-11-18"):

  H1 tick_maker : passive limit round trip on Upbit KRW coins whose spread is pinned to one price tick.
                  Conservative queue model: we sit behind the whole visible size at our price; only trades
                  at our price (after that queue is consumed) or through our price fill us. Cancels ahead
                  of us are ignored (pessimistic).
  G1 bn_fair    : buy on Upbit when its premium over the Binance-implied KRW price (Binance mid x Upbit
                  KRW-USDT mid) drops below its own trailing 900s mean; exit when the gap closes.

Same discipline as search_strategies.py: time-ordered 60/20/20 split, holdout hidden unless --open-holdout,
2x-cost stress, every combination counted. Research only; nothing here places orders.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

from mdlib import FEE, ORDER_KRW, _walk, build_grid, list_hours, read_records

DELAY = 1
COOLDOWN = 60
EPS = 1e-9

H1_GRID = list(itertools.product((20.0, 40.0), (0.0, 0.5), (60, 300), (60, 300), ("tick", "time")))  # smin, qi, W, H, sl
G1_SIGNALS = list(itertools.product((0.0008, 0.0015, 0.0025), (True, False)))  # theta, require binance 5s up
G1_EXITS = list(itertools.product((0.003, 0.006), (30, 120, 300)))  # SL, max hold
G1_PAIRS = {"KRW-BTC": "BTCUSDT", "KRW-ETH": "ETHUSDT", "KRW-XRP": "XRPUSDT", "KRW-SOL": "SOLUSDT", "KRW-DOGE": "DOGEUSDT"}


# ----------------------------------------------------------------------------------------- data loading
def load_book_and_trades(paths: list[Path], max_stale: int = 5) -> dict[str, dict]:
    """Per market, on a 1s grid: 5-level book (forward-filled <= max_stale), walk fills, level-1 queue
    imbalance, and the raw trade tape (second index, price, volume, seller-initiated flag)."""
    ob: dict[str, list] = {}
    tr: dict[str, list] = {}
    tmin = tmax = None
    alive_secs: set[int] = set()
    for r in read_records(paths):
        sec = r["rx"] // 1000
        alive_secs.add(sec)
        tmin = sec if tmin is None else min(tmin, sec)
        tmax = sec if tmax is None else max(tmax, sec)
        if r["t"] == "ob":
            ob.setdefault(r["m"], []).append((sec, r["u"]))
        else:
            tr.setdefault(r["m"], []).append((sec, r["p"], r["v"], r["s"] == "A"))
    if tmin is None:
        return {}
    n = int(tmax - tmin + 1)
    alive = np.zeros(n, dtype=bool)
    alive[np.fromiter((s - tmin for s in alive_secs), dtype=np.int64)] = True
    last_alive = np.maximum.accumulate(np.where(alive, np.arange(n), -1))
    feed_ok = (np.arange(n) - last_alive) <= max_stale
    out: dict[str, dict] = {}
    for m, rows in ob.items():
        if len(rows) < 100:
            continue
        ap = np.zeros((n, 5)); a_s = np.zeros((n, 5)); bp = np.zeros((n, 5)); b_s = np.zeros((n, 5))
        seen = np.zeros(n, dtype=bool)
        for sec, units in rows:
            i = int(sec - tmin)
            seen[i] = True
            for k, (pa, sa, pb, sb) in enumerate(units[:5]):
                ap[i, k], a_s[i, k], bp[i, k], b_s[i, k] = pa, sa, pb, sb
        idx = np.maximum.accumulate(np.where(seen, np.arange(n), -1))
        ok = (idx >= 0) & ((np.arange(n) - idx) <= max_stale) & feed_ok
        src = np.where(ok, idx, 0)
        ap, a_s, bp, b_s = ap[src], a_s[src], bp[src], b_s[src]
        ap[~ok] = 0.0; bp[~ok] = 0.0
        ok &= (ap[:, 0] > 0) & (bp[:, 0] > 0) & (ap[:, 0] > bp[:, 0])
        mid = np.where(ok, (ap[:, 0] + bp[:, 0]) / 2, np.nan)
        a1 = ap[:, 0] * a_s[:, 0]; b1 = bp[:, 0] * b_s[:, 0]
        t = tr.get(m, [])
        out[m] = dict(
            n=n, t0=tmin, ok=ok, ap=ap.astype(np.float64), a_s=a_s, bp0=bp[:, 0].copy(), bs0=b_s[:, 0].copy(),
            mid=mid, spread_bp=np.where(ok, (ap[:, 0] - bp[:, 0]) / np.where(ok, mid, 1) * 1e4, np.nan),
            qi1=np.divide(b1 - a1, b1 + a1, out=np.zeros(n), where=(b1 + a1) > 0),
            ask_fill=_walk(ap, a_s, ORDER_KRW), bid_fill=_walk(bp, b_s, ORDER_KRW),
            tsec=np.array([x[0] - tmin for x in t], dtype=np.int64),
            tpx=np.array([x[1] for x in t], dtype=np.float64),
            tvol=np.array([x[2] for x in t], dtype=np.float64),
            tsell=np.array([x[3] for x in t], dtype=bool),
        )
    return out


def load_binance_mid(paths: list[Path], t0: int, n: int, max_stale: int = 5) -> dict[str, np.ndarray]:
    raw: dict[str, list] = {}
    for r in read_records(paths):
        if r["t"] == "bt":
            raw.setdefault(r["m"], []).append((r["rx"] // 1000 - t0, (r["b"] + r["a"]) / 2))
    out = {}
    for sym, rows in raw.items():
        arr = np.full(n, np.nan); seen = np.zeros(n, dtype=bool)
        for i, v in rows:
            if 0 <= i < n:
                arr[i] = v; seen[i] = True
        idx = np.maximum.accumulate(np.where(seen, np.arange(n), -1))
        ok = (idx >= 0) & ((np.arange(n) - idx) <= max_stale)
        out[sym] = np.where(ok, arr[np.where(idx >= 0, idx, 0)], np.nan)
    return out


# ----------------------------------------------------------------------------------------- H1 tick_maker
def _finite_after(x: np.ndarray, i: int, lim: int = 30) -> float | None:
    for k in range(i, min(i + lim, len(x))):
        if np.isfinite(x[k]):
            return float(x[k])
    return None


def sim_tick_maker(g: dict, smin: float, qimin: float, wait: int, hold: int, slmode: str, stats: dict) -> list[float]:
    """Gross returns (before fees) of completed round trips."""
    n, ok, bp0, bs0, ap = g["n"], g["ok"], g["bp0"], g["bs0"], g["ap"]
    tsec, tpx, tvol, tsell = g["tsec"], g["tpx"], g["tvol"], g["tsell"]
    elig = ok & (g["spread_bp"] >= smin) & (g["spread_bp"] <= 150) & (g["qi1"] >= qimin)
    cand = np.nonzero(elig)[0]
    rets: list[float] = []
    free_at, ci = 0, 0
    while ci < len(cand):
        t = int(cand[ci])
        if t < free_at:
            ci = int(np.searchsorted(cand, free_at)); continue
        s0 = t + 1
        if s0 + wait + hold + 3 >= n or not ok[s0]:
            ci += 1; continue
        b0, p_exit = float(bp0[s0]), float(ap[s0, 0])
        if p_exit <= b0:
            ci += 1; continue
        qty = ORDER_KRW / b0
        queue = float(bs0[s0])
        stats["placed"] += 1
        lo, hi = np.searchsorted(tsec, s0, "right"), np.searchsorted(tsec, s0 + wait, "right")
        tf, cum = None, 0.0
        for j in range(lo, hi):
            if not tsell[j]:
                continue
            if tpx[j] < b0 - EPS * b0:
                tf = int(tsec[j]); break
            if abs(tpx[j] - b0) <= EPS * b0:
                cum += tvol[j]
                if cum >= queue + qty:
                    tf = int(tsec[j]); break
        if tf is None:
            free_at = s0 + wait
            ci = int(np.searchsorted(cand, free_at)); continue
        stats["filled"] += 1
        # exit order at p_exit; queue = visible size at that price at fill time
        lv = np.nonzero(np.abs(ap[tf] - p_exit) <= EPS * p_exit)[0]
        if lv.size:
            qx = float(g["a_s"][tf, lv[0]])
        elif ap[tf, 0] > p_exit + EPS * p_exit:
            qx = 0.0
        else:
            qx = float("inf")  # price level not visible: only trade-through fills
        j = int(np.searchsorted(tsec, tf, "right"))
        cumx, ret, exit_sec = 0.0, None, None
        for sec in range(tf + 1, tf + hold + 1):
            while j < len(tsec) and tsec[j] <= sec:
                if not tsell[j]:
                    if tpx[j] > p_exit + EPS * p_exit:
                        ret = p_exit / b0 - 1.0
                    elif abs(tpx[j] - p_exit) <= EPS * p_exit:
                        cumx += tvol[j]
                        if cumx >= qx + qty:
                            ret = p_exit / b0 - 1.0
                if ret is not None:
                    break
                j += 1
            if ret is not None:
                stats["exit_maker"] += 1; exit_sec = sec; break
            if slmode == "tick" and ok[sec] and bp0[sec] < b0 - EPS * b0:
                x = _finite_after(g["bid_fill"], sec + DELAY)
                ret = (x / b0 - 1.0) if x is not None else -0.01
                stats["exit_stop"] += 1; exit_sec = sec + DELAY; break
        if ret is None:
            x = _finite_after(g["bid_fill"], tf + hold + DELAY)
            ret = (x / b0 - 1.0) if x is not None else -0.01
            stats["exit_time"] += 1; exit_sec = tf + hold + DELAY
        rets.append(ret)
        free_at = exit_sec + COOLDOWN
        ci = int(np.searchsorted(cand, free_at))
    return rets


# ----------------------------------------------------------------------------------------- G1 bn_fair
def g1_features(up: dict[str, dict], bn: dict[str, np.ndarray]) -> dict[str, dict]:
    usdt = up.get("KRW-USDT", {}).get("mid")
    if usdt is None:
        return {}
    feats = {}
    for m, sym in G1_PAIRS.items():
        if m not in up or sym not in bn:
            continue
        g = up[m]
        prem = g["mid"] / (bn[sym] * usdt) - 1.0
        valid = np.isfinite(prem)
        v = np.where(valid, prem, 0.0)
        cs = np.concatenate(([0.0], np.cumsum(v))); cn = np.concatenate(([0], np.cumsum(valid)))
        n = len(prem); w = 900
        i = np.arange(n); lo = np.maximum(i - w, 0)
        s = cs[i] - cs[lo]; c = cn[i] - cn[lo]  # prior window, excludes current second
        base = np.where(c >= 600, s / np.maximum(c, 1), np.nan)
        bret5 = np.full(n, np.nan); bret5[5:] = bn[sym][5:] / bn[sym][:-5] - 1.0
        feats[m] = dict(dev=prem - base, bret5=bret5, spread_bp=g["spread_bp"], ask_fill=g["ask_fill"], bid_fill=g["bid_fill"])
    return feats


def g1_entries(f: dict, theta: float, need_up: bool) -> np.ndarray:
    raw = np.isfinite(f["dev"]) & (f["dev"] <= -theta) & (f["spread_bp"] <= 15.0) & np.isfinite(f["ask_fill"])
    if need_up:
        raw &= np.nan_to_num(f["bret5"], nan=-1.0) >= 0.0
    prev = np.concatenate(([False], raw[:-1]))
    return np.nonzero(raw & ~prev)[0]


def sim_g1(f: dict, entries: np.ndarray, sl: float, hold: int) -> list[float]:
    ask, bid, dev = f["ask_fill"], f["bid_fill"], f["dev"]
    n = len(ask); rets = []; free_at = 0
    for t in entries:
        i = int(t) + 1 + DELAY
        if i < free_at or i + hold + DELAY + 31 >= n or not np.isfinite(ask[i]):
            continue
        e = float(ask[i]); k_exit = i + hold
        for k in range(i + 1, i + hold + 1):
            if (np.isfinite(dev[k]) and dev[k] >= 0.0) or (np.isfinite(bid[k]) and bid[k] / e - 1.0 <= -sl):
                k_exit = k; break
        x = _finite_after(bid, k_exit + DELAY)
        rets.append((x / e - 1.0) if x is not None else -sl)
        free_at = k_exit + DELAY + COOLDOWN
    return rets


# ----------------------------------------------------------------------------------------- reporting
def summarize(per_unit: dict[str, list[float]], cost: float) -> dict:
    allr = np.array([r - cost for v in per_unit.values() for r in v])
    if allr.size == 0:
        return dict(n=0, win=0.0, mean_bp=0.0, se_bp=0.0, units=0)
    return dict(n=int(allr.size), win=float((allr > 0).mean()), mean_bp=float(allr.mean() * 1e4),
                se_bp=float(allr.std(ddof=1) / np.sqrt(allr.size) * 1e4) if allr.size > 1 else 0.0,
                units=sum(1 for v in per_unit.values() if v))


def passes_train(s: dict, s2: dict, min_n: int, min_win: float) -> bool:
    return s["n"] >= min_n and s["win"] >= min_win and s["mean_bp"] - s["se_bp"] > 0 and s2["mean_bp"] > 0


def split(units: list[str]) -> tuple[list[str], list[str], list[str]]:
    n = len(units); a, b = int(n * 0.6), int(n * 0.2)
    return units[:a], units[a:a + b], units[a + b:]


# ----------------------------------------------------------------------------------------- runners
def run_h1(root: Path, open_holdout: bool, min_win: float) -> dict:
    files = list_hours(root)
    by_day: dict[str, list[Path]] = {}
    for f in files:
        by_day.setdefault(f.parent.name, []).append(f)
    train, val, hold = split(sorted(by_day))
    print(f"[H1 tick_maker] days train={train} val={val} holdout={hold} (hidden={not open_holdout})", flush=True)
    visible = train + val + (hold if open_holdout else [])
    gross: dict = {c: {} for c in H1_GRID}
    stats: dict = {c: {u: dict(placed=0, filled=0, exit_maker=0, exit_stop=0, exit_time=0) for u in visible} for c in H1_GRID}
    for u in visible:
        book = load_book_and_trades(by_day[u])
        for c in H1_GRID:
            rets = []
            for m, g in book.items():
                if m == "KRW-USDT":  # stablecoin pair: excluded, not a coin bet
                    continue
                rets += sim_tick_maker(g, *c, stats[c][u])
            gross[c][u] = rets
        print(f"  {u}: {len(book)} markets simulated", flush=True)
        del book

    def agg(c, units):
        st = {k: sum(stats[c][u][k] for u in units) for k in ("placed", "filled", "exit_maker", "exit_stop", "exit_time")}
        return st

    rows = []
    for c in H1_GRID:
        tr = {u: gross[c][u] for u in train}
        s, s2 = summarize(tr, 2 * FEE), summarize(tr, 4 * FEE)
        rows.append((c, s, s2, agg(c, train)))
    rows.sort(key=lambda r: -r[1]["mean_bp"])
    cands = [r for r in rows if passes_train(r[1], r[2], 100, min_win)]
    print(f"  combos={len(H1_GRID)} passed train filters: {len(cands)}")
    print("  top 8 by train mean (net of 10bp round-trip fee):")
    for c, s, s2, st in rows[:8]:
        fr = st["filled"] / max(st["placed"], 1)
        print(f"    smin={c[0]:.0f}bp qi>={c[1]} wait={c[2]}s hold={c[3]}s sl={c[4]:4s} | n={s['n']:5d} win={s['win']:.0%} "
              f"net={s['mean_bp']:+.1f}bp (2x {s2['mean_bp']:+.1f}) se={s['se_bp']:.1f} | fill={fr:.0%} "
              f"maker_exit={st['exit_maker']} stop={st['exit_stop']} time={st['exit_time']}")
    rep = dict(units=dict(train=train, val=val, holdout=hold), combos=len(H1_GRID),
               train_top=[dict(params=list(c), train=s, train_2x=s2, counts=st) for c, s, s2, st in rows[:8]], candidates=[])
    chosen = None
    for c, s, s2, st in cands[:5]:
        v = {u: gross[c][u] for u in val}
        vs, vs2 = summarize(v, 2 * FEE), summarize(v, 4 * FEE)
        okv = vs["n"] >= 30 and vs["mean_bp"] > 0 and vs2["mean_bp"] > 0 and vs["win"] >= min_win
        rep["candidates"].append(dict(params=list(c), train=s, val=vs, val_2x=vs2, passes_validation=okv))
        print(f"    VAL {c}: n={vs['n']} win={vs['win']:.0%} net={vs['mean_bp']:+.1f}bp 2x={vs2['mean_bp']:+.1f} -> {'PASS' if okv else 'fail'}")
        if okv and chosen is None:
            chosen = c
    rep["chosen"] = None if chosen is None else list(chosen)
    if open_holdout and chosen is not None:
        h = {u: gross[chosen][u] for u in hold}
        rep["holdout"] = dict(net=summarize(h, 2 * FEE), net_2x=summarize(h, 4 * FEE))
        print(f"  HOLDOUT {chosen}: {rep['holdout']}")
    return rep


def run_g1(root: Path, bn_root: Path, open_holdout: bool, min_win: float) -> dict:
    bn_files = list_hours(bn_root)
    bn_blocks: dict[str, list[Path]] = {}
    for f in bn_files:
        bn_blocks.setdefault(f"{f.parent.name}-{int(f.stem.split('.')[0]) // 4}", []).append(f)
    up_blocks: dict[str, list[Path]] = {}
    for f in list_hours(root):
        k = f"{f.parent.name}-{int(f.stem.split('.')[0]) // 4}"
        if k in bn_blocks:
            up_blocks.setdefault(k, []).append(f)
    units = sorted(up_blocks)
    train, val, hold = split(units)
    print(f"[G1 bn_fair] 4h blocks={len(units)} train={len(train)} val={len(val)} holdout={len(hold)} (hidden={not open_holdout})", flush=True)
    visible = train + val + (hold if open_holdout else [])
    keys = [(s, e) for s in G1_SIGNALS for e in G1_EXITS]
    gross: dict = {k: {} for k in keys}
    dev_stats = []
    for u in visible:
        up = build_grid(up_blocks[u], markets=list(G1_PAIRS) + ["KRW-USDT"])
        if not up:
            continue
        any_g = next(iter(up.values())); t0 = int(any_g["t0"]); n = len(any_g["mid"])
        bn = load_binance_mid(bn_blocks[u], t0, n)
        feats = g1_features({m: {k: v.astype(np.float64) if isinstance(v, np.ndarray) else v for k, v in g.items()} for m, g in up.items()}, bn)
        for m, f in feats.items():
            d = f["dev"][np.isfinite(f["dev"])]
            if d.size:
                dev_stats.append((u, m, float(np.percentile(d, 1) * 1e4), float(np.percentile(d, 99) * 1e4)))
        for (theta, need), (sl, hd) in keys:
            rets = []
            for m, f in feats.items():
                rets += sim_g1(f, g1_entries(f, theta, need), sl, hd)
            gross[((theta, need), (sl, hd))][u] = rets
        print(f"  {u}: markets={sorted(feats)}", flush=True)
    rows = []
    for k in keys:
        tr = {u: gross[k].get(u, []) for u in train}
        rows.append((k, summarize(tr, 2 * FEE), summarize(tr, 4 * FEE)))
    rows.sort(key=lambda r: -r[1]["mean_bp"])
    cands = [r for r in rows if passes_train(r[1], r[2], 50, min_win)]
    print(f"  combos={len(keys)} passed train filters: {len(cands)}")
    if dev_stats:
        lo = np.median([x[2] for x in dev_stats]); hi = np.median([x[3] for x in dev_stats])
        print(f"  premium deviation from 900s mean: median 1st pct={lo:+.1f}bp, 99th pct={hi:+.1f}bp")
    print("  top 8 by train mean (net of 10bp round-trip fee):")
    for k, s, s2 in rows[:8]:
        print(f"    theta={k[0][0]*1e4:.0f}bp bn_up={k[0][1]!s:5s} sl={k[1][0]:.1%} hold={k[1][1]}s | n={s['n']:4d} "
              f"win={s['win']:.0%} net={s['mean_bp']:+.1f}bp (2x {s2['mean_bp']:+.1f}) se={s['se_bp']:.1f}")
    rep = dict(units=dict(train=train, val=val, holdout=hold), combos=len(keys),
               train_top=[dict(params=[list(k[0]), list(k[1])], train=s, train_2x=s2) for k, s, s2 in rows[:8]], candidates=[])
    chosen = None
    for k, s, s2 in cands[:5]:
        v = {u: gross[k].get(u, []) for u in val}
        vs, vs2 = summarize(v, 2 * FEE), summarize(v, 4 * FEE)
        okv = vs["n"] >= 20 and vs["mean_bp"] > 0 and vs2["mean_bp"] > 0 and vs["win"] >= min_win
        rep["candidates"].append(dict(params=[list(k[0]), list(k[1])], train=s, val=vs, val_2x=vs2, passes_validation=okv))
        print(f"    VAL {k}: n={vs['n']} win={vs['win']:.0%} net={vs['mean_bp']:+.1f}bp 2x={vs2['mean_bp']:+.1f} -> {'PASS' if okv else 'fail'}")
        if okv and chosen is None:
            chosen = k
    rep["chosen"] = None if chosen is None else [list(chosen[0]), list(chosen[1])]
    if open_holdout and chosen is not None:
        h = {u: gross[chosen].get(u, []) for u in hold}
        rep["holdout"] = dict(net=summarize(h, 2 * FEE), net_2x=summarize(h, 4 * FEE))
        print(f"  HOLDOUT {chosen}: {rep['holdout']}")
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/market")
    ap.add_argument("--binance-root", default="data/binance_ref")
    ap.add_argument("--only", choices=("h1", "g1", "all"), default="all")
    ap.add_argument("--min-win", type=float, default=0.55)
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/search-crypto-edges")
    a = ap.parse_args()
    report = {"combos_tried_cumulative": 486 + len(H1_GRID) + len(G1_SIGNALS) * len(G1_EXITS)}
    if a.only in ("g1", "all"):
        report["g1_bn_fair"] = run_g1(Path(a.root), Path(a.binance_root), a.open_holdout, a.min_win)
    if a.only in ("h1", "all"):
        report["h1_tick_maker"] = run_h1(Path(a.root), a.open_holdout, a.min_win)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out / 'report.json'}")


if __name__ == "__main__":
    main()
