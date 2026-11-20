"""AWS Lambda: live paper trading of the Upbit volatility breakout (k=0.7, MA5 filter), every 5 minutes.

Rules: docs/vbo-live-paper.md. Public Upbit quotation API only (no credentials, no orders).
State lives in S3 (BUCKET / PREFIX env vars). The trading logic in `step` is pure and unit-tested.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA", "KRW-LINK", "KRW-ETC"]
K = 0.7
COST = 0.0010  # per side
SLEEVE = 1_250_000.0
API = "https://api.upbit.com"


# ------------------------------------------------------------------------------------------ pure logic
def setup_day(daily: list[tuple[str, float, float, float, float]], today: str) -> dict:
    """daily: ascending (date, open, high, low, close), may include today's in-progress candle.
    Returns today's target and whether the MA5 filter allows trading."""
    done = [d for d in daily if d[0] < today]
    cur = [d for d in daily if d[0] == today]
    if len(done) < 5 or not cur or done[-1][0] == today:
        return dict(target=None, eligible=False, reason="not enough data")
    prev = done[-1]
    ma5 = sum(d[4] for d in done[-5:]) / 5
    target = cur[0][1] + K * (prev[2] - prev[3])
    return dict(target=target, eligible=prev[4] > ma5, open=cur[0][1], prev_close=prev[4], ma5=ma5,
                reason="ok" if prev[4] > ma5 else "filter: prev close <= MA5")


def new_state(now: datetime) -> dict:
    return dict(started=now.isoformat(timespec="seconds"), day=None, runs=0,
                coins={m: dict(sleeve=SLEEVE, pos=None, traded_today=False, setup=None) for m in MARKETS}, trades=[])


def step(state: dict | None, now: datetime, daily: dict, prices: dict) -> tuple[dict, list[dict]]:
    """One 5-minute run. daily: market -> candles (see setup_day). prices: market -> last trade price.
    Returns (state, new_trade_rows)."""
    state = state or new_state(now)
    today = now.date().isoformat()
    new_trades = []
    if state["day"] != today:
        for m, c in state["coins"].items():
            if c["pos"] and m in prices:  # close yesterday's position at the first price of the new day
                e, x = c["pos"]["entry"], prices[m]
                r = x / e - 1 - 2 * COST
                c["sleeve"] *= 1 + r
                row = dict(day=c["pos"]["day"], market=m, entry_time=c["pos"]["time"], entry=e,
                           exit_time=now.isoformat(timespec="seconds"), exit=x, net_return=round(r, 6))
                new_trades.append(row); state["trades"].append(row); c["pos"] = None
            c["traded_today"] = False
            c["setup"] = setup_day(daily.get(m, []), today) if m in daily else dict(target=None, eligible=False, reason="no data")
        state["day"] = today
    for m, c in state["coins"].items():  # retry setups that failed only for lack of data (e.g. right after 00:00 UTC)
        if c["setup"] and c["setup"].get("target") is None and m in daily and c["setup"].get("reason") in ("not enough data", "no data"):
            c["setup"] = setup_day(daily[m], today)
    for m, c in state["coins"].items():
        s, p = c["setup"], prices.get(m)
        if p is None or not s or not s["eligible"] or c["traded_today"] or c["pos"]:
            continue
        if p >= s["target"]:
            c["pos"] = dict(entry=p, time=now.isoformat(timespec="seconds"), day=today, target=s["target"])
            c["traded_today"] = True
    state["runs"] += 1
    state["last_run"] = now.isoformat(timespec="seconds")
    state["trades"] = state["trades"][-2000:]
    return state, new_trades


def equity(state: dict, prices: dict) -> float:
    tot = 0.0
    for m, c in state["coins"].items():
        v = c["sleeve"]
        if c["pos"] and m in prices:
            v *= prices[m] / c["pos"]["entry"] * (1 - COST)  # mark-to-market incl. the exit fee
        tot += v
    return tot


