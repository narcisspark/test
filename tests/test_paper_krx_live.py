import copy
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import paper_krx_live as pk  # noqa: E402
import stock_etf_backtest as sb  # noqa: E402


def _hist(end="2026-11-18", n=400, seed=0):
    d = np.arange(np.datetime64(end) - np.timedelta64(2 * n, "D"), np.datetime64(end) + np.timedelta64(1, "D"), dtype="datetime64[D]")
    d = [str(x) for x in d[np.is_busday(d)]][-n:]
    rng = np.random.default_rng(seed)
    out = {}
    for j, c in enumerate(pk.CODES):
        px = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01 + 0.005 * j, n))
        out[c] = [(d[i], float(px[i]), float(px[i] * 0.9)) for i in range(n)]  # adj = 0.9 x close
    return out


def _live(hist, day, bump=1.01):
    return {c: (day, hist[c][-1][1] * bump, "test") for c in pk.CODES}


def kst(s):
    return datetime.fromisoformat(s).replace(tzinfo=pk.KST)


def test_holiday_is_skipped_and_state_untouched():
    h = _hist()
    s0 = pk.new_state()
    s, info, marks, trades = pk.step(copy.deepcopy(s0), kst("2026-11-19 10:10"), h, _live(h, "2026-11-18"))
    assert info["status"] == "skipped_closed" and s == s0 and not marks and not trades


def test_intraday_runs_only_mark_and_duplicates_are_ignored():
    h = _hist()
    s, info, marks, trades = pk.step(None, kst("2026-11-19 10:10"), h, _live(h, "2026-11-19"))
    assert info["status"] == "marked" and not trades
    assert all(abs(pk.equity(a) - pk.INITIAL) < 1e-6 for a in s["accounts"].values())  # all cash before 1st decision
    s2, info2, m2, t2 = pk.step(copy.deepcopy(s), kst("2026-11-19 10:40"), h, _live(h, "2026-11-19"))
    assert info2["status"] == "duplicate" and s2 == s and not m2


def test_decision_matches_backtest_rule_and_charges_cost_only():
    h = _hist()
    live = _live(h, "2026-11-19", bump=1.02)
    s, info, _, trades = pk.step(None, kst("2026-11-19 15:10"), h, live)
    assert info["status"] == "decided"
    dates = np.array([d for d, _, _ in h[pk.CODES[0]]] + ["2026-11-19"])
    px = np.array([[h[c][i][2] for c in pk.CODES] for i in range(len(h[pk.CODES[0]]))]
                  + [[live[c][1] * 0.9 for c in pk.CODES]])
    for name, (kind, p) in pk.ACCOUNTS.items():
        w = sb.weights(kind, p, dates, px)[-1]
        acc = s["accounts"][name]
        assert np.allclose(acc["weights"], w), name
        assert abs(pk.equity(acc) - pk.INITIAL * (1 - pk.COST * w.sum())) < 1e-3, name
    assert any(t["account"] == "buyhold_kodex200" for t in trades)


def test_marking_follows_prices():
    h = _hist()
    s, *_ = pk.step(None, kst("2026-11-19 15:10"), h, _live(h, "2026-11-19", 1.0))
    acc = s["accounts"]["buyhold_kodex200"]; before = pk.equity(acc)
    live = {c: ("2026-11-20", s["last_prices"][i] * (1.05 if i == 0 else 1.0), "t") for i, c in enumerate(pk.CODES)}
    s2, info, *_ = pk.step(copy.deepcopy(s), kst("2026-11-20 10:10"), h, live)
    assert info["status"] == "marked"
    assert abs(pk.equity(s2["accounts"]["buyhold_kodex200"]) - (acc["values"][0] * 1.05 + acc["cash"])) < 1e-6
    assert pk.equity(s2["accounts"]["buyhold_kodex200"]) > before


def test_dual_momentum_decides_once_a_month():
    h = _hist()
    s, *_ = pk.step(None, kst("2026-11-19 15:10"), h, _live(h, "2026-11-19"))  # first decision: due
    assert s["dm_month"] == "2026-10"
    s["accounts"]["dualmom126"]["weights"] = [0.0, 0.0, 1.0]  # pretend it holds bonds
    s["accounts"]["dualmom126"]["values"] = [0.0, 0.0, pk.equity(s["accounts"]["dualmom126"])]
    s["accounts"]["dualmom126"]["cash"] = 0.0
    s2, *_ = pk.step(copy.deepcopy(s), kst("2026-11-20 15:10"), h, _live(h, "2026-11-20", 1.5))  # mid-month: not due
    assert s2["accounts"]["dualmom126"]["weights"] == [0.0, 0.0, 1.0]
    assert pk._last_weekday_of_month(datetime(2026, 11, 30).date())  # Monday Nov 30 is the last weekday
    assert not pk._last_weekday_of_month(datetime(2026, 11, 27).date())


def test_live_quote_falls_back_when_yahoo_is_stale_or_fails():
    stale = lambda c: ("2026-11-18", 100.0, "yahoo 15:30")  # noqa: E731
    fresh = lambda c: ("2026-11-19", 101.0, "naver_daily")  # noqa: E731

    def boom(c):
        raise OSError("down")

    assert pk.fetch_live("069500", "2026-11-19", (stale, fresh))[2] == "naver_daily"
    assert pk.fetch_live("069500", "2026-11-19", (boom, fresh))[1] == 101.0
    assert pk.fetch_live("069500", "2026-11-19", (stale, stale))[0] == "2026-11-18"  # holiday: stays stale
