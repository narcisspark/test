#!/usr/bin/env python3.11
"""Walk-forward search for a short-term long-only strategy with positive NET expectancy
and a high win rate, on data from collect_market_data.py.

Discipline built in:
  * Objective is net expectancy after fees + real book-walk fills; win rate is a constraint, not the goal.
  * Time-ordered units (UTC days) split 60/20/20 into train/validation/holdout.
  * The holdout is NOT evaluated unless --open-holdout is passed (do that once, with the final choice).
  * Every combination tried is counted and reported; a random-entry baseline uses identical exits.
  * Entry is delayed (signal second t -> order t+1 -> fill at ask-walk of t+1+delay), exit likewise.
Research only; nothing here places orders.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
from pathlib import Path

import numpy as np

from mdlib import FEE, build_grid, list_hours, unit_key

DELAY = 1  # seconds from decision to fill
COOLDOWN = 60  # seconds after an exit before the same market can re-enter

SIGNALS = []
for imb, flow, mom in itertools.product((0.2, 0.4), (0.2, 0.5), (0.0005, 0.0015)):
    SIGNALS.append(("mom", dict(imb=imb, flow=flow, ret60=mom)))
for imb, drop in itertools.product((0.0, 0.2), (0.001, 0.002)):
    SIGNALS.append(("rev", dict(imb=imb, drop=drop)))
# Pre-registered additions (see docs/candidate-algorithms.md): BTC lead-lag catch-up, shock reversal, volume burst
for btc in (0.0008, 0.0015):
    SIGNALS.append(("lead", dict(btc10=btc)))
for drop in (0.004, 0.008):
    SIGNALS.append(("shock", dict(drop300=drop)))
for ratio in (5.0, 10.0):
    SIGNALS.append(("burst", dict(ratio=ratio)))
EXITS = list(itertools.product((0.003, 0.005, 0.008), (0.003, 0.005, 0.008), (60, 180, 300)))  # TP, SL, hold(s)
MAX_SPREAD_BP = 15.0


def signal_mask(kind: str, p: dict, g: dict, btc: dict | None = None) -> np.ndarray:
    base = np.isfinite(g["ask_fill"]) & np.isfinite(g["bid_fill"]) & (g["spread_bp"] <= MAX_SPREAD_BP) & (g["turn10"] > 0)
    if kind == "lead":
        if btc is None or g is btc:
            return np.zeros(len(g["mid"]), dtype=bool)
        # BTC moved up in the last 10s but this coin has barely moved yet; need healthy flow and book
        raw = base & (btc["ret10"] >= p["btc10"]) & (g["ret10"] <= 0.3 * btc["ret10"]) & (g["imb"] >= 0.0)
    elif kind == "shock":
        # sharp 5-minute drop, selling has stopped (flow turned non-negative) and price ticked up in last 10s
        raw = base & (g["ret300"] <= -p["drop300"]) & (g["flow10"] >= 0.0) & (g["ret10"] > 0) & (g["imb"] >= 0.0)
    elif kind == "burst":
        raw = base & (g["burst"] >= p["ratio"]) & (g["flow10"] >= 0.5) & (g["ret10"] > 0)
    elif kind == "mom":
        raw = base & (g["imb"] >= p["imb"]) & (g["flow10"] >= p["flow"]) & (g["ret60"] >= p["ret60"])
    else:
        raw = base & (g["ret60"] <= -p["drop"]) & (g["imb"] >= p["imb"]) & (g["flow10"] >= 0)
    prev = np.concatenate(([False], raw[:-1]))
    return raw & ~prev  # fire only on the rising edge


def simulate(g: dict, entries: np.ndarray, tp: float, sl: float, hold: int, cost_extra: float = 0.0) -> list[float]:
    ask, bid = g["ask_fill"], g["bid_fill"]
    n = len(ask)
    rets: list[float] = []
    free_at = 0
    for t in entries:
        i = int(t) + 1 + DELAY
        if i < free_at or i + hold + DELAY + 1 >= n or not np.isfinite(ask[i]):
            continue
        e = float(ask[i])
        path = bid[i + 1: i + 1 + hold + 1] / e - 1.0
        hit_tp = np.nonzero(path >= tp)[0]
        hit_sl = np.nonzero(path <= -sl)[0]
        j_tp = hit_tp[0] if hit_tp.size else 10**9
        j_sl = hit_sl[0] if hit_sl.size else 10**9
        j = min(j_tp, j_sl, hold)
        k = i + 1 + int(j) + DELAY  # exit order placed at j, filled DELAY later
        x = bid[k] if np.isfinite(bid[k]) else None
        if x is None:  # no valid bid: assume the stop level was breached
            r = -sl
        else:
            r = float(x) / e - 1.0
        rets.append(r - 2 * FEE - cost_extra)
        free_at = k + COOLDOWN
    return rets


def summarize(per_day: dict[str, list[float]]) -> dict:
    allr = [r for v in per_day.values() for r in v]
    if not allr:
        return dict(n=0, win=0.0, mean_bp=0.0, se_bp=0.0, days=0, day_pos=0.0)
    a = np.array(allr)
    day_means = [np.mean(v) for v in per_day.values() if v]
    return dict(n=len(a), win=float((a > 0).mean()), mean_bp=float(a.mean() * 1e4),
                se_bp=float(a.std(ddof=1) / np.sqrt(len(a)) * 1e4) if len(a) > 1 else 0.0,
                days=len(day_means), day_pos=float(np.mean([d > 0 for d in day_means])))


def evaluate(grids: dict[str, dict], units: list[str], combos, extra_cost: float = 0.0, entries_cache=None):
    """Return {combo_key: summary} over `units`."""
    out = {}
    for skind, sp in SIGNALS:
        skey = (skind, tuple(sorted(sp.items())))
        for tp, sl, hold in EXITS:
            if combos is not None and (skey, tp, sl, hold) not in combos:
                continue
            per_day: dict[str, list[float]] = {}
            for u in units:
                rets: list[float] = []
                for m, g in grids[u].items():
                    ent = entries_cache[(u, m, skey)]
                    rets += simulate(g, ent, tp, sl, hold, extra_cost)
                per_day[u] = rets
            out[(skey, tp, sl, hold)] = summarize(per_day)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/market")
    ap.add_argument("--unit", choices=("day", "hour"), default="day", help="hour = smoke-test only")
    ap.add_argument("--min-win", type=float, default=0.55)
    ap.add_argument("--min-trades-train", type=int, default=100)
    ap.add_argument("--open-holdout", action="store_true", help="evaluate holdout for the single chosen combo")
    ap.add_argument("--out", default="outputs/search")
    ap.add_argument("--exclude-current", action="store_true", help="skip the unit still being written")
    args = ap.parse_args()

    files = list_hours(Path(args.root))
    by_unit: dict[str, list[Path]] = {}
    for f in files:
        by_unit.setdefault(unit_key(f, args.unit), []).append(f)
    units = sorted(by_unit)
    if args.exclude_current and units:
        units = units[:-1]
    if len(units) < 5:
        raise SystemExit(f"need >=5 {args.unit} units, have {len(units)}; keep collecting")
    n = len(units)
    n_train, n_val = int(n * 0.6), int(n * 0.2)
    train, val, hold = units[:n_train], units[n_train:n_train + n_val], units[n_train + n_val:]
    print(f"units={n} train={len(train)} val={len(val)} holdout={len(hold)} (holdout hidden={not args.open_holdout})")

    visible = train + val + (hold if args.open_holdout else [])
    grids, cache = {}, {}
    for u in visible:
        grids[u] = build_grid(by_unit[u])
        for m, g in grids[u].items():
            for skind, sp in SIGNALS:
                skey = (skind, tuple(sorted(sp.items())))
                cache[(u, m, skey)] = np.nonzero(signal_mask(skind, sp, g, grids[u].get("KRW-BTC")))[0]
        print(f"  built {u}: {len(grids[u])} markets, entries={sum(len(v) for k, v in cache.items() if k[0] == u):,}", flush=True)

    tried = len(SIGNALS) * len(EXITS)
    res_train = evaluate(grids, train, None, 0.0, cache)
    res_train2 = evaluate(grids, train, None, 2 * FEE, cache)
    cands = []
    for k, s in res_train.items():
        s2 = res_train2[k]
        if s["n"] >= args.min_trades_train and s["win"] >= args.min_win and s["mean_bp"] - s["se_bp"] > 0 and s2["mean_bp"] > 0:
            cands.append((s["mean_bp"] - s["se_bp"], k, s, s2))
    cands.sort(key=lambda x: -x[0])
    print(f"\ncombos tried={tried}; passed train filters (n>={args.min_trades_train}, win>={args.min_win}, "
          f"mean-1SE>0, 2x-cost mean>0): {len(cands)}")
    top = cands[:5]
    keys = {k for _, k, _, _ in top}
    res_val = evaluate(grids, val, keys, 0.0, cache) if keys else {}
    res_val2 = evaluate(grids, val, keys, 2 * FEE, cache) if keys else {}

    report = dict(units=dict(train=train, val=val, holdout=hold), combos_tried=tried, candidates=[])
    chosen = None
    for score, k, s, s2 in top:
        v, v2 = res_val[k], res_val2[k]
        ok = v["n"] >= 30 and v["mean_bp"] > 0 and v2["mean_bp"] > 0 and v["win"] >= args.min_win
        row = dict(signal=list(k[0][1]) and {"kind": k[0][0], **dict(k[0][1])}, tp=k[1], sl=k[2], hold_s=k[3],
                   train=s, train_2x=s2, val=v, val_2x=v2, passes_validation=ok)
        report["candidates"].append(row)
        print(f"  {k[0][0]} {dict(k[0][1])} TP={k[1]:.3%} SL={k[2]:.3%} H={k[3]}s | "
              f"train n={s['n']} win={s['win']:.1%} mean={s['mean_bp']:+.1f}bp | "
              f"val n={v['n']} win={v['win']:.1%} mean={v['mean_bp']:+.1f}bp 2x={v2['mean_bp']:+.1f}bp -> {'PASS' if ok else 'fail'}")
        if ok and chosen is None:
            chosen = k
    report["chosen"] = None if chosen is None else dict(signal=dict(chosen[0][1]), kind=chosen[0][0], tp=chosen[1], sl=chosen[2], hold_s=chosen[3])

    if args.open_holdout:
        if chosen is None:
            print("\nNo candidate passed validation: nothing to open the holdout for.")
        else:
            h = evaluate(grids, hold, {chosen}, 0.0, cache)[chosen]
            h2 = evaluate(grids, hold, {chosen}, 2 * FEE, cache)[chosen]
            # random-entry baseline with identical exits
            rng, base_rets = random.Random(0), {}
            for u in hold:
                rets = []
                for m, g in grids[u].items():
                    ne = len(cache[(u, m, chosen[0])])
                    ok_idx = np.nonzero(np.isfinite(g["ask_fill"]))[0]
                    if ne and ok_idx.size:
                        pick = np.sort(np.array(rng.sample(list(ok_idx), min(ne, ok_idx.size))))
                        rets += simulate(g, pick, chosen[1], chosen[2], chosen[3])
                base_rets[u] = rets
            b = summarize(base_rets)
            report["holdout"] = dict(strategy=h, strategy_2x_cost=h2, random_entry_baseline=b)
            print(f"\nHOLDOUT strategy: n={h['n']} win={h['win']:.1%} mean={h['mean_bp']:+.1f}bp (2x cost {h2['mean_bp']:+.1f}bp) days+={h['day_pos']:.0%}")
            print(f"HOLDOUT random-entry baseline: n={b['n']} win={b['win']:.1%} mean={b['mean_bp']:+.1f}bp")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out / 'report.json'}")


if __name__ == "__main__":
    main()