def status_md(state: dict, prices: dict) -> str:
    eq = equity(state, prices)
    tr = state["trades"]
    wins = [t for t in tr if t["net_return"] > 0]
    mean_bp = sum(t["net_return"] for t in tr) / len(tr) * 1e4 if tr else 0.0
    lines = ["# 변동성 돌파 실시간 모의 운영 (관찰용)", "",
             "가상 자금만 사용. 실제 주문 없음. 규칙: docs/vbo-live-paper.md (k=0.7, MA5 필터, 5분 간격 확인).", "",
             f"- 시작: {state['started']} UTC · 마지막 실행: {state.get('last_run')} UTC · 실행 횟수: {state['runs']}",
             f"- 평가액: {eq:,.0f}원 ({eq / (SLEEVE * len(MARKETS)) - 1:+.2%})",
             f"- 청산 거래: {len(tr)}건 · 승률 {len(wins) / len(tr):.0%} · 거래당 평균 {mean_bp:+.1f}bp" if tr else "- 청산 거래: 0건", "",
             f"## 오늘({state['day']} UTC) 종목별 상태", "", "| 종목 | 목표가 | 현재가 | 매수 가능 | 보유 |", "|---|---:|---:|---|---|"]
    for m, c in state["coins"].items():
        s = c["setup"] or {}
        tgt = f"{s['target']:,.4g}" if s.get("target") else "-"
        pos = f"{c['pos']['entry']:,.4g}에 매수" if c["pos"] else ("오늘 거래함" if c["traded_today"] else "-")
        lines.append(f"| {m} | {tgt} | {prices.get(m, 0):,.4g} | {'예' if s.get('eligible') else '아니오 (' + s.get('reason', '') + ')'} | {pos} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------ io
def _get(path: str, params: dict) -> list:
    req = urllib.request.Request(f"{API}{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Accept": "application/json", "User-Agent": "trading-paper-vbo/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_daily(market: str) -> list[tuple[str, float, float, float, float]]:
    rows = _get("/v1/candles/days", {"market": market, "count": "8"})
    return sorted((r["candle_date_time_utc"][:10], float(r["opening_price"]), float(r["high_price"]),
                   float(r["low_price"]), float(r["trade_price"])) for r in rows)


def fetch_prices() -> dict:
    return {r["market"]: float(r["trade_price"]) for r in _get("/v1/ticker", {"markets": ",".join(MARKETS)})}


def handler(event=None, context=None):
    import boto3

    s3 = boto3.client("s3")
    bucket, prefix = os.environ["BUCKET"], os.environ.get("PREFIX", "paper/vbo/")
    try:
        state = json.loads(s3.get_object(Bucket=bucket, Key=prefix + "state.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        state = None
    now = datetime.now(timezone.utc)
    prices = fetch_prices()
    need_daily = state is None or state["day"] != now.date().isoformat() or any(
        (c["setup"] or {}).get("target") is None and (c["setup"] or {}).get("reason") in ("not enough data", "no data")
        for c in state["coins"].values())
    daily = {m: fetch_daily(m) for m in MARKETS} if need_daily else {}
    state, new_trades = step(state, now, daily, prices)
    s3.put_object(Bucket=bucket, Key=prefix + "state.json", Body=json.dumps(state, ensure_ascii=False).encode())
    s3.put_object(Bucket=bucket, Key=prefix + "STATUS.md", Body=status_md(state, prices).encode(), ContentType="text/markdown; charset=utf-8")
    if new_trades or need_daily:
        buf = io.StringIO()
        if state["trades"]:
            w = csv.DictWriter(buf, fieldnames=list(state["trades"][0])); w.writeheader(); w.writerows(state["trades"])
        s3.put_object(Bucket=bucket, Key=prefix + "trades.csv", Body=buf.getvalue().encode(), ContentType="text/csv")
    out = dict(day=state["day"], runs=state["runs"], new_trades=len(new_trades),
               holding=[m for m, c in state["coins"].items() if c["pos"]], equity=round(equity(state, prices)))
    print(json.dumps(out))
    return out
