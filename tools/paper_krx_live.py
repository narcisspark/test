#!/usr/bin/env python3.11
"""Hourly live paper trading of KRX ETF rules during market hours (docs/krx-live-paper.md).

Every run marks five virtual accounts to market with the current ETF prices. Only the run at 15:xx KST makes
trading decisions, using that price as a proxy for today's close and the exact rule code of
tools/stock_etf_backtest.py. Idempotent per (date, hour). Skips holidays (no price dated today).
Virtual money only; no orders are sent anywhere.

Usage: python3.11 tools/paper_krx_live.py --state paper/krx
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import stock_etf_backtest as sb

KST = timezone(timedelta(hours=9))
CODES = sb.CODES  # 069500 KODEX 200, 229200 KOSDAQ150, 114260 treasury 3y
COST = sb.COST
INITIAL = 10_000_000.0
DECISION_HOUR = 15
ACCOUNTS = {
    "sma100_bond": ("sma", dict(n=100, off="bond")),
    "sma20_cash": ("sma", dict(n=20, off="cash")),
    "dualmom126": ("dualmom", dict(l=126)),
    "rsi2_10_sma200": ("rsi2", dict(th=10, filt="sma200")),
    "buyhold_kodex200": ("buyhold", {}),
}


# ------------------------------------------------------------------------------------------ data
def fetch_history(code: str) -> list[tuple[str, float, float]]:
    from download_krx_daily import from_naver, from_yahoo

    for fn in (from_yahoo, from_naver):
        try:
            return [(r["date"], r["close"], r["adj_close"]) for r in fn(code)]
        except Exception:
            continue
    raise RuntimeError(f"history unavailable for {code}")


def _yahoo_live(code: str) -> tuple[str, float, str]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS?range=1d&interval=1m"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    ts = datetime.fromtimestamp(meta["regularMarketTime"], tz=KST)
    return ts.date().isoformat(), float(meta["regularMarketPrice"]), f"yahoo {ts:%H:%M}"


def _naver_live(code: str) -> tuple[str, float, str]:
    from download_krx_daily import from_naver

    last = from_naver(code)[-1]  # today's in-progress daily bar while the market is open
    return last["date"], float(last["close"]), "naver_daily"


def fetch_live(code: str, today: str | None = None, sources=None) -> tuple[str, float, str]:
    """(KST date of the quote, price, source). Yahoo KRX quotes lag ~20 min, so right after the open they
    still carry yesterday's date; in that case (or on error) use Naver's in-progress daily bar."""
    today = today or datetime.now(KST).date().isoformat()
    first = None
    for fn in sources or (_yahoo_live, _naver_live):
        try:
            q = fn(code)
        except Exception:
            continue
        if q[0] == today:
            return q
        first = first or q
    if first is None:
        raise RuntimeError(f"no live quote for {code}")
    return first  # stale everywhere: caller treats it as market closed


# ------------------------------------------------------------------------------------------ logic
def _last_weekday_of_month(d: date) -> bool:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


def _prev_month(d: date) -> str:
    return (d.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def target_weights(kind: str, p: dict, dates: np.ndarray, px: np.ndarray) -> np.ndarray:
    return sb.weights(kind, p, dates, px)[-1].copy()


def new_state() -> dict:
    return dict(last_run=None, last_prices=None, dm_month=None, decisions=0,
                accounts={k: dict(values=[0.0, 0.0, 0.0], cash=INITIAL, weights=[0.0, 0.0, 0.0]) for k in ACCOUNTS})


def equity(acc: dict) -> float:
    return float(sum(acc["values"]) + acc["cash"])


def step(state: dict | None, now: datetime, hist: dict, live: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    """Advance one run. hist: code -> [(date, close, adj_close)], live: code -> (date, price, source).
    Returns (state, info, mark_rows, trade_rows). info['status'] in {'skipped_closed','duplicate','marked','decided'}."""
    state = state or new_state()
    today = now.date().isoformat()
    label = f"{today} {now:%H}"
    if state["last_run"] == label:
        return state, dict(status="duplicate", label=label), [], []
    if any(live[c][0] != today for c in CODES):
        return state, dict(status="skipped_closed", label=label, quote_dates={c: live[c][0] for c in CODES}), [], []
    prices = [live[c][1] for c in CODES]
    if state["last_prices"]:
        for acc in state["accounts"].values():
            acc["values"] = [v * p / lp for v, p, lp in zip(acc["values"], prices, state["last_prices"])]
    state["last_prices"] = prices
    state["last_run"] = label
    trades = []
    decided = now.hour >= DECISION_HOUR
    if decided:
        # history up to yesterday + today's live price scaled onto the adjusted series
        common = sorted(set.intersection(*({d for d, _, _ in hist[c] if d < today} for c in CODES)))
        series = {c: {d: (cl, adj) for d, cl, adj in hist[c]} for c in CODES}
        dates = np.array(common + [today])
        px = np.array([[series[c][d][1] for c in CODES] for d in common]
                      + [[live[c][1] * series[c][common[-1]][1] / series[c][common[-1]][0] for c in CODES]])
        d = now.date()
        dm_due = state["dm_month"] is None or _last_weekday_of_month(d) or state["dm_month"] < _prev_month(d)
        for name, (kind, p) in ACCOUNTS.items():
            acc = state["accounts"][name]
            if kind == "dualmom" and not dm_due:
                w = np.array(acc["weights"])
            else:
                w = target_weights(kind, p, dates, px)
            eq = equity(acc)
            turnover = float(np.abs(eq * w - np.array(acc["values"])).sum())
            cost = COST * turnover
            new_vals = list((eq - cost) * w)
            if any(abs(a - b) > 1e-9 for a, b in zip(w, acc["weights"])):
                trades.append(dict(date=today, account=name, from_weights=json.dumps(acc["weights"]),
                                   to_weights=json.dumps([float(x) for x in w]), cost_krw=round(cost, 1),
                                   prices=json.dumps(prices)))
            acc["values"], acc["cash"], acc["weights"] = new_vals, eq - cost - float(sum(new_vals)), [float(x) for x in w]
        if dm_due:
            state["dm_month"] = d.strftime("%Y-%m") if _last_weekday_of_month(d) else _prev_month(d)
        state["decisions"] += 1
    mark = dict(time_kst=now.strftime("%Y-%m-%d %H:%M"), decision=int(decided),
                **{f"px_{c}": p for c, p in zip(CODES, prices)},
                **{name: round(equity(a), 1) for name, a in state["accounts"].items()},
                source=";".join(live[c][2] for c in CODES))
    return state, dict(status="decided" if decided else "marked", label=label), [mark], trades


# ------------------------------------------------------------------------------------------ io
def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


def write_status(path: Path, state: dict, marks_path: Path) -> None:
    names = {"069500": "KODEX200", "229200": "코스닥150", "114260": "국고채3년"}
    lines = ["# 국내 ETF 실시간 모의투자 (전진 검증)", "",
             "가상 계좌만 사용한다. 실제 주문은 없다. 규칙: [krx-live-paper.md](../../docs/krx-live-paper.md).", "",
             f"- 마지막 실행: {state['last_run']} 시 (KST) · 매매 판단 횟수: {state['decisions']}", "",
             "| 계좌 | 평가액 | 수익률 | 현재 보유 |", "|---|---:|---:|---|"]
    for name, acc in state["accounts"].items():
        eq = equity(acc)
        held = ", ".join(f"{names[c]} {w:.0%}" for c, w in zip(CODES, acc["weights"]) if w) or "현금"
        lines.append(f"| {name} | {eq:,.0f}원 | {eq / INITIAL - 1:+.2%} | {held} |")
    lines += ["", "기록: [marks.csv](marks.csv) (매시간) · [trades.csv](trades.csv) (매매) · [state.json](state.json)"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="paper/krx")
    a = ap.parse_args()
    d = Path(a.state); d.mkdir(parents=True, exist_ok=True)
    sp = d / "state.json"
    state = json.loads(sp.read_text()) if sp.exists() else None
    now = datetime.now(KST)
    live = {c: fetch_live(c, now.date().isoformat()) for c in CODES}
    hist = {c: fetch_history(c) for c in CODES} if now.hour >= DECISION_HOUR else {}
    state, info, marks, trades = step(state, now, hist, live)
    print(f"{info} live={live}")
    if info["status"] in ("marked", "decided"):
        sp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        _append(d / "marks.csv", marks)
        _append(d / "trades.csv", trades)
        write_status(d / "STATUS.md", state, d / "marks.csv")
        for name, acc in state["accounts"].items():
            print(f"  {name:18s} equity={equity(acc):,.0f} weights={acc['weights']}")
        print(f"  trades this run: {len(trades)}")


if __name__ == "__main__":
    main()
