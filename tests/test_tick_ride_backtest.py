import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import tick_ride_backtest as tr  # noqa: E402


def _grid(n=6000, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    mid = 1000 * np.cumprod(1 + rng.normal(drift, 0.0005, n))
    g = dict(mid=mid.astype(np.float32), ask_fill=(mid * 1.0005).astype(np.float32), bid_fill=(mid * 0.9995).astype(np.float32),
             spread_bp=np.full(n, 5.0, dtype=np.float32), turn10=np.abs(rng.normal(1e6, 1e5, n)).astype(np.float32),
             burst=np.abs(rng.normal(2, 2, n)).astype(np.float32), flow10=rng.uniform(-1, 1, n).astype(np.float32),
             flow30=rng.uniform(-1, 1, n).astype(np.float32), imb=rng.uniform(-1, 1, n).astype(np.float32))
    r10 = np.full(n, np.nan); r10[10:] = mid[10:] / mid[:-10] - 1
    g["ret10"] = r10.astype(np.float32)
    return g


def _cut(g, n):
    return {k: v[:n] for k, v in g.items() if isinstance(v, np.ndarray)}


def test_rolling_max_prior_matches_naive():
    x = np.array([1, 5, 2, np.nan, 3, 9, 0, 4], dtype=float)
    got = tr.rolling_max_prior(x, 3)
    want = [np.nan, np.nan, np.nan, 5, 5, 3, 9, 9]
    assert np.allclose(got, want, equal_nan=True)


def test_entry_masks_never_look_ahead():
    btc, alt = _grid(seed=1), _grid(seed=2)
    for kind in tr.ENTRIES:
        full = tr.entry_mask(kind, dict(alt), btc)
        part = tr.entry_mask(kind, _cut(alt, 4000), _cut(btc, 4000))
        assert np.array_equal(full[:4000], part), kind


def test_trailing_exit_rides_then_exits_on_pullback():
    n = 3000
    mid = np.concatenate([np.linspace(100, 110, 1500), np.linspace(110, 100, 1500)])
    g = dict(mid=mid, ask_fill=mid * 1.0005, bid_fill=mid * 0.9995, flow30=np.zeros(n), vol1m=np.full(n, 0.001))
    ent = np.zeros(n, dtype=bool); ent[10] = True
    out = tr.simulate(g, ent, "Y1", dict(k=3))
    assert len(out) == 1
    r, hold = out[0]
    assert r > 0.06  # rode most of the +10% move
    # pullback of 0.3% from ~110 happens ~45 s after the peak at second 1499 (slope ~0.0067/s)
    assert 1490 < 12 + hold < 1560


def test_flow_reversal_exit_and_hard_stop():
    n = 2000
    mid = np.full(n, 100.0); mid[500:] = 98.5  # drop beyond -1% at second 500
    g = dict(mid=mid, ask_fill=mid * 1.0005, bid_fill=mid * 0.9995, flow30=np.zeros(n))
    ent = np.zeros(n, dtype=bool); ent[5] = True
    (r, hold), = tr.simulate(g, ent, "Y2", {})
    assert r < -0.01 and 7 + hold <= 502


def test_fees_applied_in_summary():
    s = tr.summarize({"d1": [(0.01, 60), (-0.004, 120)]}, fee=0.0005)
    assert s["n"] == 2 and np.isclose(s["mean_bp"], ((0.01 - 0.001) + (-0.004 - 0.001)) / 2 * 1e4)
