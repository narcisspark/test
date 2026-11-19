import copy
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import paper_us_ma12 as pu  # noqa: E402
import us_trend_backtest as ub  # noqa: E402


def _data(n=1500, seed=0, start="2020-01-01"):
    d = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(2 * n, "D"), dtype="datetime64[D]")
    dates = [str(x) for x in d[np.is_busday(d)][:n]]
    rng = np.random.default_rng(seed)
    px = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    rf = np.full(n, 0.0001); rf[0] = 0.0
    return dates, px, rf


def test_daily_returns_match_backtest_after_start():
    dates, px, rf = _data()
    start = 800
    state, _, _ = pu.update(None, dates[:start + 1], px[:start + 1], rf[:start + 1])
    live = {}
    for k in range(start + 2, len(dates) + 1):
        state, rows, _ = pu.update(state, dates[:k], px[:k], rf[:k])
        assert len(rows) == 1
        live[rows[0]["date"]] = rows[0]["ret"]
    w = ub.weights("msma", dict(m=12), np.array(dates), px, rf)
    r, _ = ub.run(w, px, rf, ub.COST)
    bt = dict(zip(dates, r))
    # identical except the first day, which pays the entry cost of adopting the current state
    checked = sum(1 for d, x in list(live.items())[1:] if abs(x - bt[d]) < 1e-6)
    assert checked == len(live) - 1


def test_position_changes_only_at_month_ends_and_rerun_is_idempotent():
    dates, px, rf = _data(seed=1)
    s, _, _ = pu.update(None, dates[:900], px[:900], rf[:900])
    s, rows, trades = pu.update(s, dates[:1100], px[:1100], rf[:1100])
    month_end = {r["date"] for r in rows if r["month_end"]}
    assert all(t["date"] in month_end for t in trades)
    s2, rows2, trades2 = pu.update(copy.deepcopy(s), dates[:1100], px[:1100], rf[:1100])
    assert rows2 == [] and trades2 == [] and s2 == s


def test_month_end_flag_uses_calendar_for_last_row_and_incomplete_bar_is_dropped():
    assert pu.month_end_flags(["2026-11-25", "2026-11-27"]) == [False, False]  # Fri 27th: next weekday Mon 30th
    assert pu.month_end_flags(["2026-11-27", "2026-11-30"]) == [False, True]
    et = pu.ET
    assert pu.completed(["2026-11-18", "2026-11-19"], datetime(2026, 11, 19, 15, 0, tzinfo=et)) == 1
    assert pu.completed(["2026-11-18", "2026-11-19"], datetime(2026, 11, 19, 17, 0, tzinfo=et)) == 2
