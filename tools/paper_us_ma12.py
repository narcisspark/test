#!/usr/bin/env python3.11
"""Daily paper tracking of the S&P 500 monthly MA(12) rule that passed docs/us-trend-research.md.

Run once a day after the US close (e.g. 07:10 KST). Uses only completed SPY daily bars (dividend-adjusted)
and ^IRX for cash. Same accounting as tools/us_trend_backtest.py: the position decided at the close of day d
holds over day d+1, the rest earns T-bills, cost on changes. Decisions happen only at month-end closes.
Idempotent; missed days are caught up in order. Virtual money only; no orders are sent anywhere.

Usage: python3.11 tools/paper_us_ma12.py --state paper/us_ma12
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import us_trend_backtest as ub

M = 12
COST = ub.COST
INITIAL_USD = 10_000.0
ET = timezone(timedelta(hours=-5))  # conservative: treat bars as complete only after 16:15 EST


def _next_weekday(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def month_end_flags(dates: list[str]) -> list[bool]:
    """True where the next row is in another month; for the last row use the calendar (next weekday)."""
    out = [dates[i][:7] != dates[i + 1][:7] for i in range(len(dates) - 1)]
    if dates:
        d = date.fromisoformat(dates[-1])
        out.append(_next_weekday(d).month != d.month)
    return out


def decide(px: np.ndarray, flags: list[bool], i: int) -> float:
    """Monthly MA(M) at row i (a month end): close > mean of the last M month-end closes including this one."""
    closes = [px[j] for j in range(i + 1) if flags[j]]
    if len(closes) < M:
        return 0.0
    return 1.0 if px[i] > float(np.mean(closes[-M:])) else 0.0


def current_position(px: np.ndarray, flags: list[bool], upto: int) -> float:
    """Position in force after the close of row `upto` = decision at the latest month end <= upto."""
    ends = [j for j in range(upto + 1) if flags[j]]
    return decide(px, flags, ends[-1]) if ends else 0.0


def update(state: dict | None, dates: list[str], px: np.ndarray, rf: np.ndarray) -> tuple[dict, list[dict], list[dict]]:
    flags = month_end_flags(dates)
    idx = {d: i for i, d in enumerate(dates)}
    daily, trades = [], []
    if state is None:
        i0 = len(dates) - 1
        w = current_position(px, flags, i0)
        state = dict(strategy=f"S&P 500 monthly MA({M}) via SPY, cash in T-bills", cost_per_side=COST,
                     start_date=dates[i0], last_date=dates[i0], w=w, prev_w=0.0, equity=INITIAL_USD, bench=INITIAL_USD,
                     bench_in=False, peak=INITIAL_USD, mdd=0.0, bench_peak=INITIAL_USD, bench_mdd=0.0)
        if w:
            trades.append(dict(date=dates[i0], action="BUY", spy_adj_close=round(float(px[i0]), 4), note="start: adopt current rule state"))
        daily.append(dict(date=dates[i0], equity=INITIAL_USD, ret=0.0, bench=INITIAL_USD, bench_ret=0.0, w_next=w,
                          month_end=int(flags[i0]), note="start"))
        return state, daily, trades
    for i in range(idx[state["last_date"]] + 1, len(dates)):
        p = px[i] / px[i - 1] - 1
        r = state["w"] * p + (1 - state["w"]) * rf[i] - COST * abs(state["w"] - state["prev_w"])
        b = p - (0.0 if state["bench_in"] else COST)
        state["bench_in"] = True
        state["equity"] *= 1 + r; state["bench"] *= 1 + b
        for k, pk, dd in (("equity", "peak", "mdd"), ("bench", "bench_peak", "bench_mdd")):
            state[pk] = max(state[pk], state[k]); state[dd] = min(state[dd], state[k] / state[pk] - 1)
        new_w = decide(px, flags, i) if flags[i] else state["w"]
        if new_w != state["w"]:
            trades.append(dict(date=dates[i], action="BUY" if new_w else "SELL", spy_adj_close=round(float(px[i]), 4), note="month-end decision"))
        state["prev_w"], state["w"], state["last_date"] = state["w"], new_w, dates[i]
        daily.append(dict(date=dates[i], equity=round(state["equity"], 2), ret=round(r, 6), bench=round(state["bench"], 2),
                          bench_ret=round(b, 6), w_next=new_w, month_end=int(flags[i]), note=""))
    return state, daily, trades


def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


def write_status(path: Path, s: dict, trades_path: Path) -> None:
    recent = list(csv.DictReader(open(trades_path)))[-10:] if trades_path.exists() else []
    lines = [f"# 모의 운영: {s['strategy']}", "",
             "가상 자금만 사용한다(달러 기준, 세금·환율 미반영). 실제 주문은 없다. 연구: [us-trend-research.md](../../docs/us-trend-research.md).", "",
             f"- 시작: {s['start_date']} 종가 (초기 가상자금 ${INITIAL_USD:,.0f})",
             f"- 마지막 반영 일봉: {s['last_date']} (미국 장 마감 기준)",
             f"- 전략 평가액: ${s['equity']:,.2f} ({s['equity'] / INITIAL_USD - 1:+.2%}), 최대낙폭 {s['mdd']:.2%}",
             f"- SPY 매수 후 보유: ${s['bench']:,.2f} ({s['bench'] / INITIAL_USD - 1:+.2%}), 최대낙폭 {s['bench_mdd']:.2%}",
             f"- 다음 거래일 포지션: {'SPY 100%' if s['w'] else '미국 단기 국채(현금) 100%'} (판단은 월말 종가에서만)",
             f"- 비용 가정: 편도 {s['cost_per_side']:.2%}", "",
             "## 매매 기록", "", "| 날짜 | 동작 | SPY 수정종가 | 비고 |", "|---|---|---:|---|",
             *[f"| {t['date']} | {t['action']} | {float(t['spy_adj_close']):,.2f} | {t['note']} |" for t in recent], "",
             "일별 기록: [daily.csv](daily.csv) · 매매: [trades.csv](trades.csv) · 상태: [state.json](state.json)"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def completed(dates: list[str], now_et: datetime) -> int:
    """Number of leading rows that are completed bars (drop today's bar before 16:15 ET)."""
    today = now_et.date().isoformat()
    cutoff = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
    if dates and dates[-1] == today and now_et < cutoff:
        return len(dates) - 1
    return len(dates)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="paper/us_ma12")
    ap.add_argument("--data", default="data/us", help="where tools/download_yahoo_daily.py writes SPY/^IRX")
    a = ap.parse_args()
    from download_yahoo_daily import fetch

    data = Path(a.data); data.mkdir(parents=True, exist_ok=True)
    for sym in ("SPY", "^IRX"):
        rows = fetch(sym)
        with open(data / f"{sym.replace('^', '_')}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    dates, px, rf = ub.load(data)
    n = completed(list(dates), datetime.now(ET))
    dates, px, rf = list(dates[:n]), px[:n], rf[:n]
    d = Path(a.state); d.mkdir(parents=True, exist_ok=True)
    sp = d / "state.json"
    state = json.loads(sp.read_text()) if sp.exists() else None
    before = state["last_date"] if state else None
    state, daily, trades = update(state, dates, px, rf)
    sp.write_text(json.dumps(state, indent=2))
    _append(d / "daily.csv", daily)
    _append(d / "trades.csv", trades)
    write_status(d / "STATUS.md", state, d / "trades.csv")
    print(f"SPY last completed bar {dates[-1]}; processed {len(daily)} day(s) (previous {before}); "
          f"equity=${state['equity']:,.2f} bench=${state['bench']:,.2f} position_next={'SPY' if state['w'] else 'T-bills'}; trades={len(trades)}")


if __name__ == "__main__":
    main()
