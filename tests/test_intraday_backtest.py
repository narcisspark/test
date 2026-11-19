import csv
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import intraday_backtest as ib  # noqa: E402

DAY = 86400


def _coin(days=60, seed=0, start_day=20000, vol=0.004):
    rng = np.random.default_rng(seed)
    n = days * 288
    t = start_day * DAY + np.arange(n) * ib.BAR
    c = 100 * np.cumprod(1 + rng.normal(0, vol, n))
    o = np.concatenate(([100.0], c[:-1]))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return dict(t=t, o=o, h=h, l=l, c=c)


def _cut(g, end):
    m = g["t"] < end
    return {k: v[m] for k, v in g.items()}


def test_no_lookahead_for_any_variant():
    btc, alt = _coin(seed=1, vol=0.006), _coin(seed=2, vol=0.008)
    cut = 20000 * DAY + 40 * DAY
    for kind, p in ib.VARIANTS:
        for g in (btc, alt):
            full = ib.trades_for(kind, p, g, btc)
            part = ib.trades_for(kind, p, _cut(g, cut), _cut(btc, cut))
            early = lambda tr: [x for x in tr if x[0] <= cut - 6 * 3600]  # noqa: E731
            assert early(full) == early(part), (kind, p)


def test_volatility_breakout_fill_and_exit():
    # day 0: range 10 (high 110, low 100); day 1 opens at 105, target with k=0.5 is 110
    t = np.array([0, 300, DAY, DAY + 300, DAY + 600]) + 20000 * DAY
    o = np.array([100, 105, 105, 106, 112.0]); h = np.array([110, 106, 106, 111, 113.0])
    l = np.array([100, 104, 104, 105, 111.0]); c = np.array([105, 105, 106, 110, 112.0])
    tr = ib.trades_for("vbo", dict(k=0.5, filt="none"), dict(t=t, o=o, h=h, l=l, c=c))
    assert len(tr) == 1 and np.isclose(tr[0][1], 112 / 110 - 1)  # bought at target 110, sold at day close 112


def test_orb_stop_is_conservative_when_entry_bar_also_breaks_low():
    d0 = 20000 * DAY
    t = d0 + np.arange(8) * ib.BAR  # first 30 min = 6 bars
    o = np.full(8, 100.0); c = np.full(8, 100.0)
    h = np.array([101, 101, 101, 101, 101, 101, 102, 100.0]); l = np.array([99, 99, 99, 99, 99, 99, 98, 99.0])
    tr = ib.trades_for("orb", dict(m=30), dict(t=t, o=o, h=h, l=l, c=c))
    assert len(tr) == 1 and np.isclose(tr[0][1], 99 / 101 - 1)


def test_costs_and_window():
    trades = {"A": [(10 * DAY, 0.01), (20 * DAY, -0.01)]}
    s = ib.summarize(trades, cost=0.001, lo=0, hi=15, n_coins=1)
    assert s["n"] == 1 and np.isclose(s["mean_bp"], (0.01 - 0.002) * 1e4)


def test_cli_hides_holdout(tmp_path):
    from datetime import datetime, timezone
    for name, seed in (("KRW-BTC", 1), ("KRW-ETH", 2)):
        g = _coin(days=80, seed=seed, vol=0.006)
        with open(tmp_path / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["time_utc", "open", "high", "low", "close", "turnover_krw"])
            for i in range(len(g["t"])):
                ts = datetime.fromtimestamp(int(g["t"][i]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                w.writerow([ts, g["o"][i], g["h"][i], g["l"][i], g["c"][i], 1])
    tool = Path(__file__).resolve().parents[1] / "tools" / "intraday_backtest.py"
    res = subprocess.run([sys.executable, str(tool), "--data", str(tmp_path), "--out", str(tmp_path / "r.json")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "variants tried=20" in res.stdout and "HOLDOUT" not in res.stdout
