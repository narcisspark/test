import copy
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import daily_trend as dt  # noqa: E402
import paper_daily_sma as ps  # noqa: E402


def _hist(seed, start="2025-01-01", days=420):
    rng = np.random.default_rng(seed)
    close = 1000 * np.cumprod(1 + rng.normal(0.001, 0.04, days))
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(c)) for i, c in enumerate(close)]


def _histories():
    return {"KRW-AAA": _hist(1), "KRW-BBB": _hist(2), "KRW-CCC": _hist(3, start="2025-03-01", days=361)}


def _upto(h, today):
    return {m: [(d, c) for d, c in rows if d < today] for m, rows in h.items()}


def test_forming_candle_is_ignored():
    h = _histories()
    last = h["KRW-AAA"][-1][0]
    state, rows, _ = ps.update(None, h, today=last)  # today's candle exists in the data but is still forming
    assert state["last_date"] == h["KRW-AAA"][-2][0]


def test_rerun_same_day_is_idempotent():
    h = _histories()
    today = h["KRW-AAA"][-5][0]
    s1, _, _ = ps.update(None, _upto(h, today), today)
    s2, rows, trades = ps.update(copy.deepcopy(s1), _upto(h, today), today)
    assert rows == [] and trades == [] and s2 == s1


def test_daily_returns_match_backtest_engine():
    h = _histories()
    dates = [d for d, _ in h["KRW-AAA"]]
    start_i = 300
    state, _, _ = ps.update(None, _upto(h, dates[start_i + 1]), dates[start_i + 1])  # start at close of dates[start_i]
    live = {}
    for k in range(start_i + 2, len(dates)):
        today = dates[k]
        state, rows, _ = ps.update(state, _upto(h, today), today)
        assert len(rows) == 1
        live[rows[0]["date"]] = rows[0]["ret"]
    coins = {m: dict(date=np.array([d for d, _ in rows]), close=np.array([c for _, c in rows]),
                     high=np.array([c for _, c in rows]), low=np.array([c for _, c in rows])) for m, rows in h.items()}
    bt_dates, daily, *_ = dt.portfolio(coins, "sma", dict(n=20), ps.COST)
    bt = dict(zip(bt_dates, daily))
    # the first live day differs only by the entry cost of the initial positions (prev_pos starts at 0)
    checked = 0
    for d, r in list(live.items())[1:]:
        assert abs(r - bt[d]) < 1e-6, d
        checked += 1
    assert checked > 50


def test_missed_days_are_caught_up_in_order():
    h = _histories()
    dates = [d for d, _ in h["KRW-AAA"]]
    s, _, _ = ps.update(None, _upto(h, dates[300]), dates[300])
    s, rows, _ = ps.update(s, _upto(h, dates[305]), dates[305])
    assert [r["date"] for r in rows] == dates[300:305]  # last_date was dates[299]
