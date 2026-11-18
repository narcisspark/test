import csv
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import stock_etf_backtest as sb  # noqa: E402


def _bdays(n, start="2015-10-01"):
    d = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(2 * n, "D"), dtype="datetime64[D]")
    d = d[np.is_busday(d)][:n]
    return np.array([str(x) for x in d])


def _px(n, seed=0):
    rng = np.random.default_rng(seed)
    eq1 = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    eq2 = 100 * np.cumprod(1 + rng.normal(0.0002, 0.018, n))
    bond = 100 * np.cumprod(1 + rng.normal(0.0001, 0.001, n))
    return np.column_stack([eq1, eq2, bond])


def test_weights_never_use_future_data():
    n = 1500; dates, px = _bdays(n), _px(n)
    for kind, p in sb.VARIANTS:
        full = sb.weights(kind, p, dates, px)
        cut = sb.weights(kind, p, dates[:1000], px[:1000])
        # dual momentum decides on month ends; the last day of the cut may be an artificial month end
        upto = 1000 if kind != "dualmom" else int(np.nonzero(np.array([d[:7] for d in dates[:1000]]) != dates[999][:7])[0][-1]) + 1
        assert np.array_equal(full[:upto], cut[:upto]), (kind, p)


def test_returns_applied_next_day_and_costs_on_turnover():
    px = np.array([[100, 100, 100], [110, 100, 100], [121, 100, 100], [121, 100, 100]], dtype=float)
    w = np.array([[1, 0, 0], [1, 0, 0], [0, 0, 1], [0, 0, 1]], dtype=float)
    r, held = sb.run(w, px, cost=0.01)
    # day1: enter EQ1 (cost 1%) and earn +10%; day2: +10%; day3: switch EQ1->BOND costs 2 sides
    assert np.allclose(r, [0, 0.10 - 0.01, 0.10, 0.0 - 0.02])


def test_rsi2_and_trade_accounting():
    x = np.array([10, 9, 8, 9, 10, 11], dtype=float)
    r = sb.rsi2(x)
    assert np.isnan(r[0]) and r[2] == 0.0 and r[4] == 100.0
    held = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 0], [0, 0, 0]], dtype=float)
    rets = np.array([0.0, 0.05, -0.02, 0.0])
    tr = sb.trades(rets, held)
    assert len(tr) == 1 and np.isclose(tr[0][1], 1.05 * 0.98 - 1)


def test_cli_hides_holdout_by_default(tmp_path):
    n = 2900; dates, px = _bdays(n), _px(n, 3)
    for j, code in enumerate(sb.CODES):
        with open(tmp_path / f"{code}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "close", "adj_close", "volume", "source"])
            for i in range(n):
                w.writerow([dates[i], px[i, j], px[i, j], 1, "synthetic"])
    tool = Path(__file__).resolve().parents[1] / "tools" / "stock_etf_backtest.py"
    res = subprocess.run([sys.executable, str(tool), "--data", str(tmp_path), "--out", str(tmp_path / "r.json")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "variants tried=17" in res.stdout and "HOLDOUT" not in res.stdout and "2023" not in res.stdout.split("variants tried")[0]
