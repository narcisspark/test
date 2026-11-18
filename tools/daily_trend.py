#!/usr/bin/env python3.11
"""Walk-forward test of long-only daily trend-following rules (pre-registered in docs/daily-trend-research.md).

Signal from the close of day d is applied to the return of day d+1. Costs are charged on every position
change. Portfolio = equal weight across coins with >= MIN_HISTORY days of data, each slot in coin or cash.
The buy-and-hold baseline uses the same coins and the same weighting. Research only; places no orders.

Usage: python3.11 tools/daily_trend.py --data data/daily [--open-holdout]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

COST = 0.0015  # per side: 0.05% fee + 0.10% slippage
MIN_HISTORY = 250
TRAIN_END, VAL_END = "2022-12-31", "2024-06-30"

VARIANTS = (
    [("sma", dict(n=n)) for n in (20, 50, 100, 200)]
    + [("donchian", dict(n=n, m=m)) for n, m in ((20, 10), (55, 20), (100, 50))]
    + [("tsmom", dict(l=l)) for l in (30, 90, 180, 365)]
    + [("dual_sma", dict(f=f, s=s)) for f, s in ((10, 50), (20, 100), (50, 200))]
)


def load(data: Path) -> dict[str, dict[str, np.ndarray]]:
    out = {}
    for p in sorted(data.glob("KRW-*.csv")):
        with open(p) as f:
            rows = list(csv.DictReader(f))
        if rows:
            out[p.stem] = dict(date=np.array([r["date_utc"] for r in rows]),
                               **{k: np.array([float(r[k]) for r in rows]) for k in ("high", "low", "close")})
    return out


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _rolling(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn over the PRIOR n values (excludes today)."""
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        out[i] = fn(x[i - n:i])
    return out


def position(kind: str, p: dict, c: dict[str, np.ndarray]) -> np.ndarray:
    """1.0 = hold the coin after today's close, 0.0 = cash. Uses data up to and including today only."""
    close = c["close"]; n = len(close)
    if kind == "sma":
        s = _sma(close, p["n"]); return np.where(np.isfinite(s) & (close > s), 1.0, 0.0)
    if kind == "tsmom":
        l = p["l"]; past = np.full(n, np.nan); past[l:] = close[:-l]
        return np.where(np.isfinite(past) & (close > past), 1.0, 0.0)
    if kind == "dual_sma":
        f, s = _sma(close, p["f"]), _sma(close, p["s"])
        return np.where(np.isfinite(s) & (f > s), 1.0, 0.0)
    if kind == "donchian":
        hi, lo = _rolling(c["high"], p["n"], np.max), _rolling(c["low"], p["m"], np.min)
        pos = np.zeros(n); state = 0.0
        for i in range(n):
            if state == 0.0 and np.isfinite(hi[i]) and close[i] > hi[i]:
                state = 1.0
            elif state == 1.0 and np.isfinite(lo[i]) and close[i] < lo[i]:
                state = 0.0
            pos[i] = state
        return pos
    raise ValueError(kind)


def coin_returns(pos: np.ndarray, close: np.ndarray, cost: float) -> tuple[np.ndarray, list[float]]:
    """Daily net return of one slot and the list of completed-trade returns.
    r[d] = pos[d-1] * (close[d]/close[d-1]-1) - cost * |pos[d-1]-pos[d-2]|  (trade executed at close d-1)."""
    n = len(close)
    ret = np.zeros(n)
    px = np.zeros(n); px[1:] = close[1:] / close[:-1] - 1.0
    held = np.zeros(n); held[1:] = pos[:-1]
    prev_held = np.zeros(n); prev_held[1:] = held[:-1]
    ret = held * px - cost * np.abs(held - prev_held)
    trades, entry = [], None
    for i in range(n):
        if pos[i] == 1.0 and (i == 0 or pos[i - 1] == 0.0):
            entry = close[i]
        elif pos[i] == 0.0 and i > 0 and pos[i - 1] == 1.0 and entry is not None:
            trades.append(close[i] / entry * (1 - cost) ** 2 - 1.0); entry = None
    return ret, trades


def portfolio(coins: dict, kind: str | None, p: dict | None, cost: float):
    """kind=None => buy-and-hold baseline (enter once when the coin becomes eligible)."""
    dates = sorted({d for c in coins.values() for d in c["date"]})
    idx = {d: i for i, d in enumerate(dates)}
    T = len(dates)
    num = np.zeros(T); den = np.zeros(T); expo = np.zeros(T)
    trades: list[tuple[str, float]] = []
    for c in coins.values():
        n = len(c["close"])
        eligible = np.arange(n) >= MIN_HISTORY
        pos = np.ones(n) if kind is None else position(kind, p, c)
        pos = pos * eligible  # not tradable before it has enough history
        r, tr = coin_returns(pos, c["close"], cost)
        held = np.zeros(n); held[1:] = pos[:-1]
        live = np.zeros(n, dtype=bool); live[1:] = eligible[:-1]
        ii = np.array([idx[d] for d in c["date"]])
        num[ii] += np.where(live, r, 0.0); den[ii] += live; expo[ii] += np.where(live, held, 0.0)
        exit_dates = [c["date"][i] for i in range(1, n) if pos[i] == 0.0 and pos[i - 1] == 1.0]
        trades += list(zip(exit_dates, tr))
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = np.where(den > 0, num / np.maximum(den, 1), 0.0)
        exposure = np.where(den > 0, expo / np.maximum(den, 1), 0.0)
    return np.array(dates), daily, exposure, den, trades


