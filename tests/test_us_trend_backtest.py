import csv
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import us_trend_backtest as ub  # noqa: E402


def _bdays(n, start="1993-01-29"):
    d = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(2 * n, "D"), dtype="datetime64[D]")
    return np.array([str(x) for x in d[np.is_busday(d)][:n]])


def _series(n, seed=0):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0.0004, 0.012, n))


def test_weights_never_use_future_data():
    n = 3000; dates = _bdays(n); px = _series(n); rf = np.full(n, 0.0001)
    month = np.array([d[:7] for d in dates])
    cut = 2000
    last_full_month_end = int(np.nonzero(month[:cut] != month[cut - 1])[0][-1]) + 1  # rows before the cut's partial month
    for kind, p in ub.VARIANTS:
        full = ub.weights(kind, p, dates, px, rf)
        part = ub.weights(kind, p, dates[:cut], px[:cut], rf[:cut])
        upto = cut if kind in ("sma", "dual") else last_full_month_end
        assert np.array_equal(full[:upto], part[:upto]), (kind, p)


def test_cash_earns_tbills_and_costs_on_switch():
    px = np.array([100, 110, 110, 99], dtype=float); rf = np.array([0, 0.001, 0.001, 0.001])
    w = np.array([1, 0, 1, 1], dtype=float)
    r, held = ub.run(w, px, rf, cost=0.01)
    assert np.allclose(r, [0, 0.10 - 0.01, 0.001 - 0.01, 99 / 110 - 1 - 0.01])


def test_irx_is_lagged_and_forward_filled(tmp_path):
    with open(tmp_path / "SPY.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "close", "adj_close", "source"])
        for d, p in (("2020-01-02", 100), ("2020-01-03", 101), ("2020-01-06", 102)):
            w.writerow([d, p, p, "t"])
    with open(tmp_path / "_IRX.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "close", "adj_close", "source"])
        for d, y in (("2020-01-02", 2.52), ("2020-01-06", 5.04)):
            w.writerow([d, y, y, "t"])
    dates, px, rf = ub.load(tmp_path)
    assert np.allclose(rf, [0, 2.52 / 100 / 252, 2.52 / 100 / 252])  # 01-03 has no quote: carry 01-02


def test_cli_hides_holdout_by_default(tmp_path):
    n = 8600; dates = _bdays(n); px = _series(n, 2)
    for name, vals in (("SPY.csv", px), ("_IRX.csv", np.full(n, 3.0))):
        with open(tmp_path / name, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "close", "adj_close", "source"])
            for i in range(n):
                w.writerow([dates[i], vals[i], vals[i], "synthetic"])
    tool = Path(__file__).resolve().parents[1] / "tools" / "us_trend_backtest.py"
    res = subprocess.run([sys.executable, str(tool), "--data", str(tmp_path), "--out", str(tmp_path / "r.json")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "variants tried=12" in res.stdout and "HOLDOUT" not in res.stdout
