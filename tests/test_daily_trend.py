import csv
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import daily_trend as dt  # noqa: E402


def _coin(close, start="2018-01-01"):
    close = np.asarray(close, dtype=float)
    dates = np.datetime64(start) + np.arange(len(close))
    return dict(date=np.array([str(d) for d in dates]), high=close * 1.001, low=close * 0.999, close=close)


def test_signal_applies_to_next_day_only():
    close = np.ones(10) * 100.0
    close[5] = 200.0  # jump on day 5
    pos = np.zeros(10); pos[5] = 1.0  # a rule that "sees" the jump turns on at close of day 5
    r, _ = dt.coin_returns(pos, close, cost=0.0)
    assert r[5] == 0.0  # the jump itself is not captured
    assert np.isclose(r[6], 100.0 / 200.0 - 1.0)  # exposure starts the following day


def test_cost_charged_on_every_position_change():
    close = np.ones(8) * 100.0
    pos = np.array([0, 1, 1, 0, 1, 0, 0, 0], dtype=float)
    r, trades = dt.coin_returns(pos, close, cost=0.01)
    # changes after close of days 1,3,4,5 -> charged on days 2,4,5,6
    assert np.allclose(r, [0, 0, -0.01, 0, -0.01, -0.01, -0.01, 0])
    assert len(trades) == 2 and all(np.isclose(t, 0.99 ** 2 - 1) for t in trades)


def test_sma_and_tsmom_use_only_past_data():
    rng = np.random.default_rng(0)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.03, 400))
    c = _coin(close)
    for kind, p in [("sma", dict(n=20)), ("tsmom", dict(l=30)), ("dual_sma", dict(f=10, s=50)), ("donchian", dict(n=20, m=10))]:
        full = dt.position(kind, p, c)
        cut = dt.position(kind, p, {k: v[:300] for k, v in c.items()})
        assert np.array_equal(full[:300], cut), kind  # appending future data never changes past positions


def test_donchian_enters_on_breakout_and_exits_on_breakdown():
    close = np.array([10, 10, 10, 12, 13, 13, 9, 9], dtype=float)
    c = dict(date=np.arange(8).astype(str), high=close.copy(), low=close.copy(), close=close)
    pos = dt.position("donchian", dict(n=3, m=2), c)
    assert list(pos) == [0, 0, 0, 1, 1, 1, 0, 0]


def test_buy_and_hold_baseline_and_metrics():
    g = 0.001
    close = 100 * (1 + g) ** np.arange(1000)
    coins = {"KRW-AAA": _coin(close)}
    dates, daily, expo, den, trades = dt.portfolio(coins, None, None, cost=0.0)
    m = dt.metrics(dates, daily, expo, den, trades, None, None)
    assert np.isclose(m["cagr"], (1 + g) ** 365 - 1, rtol=1e-6)
    assert m["mdd"] == 0.0 and m["days"] == 1000 - dt.MIN_HISTORY - 1  # returns start the day after eligibility


def test_cli_end_to_end_on_synthetic_csv(tmp_path):
    rng = np.random.default_rng(1)
    for name, start in (("KRW-AAA", "2017-10-01"), ("KRW-BBB", "2019-06-01")):
        n = (np.datetime64("2026-11-17") - np.datetime64(start)).astype(int)
        close = 1000 * np.cumprod(1 + rng.normal(0.0005, 0.04, n))
        c = _coin(close, start)
        with open(tmp_path / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date_utc", "open", "high", "low", "close", "volume", "turnover_krw"])
            for i in range(n):
                w.writerow([c["date"][i], close[i], c["high"][i], c["low"][i], close[i], 1, 1])
    out = tmp_path / "report.json"
    tool = Path(__file__).resolve().parents[1] / "tools" / "daily_trend.py"
    res = subprocess.run([sys.executable, str(tool), "--data", str(tmp_path), "--out", str(out)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert out.exists() and "variants tried=14" in res.stdout
    assert "HOLDOUT" not in res.stdout  # holdout stays closed by default
