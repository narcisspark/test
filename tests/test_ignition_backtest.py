import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ignition_backtest as ig  # noqa: E402

DAY = 86400


def _coin(days=60, seed=0, vol=0.004, start_day=20000):
    rng = np.random.default_rng(seed)
    n = days * 288
    t = start_day * DAY + np.arange(n) * ig.BAR
    c = 100 * np.cumprod(1 + rng.normal(0.00002, vol, n))
    o = np.concatenate(([100.0], c[:-1]))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    v = np.abs(rng.normal(1e8, 3e7, n)) * (1 + 6 * (rng.random(n) < 0.01))  # occasional volume spikes
    return dict(t=t, o=o, h=h, l=l, c=c, v=v)


def _cut(g, end):
    m = g["t"] < end
    return {k: x[m] for k, x in g.items()}


def test_resample_uses_only_completed_buckets():
    g = _coin(days=2)
    r = ig.resample(g, 3600)
    # bar 0..10 close before the first hour ends -> no completed hourly bucket yet
    assert (r["last"][:11] == -1).all() and r["last"][11] == 0
    assert np.isclose(r["C"][0], g["c"][11]) and np.isclose(r["H"][0], g["h"][:12].max())


def test_features_and_entries_never_look_ahead():
    btc, alt = _coin(seed=1), _coin(seed=2, vol=0.006)
    cut = 20000 * DAY + 45 * DAY
    fb, fa = ig.features(btc), ig.features(alt)
    fbc, fac = ig.features(_cut(btc, cut)), ig.features(_cut(alt, cut))
    n = int((cut - btc["t"][0]) // ig.BAR)
    for key in ("atr",):
        assert np.allclose(fa[key][:n], fac[key][:n], equal_nan=True)
    for k in ("e2", "x2"):
        assert np.array_equal(fa[k][:n], fac[k][:n])
    for r in (3, 5):
        assert np.array_equal(fa["e1"][r][:n], fac["e1"][r][:n]) and np.array_equal(fa["e4"][r][:n], fac["e4"][r][:n])
    e3 = ig.entry_mask("E3", {}, "KRW-ALT", alt, fa, (btc, fb))
    e3c = ig.entry_mask("E3", {}, "KRW-ALT", _cut(alt, cut), fac, (_cut(btc, cut), fbc))
    assert np.array_equal(e3[:n], e3c)


def test_trailing_stop_rides_trend_and_exits_on_pullback():
    n = 400
    t = 20000 * DAY + np.arange(n) * ig.BAR
    c = np.concatenate([np.linspace(100, 150, 300), np.linspace(150, 120, 100)])  # rally then pullback
    o = np.concatenate(([100.0], c[:-1])); h = c * 1.001; l = c * 0.999; v = np.full(n, 1e8)
    g = dict(t=t, o=o, h=h, l=l, c=c, v=v)
    f = dict(atr=np.full(n, 1.0), x2=np.zeros(n, dtype=bool))
    entries = np.zeros(n, dtype=bool); entries[10] = True
    tr = ig.simulate(g, f, entries, "X1", dict(k=3))
    assert len(tr) == 1
    assert tr[0]["ret"] > 0.3  # rode most of the rally (bought ~101.7, sold after a 3-ATR pullback from 150)
    exit_bar = int((tr[0]["exit_time"] - t[0]) // ig.BAR)
    assert exit_bar == 311  # close first < 150 - 3 at bar 310 (slope ~0.3/bar); filled at the next bar open


def test_cli_hides_holdout(tmp_path):
    from datetime import datetime, timezone
    for name, seed in (("KRW-BTC", 1), ("KRW-ETH", 2)):
        g = _coin(days=70, seed=seed, vol=0.006)
        with open(tmp_path / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["time_utc", "open", "high", "low", "close", "turnover_krw"])
            for i in range(len(g["t"])):
                ts = datetime.fromtimestamp(int(g["t"][i]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                w.writerow([ts, g["o"][i], g["h"][i], g["l"][i], g["c"][i], g["v"][i]])
    tool = Path(__file__).resolve().parents[1] / "tools" / "ignition_backtest.py"
    res = subprocess.run([sys.executable, str(tool), "--data", str(tmp_path), "--out", str(tmp_path / "r.json")],
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stderr
    assert "variants tried=18" in res.stdout and "HOLDOUT" not in res.stdout
    assert json.loads((tmp_path / "r.json").read_text())["variants_tried"] == 18