def metrics(dates, daily, exposure, den, trades, start: str | None, end: str | None) -> dict:
    m = np.ones(len(dates), dtype=bool)
    if start: m &= dates >= start
    if end: m &= dates <= end
    m &= den > 0
    r = daily[m]
    if r.size < 30:
        return dict(days=int(r.size))
    eq = np.cumprod(1 + r); peak = np.maximum.accumulate(eq)
    years = r.size / 365.0
    tr = [x for d, x in trades if (not start or d >= start) and (not end or d <= end)]
    return dict(start=str(dates[m][0]), end=str(dates[m][-1]), days=int(r.size),
                total_return=float(eq[-1] - 1), cagr=float(eq[-1] ** (1 / years) - 1),
                vol=float(r.std(ddof=1) * np.sqrt(365)), sharpe=float(r.mean() / r.std(ddof=1) * np.sqrt(365)) if r.std() > 0 else 0.0,
                mdd=float((eq / peak - 1).min()), exposure=float(exposure[m].mean()),
                trades=len(tr), trade_win=float(np.mean([x > 0 for x in tr])) if tr else 0.0)


def fmt(s: dict) -> str:
    if "cagr" not in s:
        return f"days={s.get('days', 0)} (too short)"
    return (f"CAGR={s['cagr']:+7.1%} vol={s['vol']:5.0%} sharpe={s['sharpe']:+.2f} MDD={s['mdd']:+6.1%} "
            f"expo={s['exposure']:4.0%} trades={s['trades']:4d} win={s['trade_win']:4.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/daily")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/daily-trend/report.json")
    a = ap.parse_args()
    coins = load(Path(a.data))
    if not coins:
        raise SystemExit("no data; run tools/download_daily.py first")
    for m, c in coins.items():
        print(f"{m}: {c['date'][0]} ~ {c['date'][-1]} ({len(c['date'])} days)")
    periods = {"train": (None, TRAIN_END), "val": ("2023-01-01", VAL_END), "holdout": ("2024-07-01", None)}
    bh = portfolio(coins, None, None, COST)
    bh_m = {k: metrics(*bh, *v) for k, v in periods.items()}
    print(f"\nbuy&hold train : {fmt(bh_m['train'])}\nbuy&hold val   : {fmt(bh_m['val'])}")
    rows = []
    for kind, p in VARIANTS:
        base = portfolio(coins, kind, p, COST); stress = portfolio(coins, kind, p, 2 * COST)
        rows.append(dict(kind=kind, params=p,
                         train=metrics(*base, *periods["train"]), train_2x=metrics(*stress, *periods["train"]),
                         val=metrics(*base, *periods["val"]), val_2x=metrics(*stress, *periods["val"]),
                         _base=base, _stress=stress))
    print(f"\nvariants tried={len(VARIANTS)}; train results (sorted by Sharpe):")
    rows.sort(key=lambda r: -r["train"].get("sharpe", -9))
    for r in rows:
        print(f"  {r['kind']:9s} {str(r['params']):22s} {fmt(r['train'])}")
    chosen = next((r for r in rows if r["train_2x"].get("cagr", -1) > 0), None)
    report = dict(variants_tried=len(VARIANTS), cost_per_side=COST, buy_and_hold=bh_m,
                  train_ranking=[{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    if chosen is None:
        print("\nno variant has positive train CAGR at 2x cost")
    else:
        v, v2 = chosen["val"], chosen["val_2x"]
        ok = v.get("sharpe", -1) > 0 and v2.get("total_return", -1) > 0 and v.get("mdd", -1) > bh_m["val"].get("mdd", 0)
        print(f"\nCHOSEN {chosen['kind']} {chosen['params']}\n  val    : {fmt(v)}\n  val 2x : {fmt(v2)}\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(kind=chosen["kind"], params=chosen["params"], val=v, val_2x=v2, passes_validation=ok)
        if a.open_holdout:
            h, h2 = metrics(*chosen["_base"], *periods["holdout"]), metrics(*chosen["_stress"], *periods["holdout"])
            okh = h.get("sharpe", -1) > 0 and h2.get("total_return", -1) > 0 and h.get("mdd", -1) > bh_m["holdout"].get("mdd", 0)
            print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  buy&hold  : {fmt(bh_m['holdout'])}\n  -> holdout {'PASS' if okh else 'fail'}")
            report["holdout"] = dict(strategy=h, strategy_2x=h2, buy_and_hold=bh_m["holdout"], passes=okh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
