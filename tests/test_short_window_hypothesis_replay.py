from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from trading_paper.simulation.models import Bar
from trading_paper.simulation.short_window_hypothesis_replay import (
    _complete_sessions,
    _eligible,
    _split_sessions,
)


class ShortWindowHypothesisReplayTests(unittest.TestCase):
    def test_six_sessions_split_chronologically_into_three_one_two(self):
        start = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        days = []
        for day in range(6):
            timestamp = start + timedelta(days=day)
            days.append([Bar(timestamp, "KRW-BTC", "KRW", 100, 101, 99, 100, 1)])

        windows = _split_sessions(days)

        self.assertEqual([len(windows[name]) for name in ("train", "validation", "holdout")], [3, 1, 2])
        self.assertLess(windows["train"][-1].timestamp, windows["validation"][0].timestamp)
        self.assertLess(windows["validation"][-1].timestamp, windows["holdout"][0].timestamp)

    def test_positive_returns_cannot_promote_with_too_few_holdout_trades(self):
        good = {"closed_trades": 25, "net_return_pct": 1.0, "max_drawdown_pct": 1.0}
        baseline = {"closed_trades": 25, "net_return_pct": -1.0, "max_drawdown_pct": 2.0}
        too_few = {"closed_trades": 19, "net_return_pct": 0.8, "max_drawdown_pct": 1.0}

        eligible = _eligible(
            selected_name="variant",
            selected_validation=good,
            baseline_validation=baseline,
            selected_holdout=too_few,
            baseline_holdout=baseline,
            selected_stress=good,
            baseline_stress=baseline,
        )

        self.assertFalse(eligible)

    def test_multi_market_session_is_kept_and_sorted_chronologically(self):
        start = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        bars = []
        for minute in range(1_440):
            stamp = start + timedelta(minutes=minute)
            for symbol in ("KRW-BTC", "KRW-ETH"):
                bars.append(Bar(stamp, symbol, "KRW", 100, 101, 99, 100, 1))

        sessions = _complete_sessions(bars)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]), 2_880)
        self.assertEqual(sessions[0], sorted(sessions[0], key=lambda bar: (bar.timestamp, bar.symbol)))
