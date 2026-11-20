import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import intraday_backtest as ib  # noqa: E402
import vbo_live_lambda as vl  # noqa: E402

M = "KRW-BTC"


def _daily(prev_close=105.0, today="2026-11-20"):
    # five completed days with closes 100..104 (MA5 = 102), previous day high 110 / low 100
    rows = [(f"2026-11-{15 + i:02d}", 100.0, 101.0, 99.0, 100.0 + i) for i in range(4)]
    rows.append(("2026-11-19", 104.0, 110.0, 100.0, prev_close))
    rows.append((today, 106.0, 106.0, 106.0, 106.0))  # today's in-progress candle: open 106
    return rows


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_setup_target_and_ma5_filter():
    s = vl.setup_day(_daily(), "2026-11-20")
    # target = today's open 106 + 0.7 x prev range (110 - 100); prev close 105 > MA5 of [100, 101, 102, 103, 105] = 102.2
    assert np.isclose(s["target"], 106 + 0.7 * 10) and s["eligible"]
    blocked = vl.setup_day(_daily(prev_close=101.0), "2026-11-20")
    assert not blocked["eligible"] and blocked["reason"].startswith("filter")
    assert vl.setup_day(_daily()[:3], "2026-11-20")["reason"] == "not enough data"


def test_entry_once_per_day_and_exit_next_day_with_costs():
    daily = {M: _daily()}
    s, _ = vl.step(None, at("2026-11-20 00:02"), daily, {M: 107.0})
    assert s["coins"][M]["pos"] is None  # below target 113
    s, _ = vl.step(s, at("2026-11-20 03:00"), {}, {M: 114.0})
    assert s["coins"][M]["pos"]["entry"] == 114.0  # bought at the observed price, not the target
    s, _ = vl.step(s, at("2026-11-20 04:00"), {}, {M: 120.0})
    assert s["coins"][M]["pos"]["entry"] == 114.0  # no second entry
    nd = {M: _daily(today="2026-11-21")}
    s, trades = vl.step(s, at("2026-11-21 00:01"), nd, {M: 118.0})
    assert len(trades) == 1 and np.isclose(trades[0]["net_return"], 118 / 114 - 1 - 2 * vl.COST)
    assert np.isclose(s["coins"][M]["sleeve"], vl.SLEEVE * (1 + trades[0]["net_return"]))
    # the new day's target is again 113 and the first price (118) is already above it: re-entry at 118 is the rule
    assert s["coins"][M]["traded_today"] is True and s["coins"][M]["pos"]["entry"] == 118.0
    assert s["coins"][M]["pos"]["day"] == "2026-11-21"


def test_filter_blocks_entries_and_missing_data_is_retried():
    s, _ = vl.step(None, at("2026-11-20 00:01"), {M: _daily(prev_close=101.0)}, {M: 999.0})
    assert s["coins"][M]["pos"] is None
    s2, _ = vl.step(None, at("2026-11-20 00:00"), {M: _daily()[:-1]}, {M: 999.0})  # today's candle not there yet
    assert s2["coins"][M]["setup"]["reason"] == "not enough data"
    s2, _ = vl.step(s2, at("2026-11-20 00:05"), {M: _daily()}, {M: 114.0})
    assert s2["coins"][M]["pos"] is not None


def test_target_matches_backtest_definition():
    # build two days of 5-minute bars; the backtest's target uses day-open + k * (prev high - prev low)
    t = np.array([0, 300, 86400, 86700]) + 20000 * 86400
    o = np.array([100, 104, 106, 107.0]); h = np.array([110, 105, 106, 120.0]); l = np.array([100, 103, 105, 106.0]); c = np.array([104, 104, 106, 118.0])
    tr = ib.trades_for("vbo", dict(k=0.7, filt="none"), dict(t=t, o=o, h=h, l=l, c=c))
    daily = [("2024-10-04", 100, 110, 100, 104), ("2024-10-05", 106, 120, 105, 118)]
    target = daily[1][1] + 0.7 * (daily[0][2] - daily[0][3])
    assert np.isclose(target, 113.0) and np.isclose(tr[0][1], 118 / 113 - 1)


def test_status_renders():
    s, _ = vl.step(None, at("2026-11-20 03:00"), {M: _daily()}, {M: 114.0})
    md = vl.status_md(s, {M: 114.0})
    assert "변동성 돌파" in md and "KRW-BTC" in md
