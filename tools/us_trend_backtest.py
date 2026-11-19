#!/usr/bin/env python3.11
"""Walk-forward test of trend rules on the S&P 500 (pre-registered in docs/us-trend-research.md).

Equity weight w in {0,1} decided at the close of day t applies to day t+1; the rest earns T-bills (^IRX).
Costs: cost_per_side x |change in w|. Sharpe uses returns in excess of T-bills. The holdout (strategy AND
baseline) is only evaluated with --open-holdout. Research only; places no orders.

Usage: python3.11 tools/us_trend_backtest.py --data data/us [--open-holdout]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

COST = 0.0010
WARMUP = 252
TRAIN = ("1994-01-01", "2006-12-31")
VAL = ("2007-01-01", "2013-12-31")
HOLD = ("2014-01-01", None)
PRESAMPLE = ("1960-01-01", "1993-01-28")

VARIANTS = (
    [("sma", dict(n=n)) for n in (50, 100, 150, 200)]
    + [("msma", dict(m=m)) for m in (6, 10, 12)]
    + [("tsmom", dict(l=l)) for l in (63, 126, 252)]
    + [("dual", dict(f=f, s=s)) for f, s in ((50, 200), (20, 100))]
)


def _read(path: Path) -> dict[str, float]:
    with open(path) as f:
        return {r["date"]: float(r["adj_close"]) for r in csv.DictReader(f)}


def load(data: Path, equity_file: str = "SPY.csv") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dates, equity prices and daily T-bill returns (previous day's ^IRX yield / 252)."""
    eq, irx = _read(data / equity_file), _read(data / "_IRX.csv")
    dates = np.array(sorted(d for d in eq if d >= min(irx)))
    px = np.array([eq[d] for d in dates])
    y, last = np.zeros(len(dates)), None
    irx_dates = sorted(irx); j = 0
    for i, d in enumerate(dates):  # forward-fill the yield onto equity dates
        while j < len(irx_dates) and irx_dates[j] <= d:
            last = irx[irx_dates[j]]; j += 1
        y[i] = last if last is not None else 0.0
    rf = np.zeros(len(dates)); rf[1:] = y[:-1] / 100.0 / 252.0
    return dates, px, rf


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0)); out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _month_ends(dates: np.ndarray) -> np.ndarray:
    m = np.array([d[:7] for d in dates])
    return np.append(m[1:] != m[:-1], True)


def weights(kind: str, p: dict, dates: np.ndarray, px: np.ndarray, rf: np.ndarray) -> np.ndarray:
    """Equity weight after each close, using data up to that close only."""
    T = len(px); w = np.zeros(T)
    if kind == "buyhold":
        return np.ones(T)
    if kind == "sma":
        s = _sma(px, p["n"]); return np.where(np.isfinite(s) & (px > s), 1.0, 0.0)
    if kind == "dual":
        f, s = _sma(px, p["f"]), _sma(px, p["s"]); return np.where(np.isfinite(s) & (f > s), 1.0, 0.0)
    me = _month_ends(dates)
    # the last row may be a month end only because the data stops there; that is still "known at that close"
    state, closes = 0.0, []
    growth = np.cumprod(1 + rf)
    for t in range(T):
        if me[t]:
            closes.append(px[t])
            if kind == "msma" and len(closes) >= p["m"]:
                state = 1.0 if px[t] > np.mean(closes[-p["m"]:]) else 0.0
            elif kind == "tsmom" and t >= p["l"]:
                state = 1.0 if px[t] / px[t - p["l"]] - 1 > growth[t] / growth[t - p["l"]] - 1 else 0.0
        w[t] = state
    if kind not in ("msma", "tsmom"):
        raise ValueError(kind)
    return w


def run(w: np.ndarray, px: np.ndarray, rf: np.ndarray, cost: float) -> tuple[np.ndarray, np.ndarray]:
    ret = np.zeros(len(px)); ret[1:] = px[1:] / px[:-1] - 1
    held = np.zeros(len(w)); held[1:] = w[:-1]
    prev = np.zeros(len(w)); prev[1:] = held[:-1]
    return held * ret + (1 - held) * rf - cost * np.abs(held - prev), held


def trades(r: np.ndarray, held: np.ndarray) -> list[tuple[int, float]]:
    out, start = [], None
    for t in range(len(r) + 1):
        h = held[t] if t < len(r) else 0.0
        if h and start is None:
            start = t
        elif not h and start is not None:
            out.append((t - 1, float(np.prod(1 + r[start:t]) - 1))); start = None
    return out


def metrics(dates, r, held, rf, tr, start, end) -> dict:
    m = np.arange(len(dates)) >= WARMUP
    if start: m &= dates >= start
    if end: m &= dates <= end
    x, f = r[m], rf[m]
    if x.size < 60:
        return dict(days=int(x.size))
    years = x.size / 252
    eq = np.cumprod(1 + x); ex = x - f
    idx = np.nonzero(m)[0]; tt = [v for i, v in tr if idx[0] <= i <= idx[-1]]
    return dict(start=str(dates[idx[0]]), end=str(dates[idx[-1]]), days=int(x.size),
                cagr=float(eq[-1] ** (1 / years) - 1), tbill_cagr=float(np.prod(1 + f) ** (1 / years) - 1),
                vol=float(x.std(ddof=1) * np.sqrt(252)),
                sharpe=float(ex.mean() / ex.std(ddof=1) * np.sqrt(252)) if ex.std() > 0 else 0.0,
                mdd=float((eq / np.maximum.accumulate(eq) - 1).min()), exposure=float(held[m].mean()),
                trades=len(tt), trade_win=float(np.mean([v > 0 for v in tt])) if tt else 0.0)


