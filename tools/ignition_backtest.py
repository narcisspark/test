#!/usr/bin/env python3.11
"""Walk-forward test of "detect ignition, ride the flow" rules on Upbit 5-minute candles
(pre-registered in docs/ignition-ride-research.md). Long only, one position per coin, exits when the flow
ends (trailing ATR stop or 15-minute EMA break), max 7 days. Research only; places no orders.

Usage: python3.11 tools/ignition_backtest.py --data data/upbit5m [--open-holdout]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

COST = 0.0010
BAR = 300
MAX_HOLD = 2016  # 7 days of 5-minute bars
ENTRIES = [("E1", dict(r=3)), ("E1", dict(r=5)), ("E2", {}), ("E3", {}), ("E4", dict(r=3)), ("E4", dict(r=5))]
EXITS = [("X1", dict(k=2)), ("X1", dict(k=3)), ("X2", {})]
VARIANTS = [(e, ep, x, xp) for e, ep in ENTRIES for x, xp in EXITS]


# ------------------------------------------------------------------------------------------ data & features
def load(data: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(data.glob("KRW-*.csv")):
        with open(p) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        t = np.array([int(datetime.fromisoformat(r["time_utc"]).replace(tzinfo=timezone.utc).timestamp()) for r in rows])
        out[p.stem] = dict(t=t, **{k: np.array([float(r[col]) for r in rows]) for k, col in
                                   (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "turnover_krw"))})
    return out


def resample(g: dict, sec: int) -> dict:
    """Aggregate 5-minute bars into `sec` buckets. `last[i]` = index of the last bucket COMPLETED by the close of bar i."""
    b = g["t"] // sec * sec
    starts, idx = np.unique(b, return_index=True)
    ends_idx = np.append(idx[1:], len(b))
    O = g["o"][idx]; C = g["c"][ends_idx - 1]
    H = np.array([g["h"][s:e].max() for s, e in zip(idx, ends_idx)])
    L = np.array([g["l"][s:e].min() for s, e in zip(idx, ends_idx)])
    V = np.array([g["v"][s:e].sum() for s, e in zip(idx, ends_idx)])
    last = np.searchsorted(starts + sec, g["t"] + BAR, side="right") - 1
    return dict(start=starts, O=O, H=H, L=L, C=C, V=V, last=last)


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty(len(x)); a = 2 / (n + 1)
    for i, v in enumerate(x):
        out[i] = v if i == 0 else a * v + (1 - a) * out[i - 1]
    out[: n - 1] = np.nan
    return out


def _at(arr: np.ndarray, last: np.ndarray) -> np.ndarray:
    """Value of a bucket series as known at each 5-minute bar close (NaN before the first completed bucket)."""
    out = np.full(len(last), np.nan)
    ok = last >= 0
    out[ok] = arr[last[ok]]
    return out


def features(g: dict) -> dict:
    t, o, h, c, v = g["t"], g["o"], g["h"], g["c"], g["v"]
    n = len(t)
    h1, m15, h4 = resample(g, 3600), resample(g, 900), resample(g, 14400)
    # ATR(14) on completed hourly bars
    pc = np.concatenate(([np.nan], h1["C"][:-1]))
    tr = np.nanmax(np.vstack([h1["H"] - h1["L"], np.abs(h1["H"] - pc), np.abs(h1["L"] - pc)]), axis=0)
    atr = np.convolve(tr, np.ones(14) / 14, mode="full")[: len(tr)]; atr[:13] = np.nan
    # volume-surge breakout (E1): close > prior 2h high, green bar, 15-min turnover vs prior-24h average
    prior_hi = np.full(n, np.nan); cv = np.concatenate(([0.0], np.cumsum(v)))
    for i in range(24, n):
        prior_hi[i] = h[i - 24:i].max()
    turn15 = np.full(n, np.nan); avg15 = np.full(n, np.nan)
    ii = np.arange(n)
    ok = ii >= 288 + 2
    turn15[ok] = cv[ii[ok] + 1] - cv[ii[ok] - 2]
    avg15[ok] = (cv[ii[ok] - 2] - cv[ii[ok] - 2 - 288]) / 96
    base = np.isfinite(prior_hi) & (c > prior_hi) & (c > o) & np.isfinite(avg15) & (avg15 > 0)
    e1 = {r: base & (turn15 >= r * avg15) for r in (3, 5)}
    # 4h trend filter (E4)
    up4 = _at(_ema(h4["C"], 20), h4["last"]) > _at(_ema(h4["C"], 50), h4["last"])
    # squeeze release on hourly Bollinger bands (E2), checked only at the bar that completes an hour
    C1 = h1["C"]; m = len(C1)
    mid = np.convolve(C1, np.ones(20) / 20, mode="full")[:m]; mid[:19] = np.nan
    sd = np.array([C1[max(0, k - 19):k + 1].std() if k >= 19 else np.nan for k in range(m)])
    up, bw = mid + 2 * sd, (4 * sd) / mid
    sq = np.zeros(m, dtype=bool)
    for k in range(720 + 19, m):
        prev = bw[k - 720:k]; prev = prev[np.isfinite(prev)]
        sq[k] = prev.size > 100 and bw[k] <= np.percentile(prev, 10)
    e2h = np.zeros(m, dtype=bool)
    for k in range(2, m):
        e2h[k] = np.isfinite(up[k]) and C1[k] > up[k] and sq[k - 2:k + 1].any()
    hour_done = (t + BAR) % 3600 == 0
    e2 = hour_done & (h1["last"] >= 0) & e2h[np.maximum(h1["last"], 0)]
    # 15-minute EMA(20) break (X2), checked only at the bar that completes a 15-minute bucket
    ema15 = _ema(m15["C"], 20)
    brk15 = np.zeros(m15["C"].size, dtype=bool); brk15[:] = m15["C"] < ema15
    q_done = (t + BAR) % 900 == 0
    x2 = q_done & (m15["last"] >= 0) & brk15[np.maximum(m15["last"], 0)]
    pos = {int(x): i for i, x in enumerate(t)}
    return dict(atr=_at(atr, h1["last"]), e1=e1, e4={r: e1[r] & up4 for r in (3, 5)}, e2=e2, x2=x2, pos=pos)


def ret_1h(g: dict, f: dict, i: int) -> float | None:
    j = f["pos"].get(int(g["t"][i] - 3600))
    return None if j is None else g["c"][i] / g["c"][j] - 1


def entry_mask(kind: str, p: dict, m: str, g: dict, f: dict, btc: tuple | None) -> np.ndarray:
    if kind == "E1":
        return f["e1"][p["r"]]
    if kind == "E4":
        return f["e4"][p["r"]]
    if kind == "E2":
        return f["e2"]
    if kind == "E3":
        out = np.zeros(len(g["t"]), dtype=bool)
        if btc is None or m == "KRW-BTC":
            return out
        bg, bf = btc
        for bi in np.nonzero(bf["e1"][3])[0]:
            i = f["pos"].get(int(bg["t"][bi]))
            if i is None:
                continue
            br, ar = ret_1h(bg, bf, bi), ret_1h(g, f, i)
            if br is not None and ar is not None and br > 0 and ar < 0.3 * br:
                out[i] = True
        return out
    raise ValueError(kind)


def simulate(g: dict, f: dict, entries: np.ndarray, xkind: str, xp: dict) -> list[dict]:
    o, c, t, atr = g["o"], g["c"], g["t"], f["atr"]
    n = len(t)
    trades, i = [], 0
    sig = np.nonzero(entries)[0]
    s = 0
    while s < len(sig):
        i = int(sig[s])
        e = i + 1
        if e >= n or not np.isfinite(atr[i]):
            s += 1; continue
        ep = o[e]; hc = ep; a0 = atr[i]; x = None
        for k in range(e, n - 1):
            hc = max(hc, c[k])
            a = atr[k] if np.isfinite(atr[k]) else a0
            if xkind == "X1":
                hit = c[k] < hc - xp["k"] * a
            else:
                hit = bool(f["x2"][k]) or c[k] < ep - 3 * a0
            if hit or k - e >= MAX_HOLD:
                x = k + 1; break
        if x is None:
            break  # still open when data ends: not counted
        trades.append(dict(entry_time=int(t[e]), exit_time=int(t[x]), ret=o[x] / ep - 1, hold_h=(t[x] - t[e]) / 3600))
        s = int(np.searchsorted(sig, x))  # next signal after the exit bar
    return trades


# ------------------------------------------------------------------------------------------ evaluation
def split_bounds(coins: dict) -> tuple[int, int, int, int]:
    days = sorted({int(x) // 86400 for g in coins.values() for x in g["t"]})
    n = len(days)
    return days[0], days[int(n * 0.5)], days[int(n * 0.75)], days[-1] + 1


def summarize(trades: dict[str, list[dict]], cost: float, lo: int, hi: int, n_coins: int) -> dict:
    sel = {m: [x for x in tr if lo * 86400 < x["exit_time"] <= hi * 86400] for m, tr in trades.items()}
    r = np.array([x["ret"] - 2 * cost for v in sel.values() for x in v])
    if r.size == 0:
        return dict(n=0)
    hold = np.array([x["hold_h"] for v in sel.values() for x in v])
    sleeves = [np.prod([1 + x["ret"] - 2 * cost for x in v]) - 1 if v else 0.0 for v in sel.values()]
    sleeves += [0.0] * (n_coins - len(sleeves))
    srt = np.sort(r)[::-1]; top = srt[: max(1, int(np.ceil(r.size * 0.1)))]
    wins, losses = r[r > 0], r[r <= 0]
    return dict(n=int(r.size), win=float((r > 0).mean()), mean_bp=float(r.mean() * 1e4),
                se_bp=float(r.std(ddof=1) / np.sqrt(r.size) * 1e4) if r.size > 1 else 0.0,
                avg_win_bp=float(wins.mean() * 1e4) if wins.size else 0.0,
                avg_loss_bp=float(losses.mean() * 1e4) if losses.size else 0.0,
                payoff=float(wins.mean() / -losses.mean()) if wins.size and losses.size and losses.mean() < 0 else None,
                hold_median_h=float(np.median(hold)), hold_p90_h=float(np.percentile(hold, 90)),
                top10pct_sum_bp=float(top.sum() * 1e4), total_sum_bp=float(r.sum() * 1e4),
                portfolio_return=float(np.mean(sleeves)))


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "no trades"
    po = f"{s['payoff']:.2f}" if s.get("payoff") else "-"
    return (f"n={s['n']:4d} win={s['win']:4.0%} mean={s['mean_bp']:+6.1f}bp (se {s['se_bp']:.1f}) avgW={s['avg_win_bp']:+.0f} "
            f"avgL={s['avg_loss_bp']:+.0f} payoff={po} hold med {s['hold_median_h']:.1f}h p90 {s['hold_p90_h']:.0f}h "
            f"top10%={s['top10pct_sum_bp']:+.0f}/{s['total_sum_bp']:+.0f}bp portfolio={s['portfolio_return']:+.1%}")


def buyhold(coins: dict, lo: int, hi: int) -> float:
    rets = []
    for g in coins.values():
        m = (g["t"] >= lo * 86400) & (g["t"] < hi * 86400)
        if m.sum() > 1:
            rets.append(g["c"][m][-1] / g["o"][m][0] - 1)
    return float(np.mean(rets)) if rets else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/upbit5m")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--out", default="outputs/ignition/report.json")
    a = ap.parse_args()
    coins = load(Path(a.data))
    a0, b, c, d = split_bounds(coins)
    day = lambda x: datetime.fromtimestamp(x * 86400, tz=timezone.utc).date().isoformat()  # noqa: E731
    print(f"coins={sorted(coins)} train {day(a0)}~{day(b - 1)} val {day(b)}~{day(c - 1)} holdout {day(c)}~ (hidden={not a.open_holdout})")
    print(f"buy&hold equal-weight: train {buyhold(coins, a0, b):+.1%}  val {buyhold(coins, b, c):+.1%}")
    feats = {m: features(g) for m, g in coins.items()}
    btc = (coins["KRW-BTC"], feats["KRW-BTC"]) if "KRW-BTC" in coins else None
    masks = {(m, e, json.dumps(ep)): entry_mask(e, ep, m, coins[m], feats[m], btc) for m in coins for e, ep in ENTRIES}
    rows = []
    for e, ep, x, xp in VARIANTS:
        tr = {m: simulate(coins[m], feats[m], masks[(m, e, json.dumps(ep))], x, xp) for m in coins}
        rows.append(dict(entry=e, entry_params=ep, exit=x, exit_params=xp, train=summarize(tr, COST, a0, b, len(coins)),
                         train_2x=summarize(tr, 2 * COST, a0, b, len(coins)), _tr=tr))
    rows.sort(key=lambda r: -(r["train"].get("mean_bp", -1e9) - r["train"].get("se_bp", 0)))
    print(f"\nvariants tried={len(VARIANTS)}; train ranking by (mean - 1SE), net of {2 * COST:.2%} round trip:")
    for r in rows:
        print(f"  {r['entry']}{r['entry_params'] or ''} + {r['exit']}{r['exit_params'] or ''}: {fmt(r['train'])} | 2x {r['train_2x'].get('mean_bp', 0):+.1f}bp")
    report = dict(variants_tried=len(VARIANTS), cost_per_side=COST,
                  buy_and_hold=dict(train=buyhold(coins, a0, b), val=buyhold(coins, b, c)),
                  train_ranking=[{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    ok_train = [r for r in rows if r["train"].get("n", 0) >= 100 and r["train"]["mean_bp"] - r["train"]["se_bp"] > 0
                and r["train_2x"].get("mean_bp", -1) > 0]
    if not ok_train:
        print("\nno variant passed the train filters (n>=100, mean-1SE>0, 2x-cost mean>0)")
    else:
        ch = ok_train[0]
        v, v2 = summarize(ch["_tr"], COST, b, c, len(coins)), summarize(ch["_tr"], 2 * COST, b, c, len(coins))
        ok = v.get("n", 0) >= 30 and v2.get("mean_bp", -1) > 0 and v.get("portfolio_return", -1) > 0
        print(f"\nCHOSEN {ch['entry']}{ch['entry_params']} + {ch['exit']}{ch['exit_params']}\n  val    : {fmt(v)}\n  val 2x : {fmt(v2)}\n  -> validation {'PASS' if ok else 'fail'}")
        report["chosen"] = dict(entry=ch["entry"], entry_params=ch["entry_params"], exit=ch["exit"], exit_params=ch["exit_params"],
                                val=v, val_2x=v2, passes_validation=ok)
        if a.open_holdout:
            if not ok:
                print("  holdout stays closed: validation failed")
            else:
                h, h2 = summarize(ch["_tr"], COST, c, d, len(coins)), summarize(ch["_tr"], 2 * COST, c, d, len(coins))
                okh = h.get("n", 0) >= 30 and h2.get("mean_bp", -1) > 0 and h.get("portfolio_return", -1) > 0
                print(f"  HOLDOUT   : {fmt(h)}\n  HOLDOUT 2x: {fmt(h2)}\n  buy&hold  : {buyhold(coins, c, d):+.1%}\n  -> holdout {'PASS' if okh else 'fail'}")
                report["holdout"] = dict(strategy=h, strategy_2x=h2, buy_and_hold=buyhold(coins, c, d), passes=okh)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
