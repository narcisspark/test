#!/usr/bin/env python3.11
"""Live paper tracking of the SMA(20) daily trend filter that passed docs/daily-trend-research.md.

Run once a day after the Upbit daily candle closes (00:00 UTC = 09:00 KST). Only COMPLETED candles are used:
the candle whose UTC date is today is still forming and is dropped. The run is idempotent: dates already
processed are skipped, missed days are caught up in order. Same accounting as tools/daily_trend.py: the signal
from the close of day d is held over day d+1, costs are charged on position changes, equal weight across coins
with >= MIN_HISTORY days of history. Virtual money only; no orders are sent anywhere.

Usage: python3.11 tools/paper_daily_sma.py --state paper/sma20
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

N = 20
COST = 0.0015
MIN_HISTORY = 250
INITIAL_KRW = 10_000_000.0
PAGES = 2  # 2 x 200 daily candles: enough for the 250-day eligibility rule and SMA(20)


def fetch_histories(markets: list[str]) -> dict[str, list[tuple[str, float]]]:
    import time

    from download_daily import fetch

    out = {}
    for m in markets:
        rows: dict[str, float] = {}
        to = None
        for _ in range(PAGES):
            batch = fetch(m, to)
            if not batch:
                break
            for c in batch:
                rows[c["candle_date_time_utc"][:10]] = float(c["trade_price"])
            to = min(c["candle_date_time_utc"] for c in batch).replace("T", " ")
            time.sleep(0.15)
        out[m] = sorted(rows.items())
    return out


def _signal(closes: list[float]) -> float:
    """1.0 if the last close is above the mean of the last N closes (SMA includes today), as in daily_trend."""
    if len(closes) < N:
        return 0.0
    return 1.0 if closes[-1] > float(np.mean(closes[-N:])) else 0.0


def _eligible(hist: list[tuple[str, float]], i: int) -> bool:
    return i >= MIN_HISTORY  # same as daily_trend: index in the coin's own history


def update(state: dict | None, histories: dict[str, list[tuple[str, float]]], today: str) -> tuple[dict, list[dict], list[dict]]:
    """Advance the paper portfolio through every completed day not yet processed.
    Returns (new_state, new_daily_rows, new_trade_rows)."""
    hist = {m: [(d, c) for d, c in rows if d < today] for m, rows in histories.items()}  # drop forming candle
    idx = {m: {d: i for i, (d, _) in enumerate(rows)} for m, rows in hist.items()}
    all_dates = sorted({d for rows in hist.values() for d, _ in rows})
    if not all_dates:
        raise SystemExit("no completed candles")
    latest = all_dates[-1]
    daily_rows, trade_rows = [], []

    def signals_at(d: str) -> dict[str, float]:
        out = {}
        for m, rows in hist.items():
            i = idx[m].get(d)
            out[m] = _signal([c for _, c in rows[: i + 1]]) if i is not None and _eligible(rows, i) else 0.0
        return out

    if state is None:
        pos = signals_at(latest)
        state = dict(strategy=f"SMA({N}) daily trend filter", cost_per_side=COST, start_date=latest, last_date=latest,
                     equity=INITIAL_KRW, bench_equity=INITIAL_KRW, peak=INITIAL_KRW, mdd=0.0, bench_peak=INITIAL_KRW,
                     bench_mdd=0.0, pos=pos, prev_pos={m: 0.0 for m in hist}, bench_in={m: False for m in hist})
        for m, p in pos.items():
            if p:
                trade_rows.append(dict(date=latest, market=m, action="BUY", close=hist[m][idx[m][latest]][1]))
        if any(pos.values()):
            state["first_entry"] = dict(date=latest, equity=INITIAL_KRW, bench=INITIAL_KRW)
        daily_rows.append(dict(date=latest, equity=INITIAL_KRW, ret=0.0, bench_equity=INITIAL_KRW, bench_ret=0.0,
                               held=sum(1 for p in pos.values() if p), note="start (signal at this close, exposure from next day)"))
        return state, daily_rows, trade_rows

    for d in [x for x in all_dates if x > state["last_date"]]:
        prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        rets, bench = [], []
        for m, rows in hist.items():
            i, j = idx[m].get(d), idx[m].get(prev)
            if i is None or j is None or not _eligible(rows, j):
                continue  # not live yet (or a missing candle): this coin's slot is skipped today
            px = rows[i][1] / rows[j][1] - 1.0
            held, prev_held = state["pos"].get(m, 0.0), state["prev_pos"].get(m, 0.0)
            rets.append(held * px - COST * abs(held - prev_held))
            entry = 0.0 if state["bench_in"].get(m) else COST
            state["bench_in"][m] = True
            bench.append(px - entry)
        r = float(np.mean(rets)) if rets else 0.0
        b = float(np.mean(bench)) if bench else 0.0
        state["equity"] *= 1 + r
        state["bench_equity"] *= 1 + b
        for k, pk, dd in (("equity", "peak", "mdd"), ("bench_equity", "bench_peak", "bench_mdd")):
            state[pk] = max(state[pk], state[k]); state[dd] = min(state[dd], state[k] / state[pk] - 1)
        new = signals_at(d)
        if any(new.values()) and not state.get("first_entry"):
            # first time the trend filter turns on: from here we compare how much of the up-leg it captures
            state["first_entry"] = dict(date=d, equity=state["equity"], bench=state["bench_equity"])
        state["invested_days"] = state.get("invested_days", 0) + (1 if any(state["pos"].values()) else 0)
        for m in hist:
            if new[m] != state["pos"].get(m, 0.0):
                trade_rows.append(dict(date=d, market=m, action="BUY" if new[m] else "SELL", close=hist[m][idx[m][d]][1]))
        state["prev_pos"], state["pos"], state["last_date"] = state["pos"], new, d
        daily_rows.append(dict(date=d, equity=round(state["equity"], 2), ret=round(r, 6), bench_equity=round(state["bench_equity"], 2),
                               bench_ret=round(b, 6), held=sum(1 for p in new.values() if p), note=""))
    return state, daily_rows, trade_rows


def _append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


def _since_entry_lines(state: dict) -> list[str]:
    fe = state.get("first_entry")
    if not fe:
        return ["## 첫 진입 이후 비교", "", "- 아직 진입 신호 없음 (모든 코인이 20일 평균 아래). 첫 매수부터 상승 국면 추적을 시작한다.", ""]
    s_ret = state["equity"] / fe["equity"] - 1
    b_ret = state["bench_equity"] / fe["bench"] - 1
    return ["## 첫 진입 이후 비교", "",
            f"- 첫 매수 신호: {fe['date']} 종가 · 그 뒤 보유한 날: {state.get('invested_days', 0)}일",
            f"- 첫 진입 이후 전략 {s_ret:+.2%} vs 매수 후 보유 {b_ret:+.2%} (차이 {s_ret - b_ret:+.2%}p)", ""]


def write_status(path: Path, state: dict, trades_path: Path) -> None:
    eq, be = state["equity"], state["bench_equity"]
    held = [m for m, p in state["pos"].items() if p]
    recent = []
    if trades_path.exists():
        with open(trades_path) as f:
            recent = list(csv.DictReader(f))[-10:]
    lines = [
        f"# 모의 운영: {state['strategy']}",
        "",
        "가상 자금만 사용한다. 실제 주문은 없다. 사전 등록 연구: [daily-trend-research.md](../../docs/daily-trend-research.md).",
        "",
        f"- 시작: {state['start_date']} 종가 신호 (초기 가상자금 {INITIAL_KRW:,.0f}원)",
        f"- 마지막 반영 일봉: {state['last_date']} (UTC 일봉, KST 다음 날 09:00 마감)",
        f"- 전략 평가액: {eq:,.0f}원 ({eq / INITIAL_KRW - 1:+.2%}), 최대낙폭 {state['mdd']:.2%}",
        f"- 매수 후 보유 기준선: {be:,.0f}원 ({be / INITIAL_KRW - 1:+.2%}), 최대낙폭 {state['bench_mdd']:.2%}",
        f"- 다음 날 보유 코인 ({len(held)}/{len(state['pos'])}): {', '.join(m[4:] for m in held) or '없음 (전부 현금)'}",
        f"- 비용 가정: 편도 {state['cost_per_side']:.2%}",
        "",
        *_since_entry_lines(state),
        "## 최근 거래 (종가 기준 가상 체결)",
        "",
        "| 날짜 | 코인 | 동작 | 종가 |",
        "|---|---|---|---:|",
        *[f"| {t['date']} | {t['market']} | {t['action']} | {float(t['close']):,.4g} |" for t in recent],
        "",
        "일별 기록: [daily.csv](daily.csv) · 거래: [trades.csv](trades.csv) · 상태: [state.json](state.json)",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="paper/sma20")
    ap.add_argument("--today", default=None, help="UTC date override (tests); default = now")
    a = ap.parse_args()
    from download_daily import UNIVERSE

    d = Path(a.state); d.mkdir(parents=True, exist_ok=True)
    sp = d / "state.json"
    state = json.loads(sp.read_text()) if sp.exists() else None
    today = a.today or datetime.now(timezone.utc).date().isoformat()
    histories = fetch_histories(UNIVERSE)
    for m, rows in histories.items():
        done = [x for x, _ in rows if x < today]
        print(f"{m}: {len(done)} completed candles, last {done[-1] if done else '-'}")
    before = state["last_date"] if state else None
    state, daily_rows, trade_rows = update(state, histories, today)
    sp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    _append_csv(d / "daily.csv", daily_rows)
    _append_csv(d / "trades.csv", trade_rows)
    write_status(d / "STATUS.md", state, d / "trades.csv")
    print(f"processed {len(daily_rows)} day(s) (previous last_date={before}, now {state['last_date']}); "
          f"equity={state['equity']:,.0f} bench={state['bench_equity']:,.0f}; trades today={len(trade_rows)}")


if __name__ == "__main__":
    main()