def fmt(s: dict) -> str:
    if "cagr" not in s:
        return f"days={s.get('days', 0)} (too short)"
    return (f"CAGR={s['cagr']:+6.1%} (tbill {s['tbill_cagr']:+.1%}) vol={s['vol']:4.0%} sharpe={s['sharpe']:+.2f} "
            f"MDD={s['mdd']:+6.1%} expo={s['exposure']:4.0%} trades={s['trades']:4d} win={s['trade_win']:4.0%}")


def passes(s: dict, s2: dict, bh: dict) -> tuple[bool, list[str]]:
    c = [s2.get("cagr", -1) > s2.get("tbill_cagr", 9), s.get("mdd", -1) > bh.get("mdd", 0),
         s.get("sharpe", -9) >= 0.8 * bh.get("sharpe", 9)]
    names = ["2x-cost CAGR > T-bill", "MDD better than buy&hold", "Sharpe >= 0.8 x buy&hold"]
    return all(c), [f"{n}: {'ok' if ok else 'FAIL'}" for n, ok in zip(names, c)]


def evaluate(kind, p, dates, px, rf, cost):
    r, held = run(weights(kind, p, dates, px, rf), px, rf, cost)
    return r, held, rf, trades(r, held)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/us")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/us-trend/report.json")
    a = ap.parse_args()
    dates, px, rf = load(Path(a.data))
    print(f"SPY {dates[0]} ~ {dates[-1]} ({len(dates)} days, first {WARMUP} warm-up)")
    bh = evaluate("buyhold", {}, dates, px, rf, COST)
    bh_m = {"train": metrics(dates, *bh, *TRAIN), "val": metrics(dates, *bh, *VAL)}
    print(f"SPY buy&hold train: {fmt(bh_m['train'])}\nSPY buy&hold val  : {fmt(bh_m['val'])}")
    rows = []
    for kind, p in VARIANTS:
        base, stress = evaluate(kind, p, dates, px, rf, COST), evaluate(kind, p, dates, px, rf, 2 * COST)
        rows.append(dict(kind=kind, params=p, train=metrics(dates, *base, *TRAIN), train_2x=metrics(dates, *stress, *TRAIN),
                         _base=base, _stress=stress))
    rows.sort(key=lambda r: -r["train"].get("sharpe", -9))
    print(f"\nvariants tried={len(VARIANTS)}; train ranking by Sharpe (excess of T-bills):")
    for r in rows:
        print(f"  {r['kind']:6s} {str(r['params']):20s} {fmt(r['train'])}")
    report = dict(variants_tried=len(VARIANTS), cost_per_side=COST, buy_and_hold=bh_m,
                  train_ranking=[{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    chosen = next((r for r in rows if r["train_2x"].get("cagr", -1) > r["train_2x"].get("tbill_cagr", 9)), None)
    if chosen is None:
        print("\nno variant beats T-bills at 2x cost in train")
    else:
        v, v2 = metrics(dates, *chosen["_base"], *VAL), metrics(dates, *chosen["_stress"], *VAL)
        ok, why = passes(v, v2, bh_m["val"])
        print(f"\nCHOSEN {chosen['kind']} {chosen['params']}\n  val    : {fmt(v)}\n  val 2x : {fmt(v2)}\n  "
              + "; ".join(why) + f"\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(kind=chosen["kind"], params=chosen["params"], val=v, val_2x=v2, passes_validation=ok, criteria=why)
        gspc = Path(a.data) / "_GSPC.csv"
        if gspc.exists():  # pre-sample check: never used for selection
            gd, gp, grf = load(Path(a.data), "_GSPC.csv")
            gs = metrics(gd, *evaluate(chosen["kind"], chosen["params"], gd, gp, grf, COST), *PRESAMPLE)
            gb = metrics(gd, *evaluate("buyhold", {}, gd, gp, grf, COST), *PRESAMPLE)
            print(f"  PRE-SAMPLE ^GSPC (price only, no dividends): strategy {fmt(gs)}\n                                   buy&hold {fmt(gb)}")
            report["presample_gspc_price_only"] = dict(strategy=gs, buy_and_hold=gb)
        if a.open_holdout:
            if not ok:
                print("  holdout stays closed: validation failed")
            else:
                h, h2 = metrics(dates, *chosen["_base"], *HOLD), metrics(dates, *chosen["_stress"], *HOLD)
                bhh = metrics(dates, *bh, *HOLD)
                okh, whyh = passes(h, h2, bhh)
                print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  SPY B&H   : {fmt(bhh)}\n  " + "; ".join(whyh)
                      + f"\n  -> holdout {'PASS' if okh else 'fail'}")
                report["holdout"] = dict(strategy=h, strategy_2x=h2, buy_and_hold=bhh, passes=okh, criteria=whyh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
