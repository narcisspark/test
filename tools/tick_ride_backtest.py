#!/usr/bin/env python3.11
"""Tick-level "ignition + ride the flow" test on Upbit trades/orderbook (docs/tick-ignition-research.md).

1-second grids and 300k-KRW book-walk fills come from mdlib.build_grid (same as search_strategies.py).
Entry and exit are filled DELAY seconds after the decision second. Fees are applied in the summary
(gross trades are identical for base and 2x-fee runs). Research only; places no orders.

Usage: python3.11 tools/tick_ride_backtest.py --root data/market [--open-holdout]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from mdlib import FEE, build_grid, list_hours

DELAY = 1
COOLDOWN = 60
MAX_HOLD = 4 * 3600
PERIODS = {"train": ("20261113", "20261119"), "val": ("20261120", "20261123"), "holdout": ("20261124", "20261127")}
ENTRIES = ["burst", "push", "btclead"]
EXITS = [("Y1", dict(k=3)), ("Y1", dict(k=6)), ("Y2", {})]
VARIANTS = [(e, x, tuple(sorted(xp.items()))) for e in ENTRIES for x, xp in EXITS]  # hashable keys


# ------------------------------------------------------------------------------------------ features
def rolling_max_prior(x: np.ndarray, w: int) -> np.ndarray:
    """out[i] = max(x[i-w:i]) (excludes i); NaN where undefined or all-NaN."""
    n = len(x)
    out = np.full(n, np.nan)
    if n > w:
        xx = np.where(np.isfinite(x), x, -np.inf)
        v = sliding_window_view(xx, w).max(axis=1)  # v[j] = max(xx[j:j+w])
        out[w:] = v[: n - w]
        out[~np.isfinite(out)] = np.nan
    return out


def vol_1m(mid: np.ndarray, window: int = 1800) -> np.ndarray:
    """Mean |60-second mid return| over the prior `window` seconds (excludes the current second)."""
    n = len(mid)
    r = np.full(n, np.nan)
    r[60:] = np.abs(mid[60:] / mid[:-60] - 1)
    ok = np.isfinite(r)
    cs = np.concatenate(([0.0], np.cumsum(np.where(ok, r, 0.0)))); cn = np.concatenate(([0], np.cumsum(ok)))
    i = np.arange(n); lo = np.maximum(i - window, 0)
    s, c = cs[i] - cs[lo], cn[i] - cn[lo]
    return np.where(c >= 300, s / np.maximum(c, 1), np.nan)


def ema_seconds(x: np.ndarray, span: int) -> np.ndarray:
    a = 2 / (span + 1)
    out = np.full(len(x), np.nan); last = np.nan
    for i, v in enumerate(x):
        if np.isfinite(v):
            last = v if not np.isfinite(last) else a * v + (1 - a) * last
        out[i] = last
    return out


def entry_mask(kind: str, g: dict, btc: dict | None) -> np.ndarray:
    base = np.isfinite(g["ask_fill"]) & np.isfinite(g["bid_fill"]) & (g["spread_bp"] <= 15.0) & (g["turn10"] > 0)
    if kind == "burst":
        raw = base & (g["burst"] >= 5.0) & (g["flow10"] >= 0.5) & (g["ret10"] > 0)
    elif kind == "push":
        if "max300" not in g:
            g["max300"] = rolling_max_prior(g["mid"].astype(np.float64), 300)
        raw = base & (g["imb"] >= 0.4) & (g["flow10"] >= 0.5) & (g["mid"] > g["max300"])
    elif kind == "btclead":
        if btc is None or g is btc:
            return np.zeros(len(g["mid"]), dtype=bool)
        raw = base & (btc["ret10"] >= 0.0015) & (g["ret10"] <= 0.3 * btc["ret10"]) & (g["imb"] >= 0.0)
    else:
        raise ValueError(kind)
    raw = np.nan_to_num(raw, nan=0).astype(bool)
    prev = np.concatenate(([False], raw[:-1]))
    return raw & ~prev


def simulate(g: dict, entries: np.ndarray, xkind: str, xp: dict) -> list[tuple[float, int]]:
    """Gross trades (exit/entry - 1, holding seconds)."""
    ask, bid = g["ask_fill"].astype(np.float64), g["bid_fill"].astype(np.float64)
    n = len(ask)
    if xkind == "Y1" and "vol1m" not in g:
        g["vol1m"] = vol_1m(g["mid"].astype(np.float64))
    if xkind == "Y2" and "ema120" not in g:
        g["ema120"] = ema_seconds(g["mid"].astype(np.float64), 120)
    out, free_at = [], 0
    for t in np.nonzero(entries)[0]:
        i = int(t) + 1 + DELAY
        if t < free_at or i >= n - 2 or not np.isfinite(ask[i]):
            continue
        e = ask[i]
        end = min(n - 1, i + MAX_HOLD)
        w = slice(i + 1, end + 1)
        b = bid[w]
        fin = np.isfinite(b)
        if xkind == "Y1":
            start = bid[i] if np.isfinite(bid[i]) else e
            peak = np.maximum.accumulate(np.where(fin, b, -np.inf)); peak = np.maximum(peak, start)
            vol = g["vol1m"][w]
            d = np.maximum(0.003, xp["k"] * np.nan_to_num(vol, nan=0.0))
            cond = fin & (b <= peak * (1 - d))
        else:
            mid, ema = g["mid"][w].astype(np.float64), g["ema120"][w]
            cond = ((g["flow30"][w] <= -0.3) & np.isfinite(mid) & (mid < ema)) | (fin & (b <= e * 0.99))
        if cond.any():
            k = min(n - 1, i + 1 + int(np.argmax(cond)) + DELAY)
        else:
            k = end
        x = None
        for kk in range(k, min(n, k + 10)):
            if np.isfinite(bid[kk]):
                x = bid[kk]; break
        r = (x / e - 1) if x is not None else -0.01
        out.append((float(r), int(k - i)))
        free_at = k + COOLDOWN
    return out


# ------------------------------------------------------------------------------------------ evaluation
def summarize(per_day: dict[str, list[tuple[float, int]]], fee: float) -> dict:
    r = np.array([x[0] - 2 * fee for v in per_day.values() for x in v])
    if r.size == 0:
        return dict(n=0)
    hold = np.array([x[1] for v in per_day.values() for x in v]) / 60
    wins, losses = r[r > 0], r[r <= 0]
    top = np.sort(r)[::-1][: max(1, int(np.ceil(r.size * 0.1)))]
    day_means = [np.mean([x[0] - 2 * fee for x in v]) for v in per_day.values() if v]
    return dict(n=int(r.size), win=float((r > 0).mean()), mean_bp=float(r.mean() * 1e4),
                se_bp=float(r.std(ddof=1) / np.sqrt(r.size) * 1e4) if r.size > 1 else 0.0,
                avg_win_bp=float(wins.mean() * 1e4) if wins.size else 0.0,
                avg_loss_bp=float(losses.mean() * 1e4) if losses.size else 0.0,
                payoff=float(wins.mean() / -losses.mean()) if wins.size and losses.size and losses.mean() < 0 else None,
                hold_median_min=float(np.median(hold)), hold_p90_min=float(np.percentile(hold, 90)),
                top10pct_sum_bp=float(top.sum() * 1e4), total_sum_bp=float(r.sum() * 1e4),
                days=len(day_means), days_positive=float(np.mean([d > 0 for d in day_means])) if day_means else 0.0)


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "no trades"
    po = f"{s['payoff']:.2f}" if s.get("payoff") else "-"
    return (f"n={s['n']:5d} win={s['win']:4.0%} mean={s['mean_bp']:+6.1f}bp (se {s['se_bp']:.1f}) avgW={s['avg_win_bp']:+.0f} "
            f"avgL={s['avg_loss_bp']:+.0f} payoff={po} hold med {s['hold_median_min']:.1f}m p90 {s['hold_p90_min']:.0f}m "
            f"top10%={s['top10pct_sum_bp']:+.0f}/{s['total_sum_bp']:+.0f}bp days+={s['days_positive']:.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/market")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/tick-ride/report.json")
    a = ap.parse_args()
    by_day: dict[str, list[Path]] = {}
    for f in list_hours(Path(a.root)):
        by_day.setdefault(f.parent.name, []).append(f)
    use = {p: [d for d in sorted(by_day) if lo <= d <= hi and len(by_day[d]) == 24] for p, (lo, hi) in PERIODS.items()}
    if not a.open_holdout:
        use["holdout"] = []
    print({p: (v[0], v[-1], len(v)) if v else None for p, v in use.items()}, f"(holdout hidden={not a.open_holdout})", flush=True)
    trades = {v: {} for v in VARIANTS}
    for period, days in use.items():
        for day in days:
            grid = build_grid(by_day[day])
            btc = grid.get("KRW-BTC")
            for m, g in grid.items():
                if m == "KRW-USDT":  # stablecoin pair, not a coin bet
                    continue
                masks = {e: entry_mask(e, g, btc) for e in ENTRIES}
                for e, x, xp in VARIANTS:
                    trades[(e, x, xp)].setdefault((period, day), []).extend(simulate(g, masks[e], x, dict(xp)))
            print(f"  {period} {day}: {len(grid)} markets", flush=True)
            del grid

    def per(v, period):
        return {d: tr for (p, d), tr in trades[v].items() if p == period}

    rows = []
    for v in VARIANTS:
        rows.append(dict(entry=v[0], exit=v[1], exit_params=dict(v[2]), train=summarize(per(v, "train"), FEE),
                         train_2x=summarize(per(v, "train"), 2 * FEE), _v=v))
    rows.sort(key=lambda r: -(r["train"].get("mean_bp", -1e9) - r["train"].get("se_bp", 0)))
    print(f"\nvariants tried={len(VARIANTS)}; train ranking by (mean - 1SE), book-walk fills + {2 * FEE:.2%} fees:")
    for r in rows:
        print(f"  {r['entry']:8s} {r['exit']}{r['exit_params'] or ''}: {fmt(r['train'])} | 2x {r['train_2x'].get('mean_bp', 0):+.1f}bp")
    report = dict(variants_tried=len(VARIANTS), fee_per_side=FEE, days={p: v for p, v in use.items()},
                  train_ranking=[{k: x for k, x in r.items() if not k.startswith("_")} for r in rows])
    ok_train = [r for r in rows if r["train"].get("n", 0) >= 100 and r["train"]["mean_bp"] - r["train"]["se_bp"] > 0
                and r["train_2x"].get("mean_bp", -1) > 0]
    if not ok_train:
        print("\nno variant passed the train filters (n>=100, mean-1SE>0, 2x-fee mean>0)")
    else:
        ch = ok_train[0]
        vs, vs2 = summarize(per(ch["_v"], "val"), FEE), summarize(per(ch["_v"], "val"), 2 * FEE)
        ok = vs.get("n", 0) >= 30 and vs["mean_bp"] > 0 and vs2.get("mean_bp", -1) > 0
        print(f"\nCHOSEN {ch['entry']} {ch['exit']}{ch['exit_params']}\n  val    : {fmt(vs)}\n  val 2x : {fmt(vs2)}\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(entry=ch["entry"], exit=ch["exit"], exit_params=ch["exit_params"], val=vs, val_2x=vs2, passes_validation=ok)
        if a.open_holdout:
            if not ok:
                print("  holdout stays closed: validation failed")
            else:
                h, h2 = summarize(per(ch["_v"], "holdout"), FEE), summarize(per(ch["_v"], "holdout"), 2 * FEE)
                okh = h.get("n", 0) >= 30 and h["mean_bp"] > 0 and h2.get("mean_bp", -1) > 0
                print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  -> holdout {'PASS' if okh else 'fail'}")
                report["holdout"] = dict(strategy=h, strategy_2x=h2, passes=okh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
