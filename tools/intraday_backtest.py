#!/usr/bin/env python3.11
"""Walk-forward test of intraday (15 min - 1 day) crypto rules on Upbit 5-minute candles
(pre-registered in docs/intraday-crypto-research.md). Long only, flat by the end of each UTC day
(I4/I5 exit after a fixed holding time). Every trade pays 2 x COST. Research only; places no orders.

Usage: python3.11 tools/intraday_backtest.py --data data/upbit5m [--open-holdout]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

COST = 0.0010
BAR = 300  # seconds
VARIANTS = (
    [("vbo", dict(k=k, filt=f)) for k in (0.3, 0.5, 0.7) for f in ("ma5", "none")]
    + [("imom", dict(h=1)), ("imom", dict(h=4)), ("imom_last4", {})]
    + [("orb", dict(m=m)) for m in (30, 60, 120)]
    + [("rev", dict(th=th, hold=hd)) for th in (0.02, 0.03) for hd in (1, 4)]
    + [("lead", dict(th=th, hold=hd)) for th in (0.005, 0.01) for hd in (15, 60)]
)


def load(data: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(data.glob("KRW-*.csv")):
        with open(p) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        t = np.array([int(datetime.fromisoformat(r["time_utc"]).replace(tzinfo=timezone.utc).timestamp()) for r in rows])
        out[p.stem] = dict(t=t, o=np.array([float(r["open"]) for r in rows]), h=np.array([float(r["high"]) for r in rows]),
                           l=np.array([float(r["low"]) for r in rows]), c=np.array([float(r["close"]) for r in rows]))
    return out


def _days(t: np.ndarray) -> dict[int, np.ndarray]:
    day = t // 86400
    out, start = {}, 0
    for i in range(1, len(t) + 1):
        if i == len(t) or day[i] != day[start]:
            out[int(day[start])] = np.arange(start, i); start = i
    return out


def trades_for(kind: str, p: dict, g: dict, btc: dict | None = None) -> list[tuple[int, float]]:
    """(exit_time, gross return) for one coin. Gross = exit/entry - 1 (costs applied later)."""
    t, o, h, l, c = g["t"], g["o"], g["h"], g["l"], g["c"]
    out: list[tuple[int, float]] = []
    days = _days(t)
    keys = sorted(days)
    if kind == "vbo":
        closes = {d: c[days[d][-1]] for d in keys}
        for j in range(1, len(keys)):
            d, pd_ = keys[j], keys[j - 1]
            if pd_ != d - 1:
                continue
            idx, pidx = days[d], days[pd_]
            if p["filt"] == "ma5":
                prev5 = [closes[x] for x in keys[max(0, j - 5):j]]
                if len(prev5) < 5 or closes[pd_] <= np.mean(prev5):
                    continue
            target = o[idx[0]] + p["k"] * (h[pidx].max() - l[pidx].min())
            hit = np.nonzero(h[idx] >= target)[0]
            if hit.size:
                e = max(target, o[idx[hit[0]]])
                out.append((int(t[idx[-1]] + BAR), c[idx[-1]] / e - 1))
        return out
    if kind in ("imom", "imom_last4"):
        for d in keys:
            idx = days[d]; d0 = d * 86400
            at = d0 + (p["h"] * 3600 if kind == "imom" else 20 * 3600)
            before = idx[t[idx] < at]; after = idx[t[idx] >= at]
            if before.size == 0 or after.size == 0 or t[idx[0]] != d0:
                continue
            if c[before[-1]] / o[idx[0]] - 1 > 0:
                e = o[after[0]]
                out.append((int(t[idx[-1]] + BAR), c[idx[-1]] / e - 1))
        return out
    if kind == "orb":
        for d in keys:
            idx = days[d]; d0 = d * 86400
            rng = idx[t[idx] < d0 + p["m"] * 60]; rest = idx[t[idx] >= d0 + p["m"] * 60]
            if rng.size == 0 or rest.size == 0 or t[idx[0]] != d0:
                continue
            hi, lo = h[rng].max(), l[rng].min()
            hit = np.nonzero(h[rest] > hi)[0]
            if not hit.size:
                continue
            k0 = hit[0]; e = max(hi, o[rest[k0]]); x = None
            for k in range(k0, rest.size):  # stop checked from the entry bar on (conservative)
                if l[rest[k]] <= lo:
                    x = min(lo, o[rest[k]]) if k > k0 else lo; xt = t[rest[k]] + BAR; break
            if x is None:
                x, xt = c[rest[-1]], t[rest[-1]] + BAR
            out.append((int(xt), x / e - 1))
        return out
    if kind == "rev":
        pos = {int(x): i for i, x in enumerate(t)}
        free = 0
        for i in range(len(t) - 1):
            if t[i] < free:
                continue
            j = pos.get(int(t[i] - 3600))  # bar starting 1h earlier: its close is 1h before this bar's close
            if j is None or c[i] / c[j] - 1 > -p["th"]:
                continue
            ei = i + 1; xt_target = t[ei] + p["hold"] * 3600
            xi = np.searchsorted(t, xt_target)
            if xi >= len(t):
                break
            out.append((int(t[xi]), o[xi] / o[ei] - 1)); free = t[xi]
        return out
    if kind == "lead":
        if btc is None or g is btc:
            return out
        bpos = {int(x): i for i, x in enumerate(btc["t"])}
        free = 0
        for i in range(len(t) - 1):
            if t[i] < free:
                continue
            b = bpos.get(int(t[i]))
            if b is None:
                continue
            br = btc["c"][b] / btc["o"][b] - 1; ar = c[i] / o[i] - 1
            if br < p["th"] or ar >= 0.3 * br:
                continue
            ei = i + 1
            xi = np.searchsorted(t, t[ei] + p["hold"] * 60)
            if xi >= len(t):
                break
            out.append((int(t[xi]), o[xi] / o[ei] - 1)); free = t[xi]
        return out
    raise ValueError(kind)


def split_bounds(coins: dict) -> tuple[int, int, int, int]:
    days = sorted({int(x) // 86400 for g in coins.values() for x in g["t"]})
    n = len(days)
    return days[0], days[int(n * 0.5)], days[int(n * 0.75)], days[-1] + 1  # day numbers: [a,b) train, [b,c) val, [c,d) holdout


def summarize(trades: dict[str, list[tuple[int, float]]], cost: float, lo: int, hi: int, n_coins: int) -> dict:
    sel = {m: [r - 2 * cost for x, r in tr if lo * 86400 < x <= hi * 86400] for m, tr in trades.items()}
    allr = np.array([r for v in sel.values() for r in v])
    if allr.size == 0:
        return dict(n=0)
    # equal-weight portfolio: each coin sleeve 1/n_coins, trade returns compound within a sleeve
    sleeves = [np.prod(1 + np.array(v)) - 1 if v else 0.0 for v in sel.values()]
    sleeves += [0.0] * (n_coins - len(sleeves))
    return dict(n=int(allr.size), win=float((allr > 0).mean()), mean_bp=float(allr.mean() * 1e4),
                se_bp=float(allr.std(ddof=1) / np.sqrt(allr.size) * 1e4) if allr.size > 1 else 0.0,
                avg_win_bp=float(allr[allr > 0].mean() * 1e4) if (allr > 0).any() else 0.0,
                avg_loss_bp=float(allr[allr <= 0].mean() * 1e4) if (allr <= 0).any() else 0.0,
                portfolio_return=float(np.mean(sleeves)))


def buyhold(coins: dict, lo: int, hi: int) -> dict:
    rets = []
    for g in coins.values():
        m = (g["t"] >= lo * 86400) & (g["t"] < hi * 86400)
        if m.sum() > 1:
            rets.append(g["c"][m][-1] / g["o"][m][0] - 1)
    return dict(equal_weight_return=float(np.mean(rets)) if rets else 0.0)


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "no trades"
    return (f"n={s['n']:5d} win={s['win']:4.0%} mean={s['mean_bp']:+6.1f}bp (se {s['se_bp']:.1f}) "
            f"avgW={s['avg_win_bp']:+.0f} avgL={s['avg_loss_bp']:+.0f} portfolio={s['portfolio_return']:+.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/upbit5m")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/intraday/report.json")
    a = ap.parse_args()
    coins = load(Path(a.data))
    a0, b, c, d = split_bounds(coins)
    day = lambda x: datetime.fromtimestamp(x * 86400, tz=timezone.utc).date().isoformat()  # noqa: E731
    print(f"coins={sorted(coins)} train {day(a0)}~{day(b - 1)} val {day(b)}~{day(c - 1)} holdout {day(c)}~ (hidden={not a.open_holdout})")
    bh = {"train": buyhold(coins, a0, b), "val": buyhold(coins, b, c)}
    print(f"buy&hold equal-weight: train {bh['train']['equal_weight_return']:+.1%}  val {bh['val']['equal_weight_return']:+.1%}")
    btc = coins.get("KRW-BTC")
    rows = []
    for kind, p in VARIANTS:
        tr = {m: trades_for(kind, p, g, btc) for m, g in coins.items()}
        rows.append(dict(kind=kind, params=p, train=summarize(tr, COST, a0, b, len(coins)),
                         train_2x=summarize(tr, 2 * COST, a0, b, len(coins)), _tr=tr))
    rows.sort(key=lambda r: -(r["train"].get("mean_bp", -1e9) - r["train"].get("se_bp", 0)))
    print(f"\nvariants tried={len(VARIANTS)}; train ranking by (mean - 1SE), net of {2 * COST:.2%} round trip:")
    for r in rows:
        print(f"  {r['kind']:10s} {str(r['params']):30s} {fmt(r['train'])} | 2x mean {r['train_2x'].get('mean_bp', 0):+.1f}bp")
    report = dict(variants_tried=len(VARIANTS), cost_per_side=COST, buy_and_hold=bh,
                  train_ranking=[{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    ok_train = [r for r in rows if r["train"].get("n", 0) >= 100 and r["train"]["mean_bp"] - r["train"]["se_bp"] > 0
                and r["train_2x"].get("mean_bp", -1) > 0]
    if not ok_train:
        print("\nno variant passed the train filters (n>=100, mean-1SE>0, 2x-cost mean>0)")
    else:
        ch = ok_train[0]
        v, v2 = summarize(ch["_tr"], COST, b, c, len(coins)), summarize(ch["_tr"], 2 * COST, b, c, len(coins))
        ok = v.get("n", 0) >= 30 and v2.get("mean_bp", -1) > 0 and v.get("portfolio_return", -1) > 0
        print(f"\nCHOSEN {ch['kind']} {ch['params']}\n  val    : {fmt(v)}\n  val 2x : {fmt(v2)}\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(kind=ch["kind"], params=ch["params"], val=v, val_2x=v2, passes_validation=ok)
        if a.open_holdout:
            if not ok:
                print("  holdout stays closed: validation failed")
            else:
                h, h2 = summarize(ch["_tr"], COST, c, d, len(coins)), summarize(ch["_tr"], 2 * COST, c, d, len(coins))
                bhh = buyhold(coins, c, d)
                okh = h.get("n", 0) >= 30 and h2.get("mean_bp", -1) > 0 and h.get("portfolio_return", -1) > 0
                print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  buy&hold  : {bhh['equal_weight_return']:+.1%}\n  -> holdout {'PASS' if okh else 'fail'}")
                report["holdout"] = dict(strategy=h, strategy_2x=h2, buy_and_hold=bhh, passes=okh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
