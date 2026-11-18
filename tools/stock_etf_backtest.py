#!/usr/bin/env python3.11
"""Walk-forward test of daily ETF rules on KRX (pre-registered in docs/stock-etf-research.md).

Weights decided at the close of day t are applied to the return of day t+1. Costs: cost_per_side x turnover.
Columns: 0 = EQ1 KODEX 200, 1 = EQ2 KODEX KOSDAQ150, 2 = BOND KODEX treasury 3y; the rest is cash (0 return).
The holdout (strategy AND baseline) is only evaluated with --open-holdout. Research only; places no orders.

Usage: python3.11 tools/stock_etf_backtest.py --data data/krx [--open-holdout]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

CODES = ["069500", "229200", "114260"]
COST = 0.0007
WARMUP = 252
TRAIN_END, VAL_START, VAL_END, HOLD_START = "2020-12-31", "2021-01-01", "2022-12-31", "2023-01-01"

VARIANTS = (
    [("sma", dict(n=n, off=off)) for n in (20, 50, 100, 200) for off in ("cash", "bond")]
    + [("dualmom", dict(l=l)) for l in (63, 126, 252)]
    + [("rsi2", dict(th=th, filt=f)) for th in (5, 10, 20) for f in ("sma200", "none")]
)


def load(data: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    series, sources = {}, []
    for code in CODES:
        with open(data / f"{code}.csv") as f:
            rows = list(csv.DictReader(f))
        series[code] = {r["date"]: float(r["adj_close"]) for r in rows}
        sources.append(f"{code}:{rows[0].get('source', '?')}")
    dates = sorted(set.intersection(*(set(s) for s in series.values())))
    px = np.array([[series[c][d] for c in CODES] for d in dates])
    return np.array(dates), px, sources


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0)); out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rsi2(x: np.ndarray) -> np.ndarray:
    d = np.diff(x, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    au, ad = _sma(np.nan_to_num(up), 2), _sma(np.nan_to_num(dn), 2)
    out = np.where(au + ad > 0, 100 * au / np.where(au + ad > 0, au + ad, 1), 50.0)
    out[:2] = np.nan
    return out


def weights(kind: str, p: dict, dates: np.ndarray, px: np.ndarray) -> np.ndarray:
    """T x 3 target weights after the close of each day, using data up to that day only."""
    T = len(px); w = np.zeros((T, 3)); eq = px[:, 0]
    if kind == "sma":
        s = _sma(eq, p["n"]); on = np.isfinite(s) & (eq > s)
        w[on, 0] = 1.0
        if p["off"] == "bond":
            w[np.isfinite(s) & ~on, 2] = 1.0
        return w
    if kind == "dualmom":
        l = p["l"]; month = np.array([d[:7] for d in dates]); state = np.zeros(3)
        for t in range(T):
            month_end = t == T - 1 or month[t + 1] != month[t]
            if month_end and t >= l:
                r = px[t] / px[t - l] - 1
                best = 0 if r[0] >= r[1] else 1
                state = np.zeros(3); state[best if r[best] > r[2] else 2] = 1.0
            w[t] = state
        return w
    if kind == "rsi2":
        r, s5, s200 = rsi2(eq), _sma(eq, 5), _sma(eq, 200); held = False
        for t in range(T):
            if not held and np.isfinite(r[t]) and r[t] < p["th"] and (p["filt"] == "none" or (np.isfinite(s200[t]) and eq[t] > s200[t])):
                held = True
            elif held and np.isfinite(s5[t]) and eq[t] > s5[t]:
                held = False
            w[t, 0] = 1.0 if held else 0.0
        return w
    if kind == "buyhold":
        w[:, 0] = 1.0; return w
    raise ValueError(kind)


def run(w: np.ndarray, px: np.ndarray, cost: float) -> tuple[np.ndarray, np.ndarray]:
    """Daily net portfolio returns and the held weights for each day."""
    ret = np.zeros_like(px); ret[1:] = px[1:] / px[:-1] - 1
    held = np.zeros_like(w); held[1:] = w[:-1]
    prev = np.zeros_like(w); prev[1:] = held[:-1]
    r = (held * ret).sum(1) - cost * np.abs(held - prev).sum(1)
    return r, held


def trades(r: np.ndarray, held: np.ndarray) -> list[tuple[int, float]]:
    """A trade = a contiguous run of days holding the same non-cash asset mix; returns (end_index, return)."""
    out, start, key = [], None, None
    for t in range(len(r) + 1):
        k = tuple(held[t]) if t < len(r) else None
        if k != key:
            if key is not None and any(key):
                out.append((t - 1, float(np.prod(1 + r[start:t + 1 if t < len(r) else t]) - 1)))
            start, key = t, k
    return out


def metrics(dates, r, held, tr, start, end) -> dict:
    m = (dates >= start if start else np.ones(len(dates), bool)) & (dates <= end if end else True)
    m &= np.arange(len(dates)) >= WARMUP
    x = r[m]
    if x.size < 60:
        return dict(days=int(x.size))
    eq = np.cumprod(1 + x); years = x.size / 252
    idx = np.nonzero(m)[0]; lo, hi = idx[0], idx[-1]
    tt = [v for i, v in tr if lo <= i <= hi]
    return dict(start=str(dates[lo]), end=str(dates[hi]), days=int(x.size), total_return=float(eq[-1] - 1),
                cagr=float(eq[-1] ** (1 / years) - 1), vol=float(x.std(ddof=1) * np.sqrt(252)),
                sharpe=float(x.mean() / x.std(ddof=1) * np.sqrt(252)) if x.std() > 0 else 0.0,
                mdd=float((eq / np.maximum.accumulate(eq) - 1).min()),
                equity_exposure=float(held[m][:, :2].sum(1).mean()), trades=len(tt),
                trade_win=float(np.mean([v > 0 for v in tt])) if tt else 0.0,
                avg_win=float(np.mean([v for v in tt if v > 0])) if any(v > 0 for v in tt) else 0.0,
                avg_loss=float(np.mean([v for v in tt if v <= 0])) if any(v <= 0 for v in tt) else 0.0)


def fmt(s: dict) -> str:
    if "cagr" not in s:
        return f"days={s.get('days', 0)} (too short)"
    return (f"CAGR={s['cagr']:+6.1%} vol={s['vol']:4.0%} sharpe={s['sharpe']:+.2f} MDD={s['mdd']:+6.1%} "
            f"eq_expo={s['equity_exposure']:4.0%} trades={s['trades']:4d} win={s['trade_win']:4.0%} "
            f"avgW={s['avg_win']:+.2%} avgL={s['avg_loss']:+.2%}")


def evaluate(kind, p, dates, px, cost):
    w = weights(kind, p, dates, px)
    r, held = run(w, px, cost)
    return r, held, trades(r, held)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/krx")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/stock-etf/report.json")
    a = ap.parse_args()
    dates, px, sources = load(Path(a.data))
    print(f"common dates {dates[0]} ~ {dates[-1]} ({len(dates)} days, first {WARMUP} are warm-up); sources {sources}")
    periods = {"train": (None, TRAIN_END), "val": (VAL_START, VAL_END)}
    bh = evaluate("buyhold", {}, dates, px, COST)
    bh_m = {k: metrics(dates, *bh, *v) for k, v in periods.items()}
    print(f"KODEX200 buy&hold train: {fmt(bh_m['train'])}\nKODEX200 buy&hold val  : {fmt(bh_m['val'])}")
    rows = []
    for kind, p in VARIANTS:
        base, stress = evaluate(kind, p, dates, px, COST), evaluate(kind, p, dates, px, 2 * COST)
        rows.append(dict(kind=kind, params=p, train=metrics(dates, *base, *periods["train"]),
                         train_2x=metrics(dates, *stress, *periods["train"]), _base=base, _stress=stress))
    rows.sort(key=lambda r: -r["train"].get("sharpe", -9))
    print(f"\nvariants tried={len(VARIANTS)}; train ranking by Sharpe:")
    for r in rows:
        print(f"  {r['kind']:8s} {str(r['params']):30s} {fmt(r['train'])}")
    report = dict(variants_tried=len(VARIANTS), cost_per_side=COST, sources=sources, buy_and_hold=bh_m,
                  train_ranking=[{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    chosen = next((r for r in rows if r["train_2x"].get("cagr", -1) > 0), None)
    if chosen is None:
        print("\nno variant has positive train CAGR at 2x cost")
    else:
        v, v2 = metrics(dates, *chosen["_base"], *periods["val"]), metrics(dates, *chosen["_stress"], *periods["val"])
        ok = v.get("sharpe", -1) > 0 and v2.get("total_return", -1) > 0 and v.get("mdd", -1) > bh_m["val"].get("mdd", 0)
        print(f"\nCHOSEN {chosen['kind']} {chosen['params']}\n  val    : {fmt(v)}\n  val 2x : {fmt(v2)}\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(kind=chosen["kind"], params=chosen["params"], val=v, val_2x=v2, passes_validation=ok)
        if a.open_holdout:
            if not ok:
                print("  holdout stays closed: validation failed")
            else:
                h, h2 = metrics(dates, *chosen["_base"], HOLD_START, None), metrics(dates, *chosen["_stress"], HOLD_START, None)
                bhh = metrics(dates, *bh, HOLD_START, None)
                okh = h.get("sharpe", -1) > 0 and h2.get("total_return", -1) > 0 and h.get("mdd", -1) > bhh.get("mdd", 0)
                print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  KODEX200 B&H: {fmt(bhh)}\n  -> holdout {'PASS' if okh else 'fail'}")
                report["holdout"] = dict(strategy=h, strategy_2x=h2, buy_and_hold=bhh, passes=okh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
