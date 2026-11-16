import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trading_paper.simulation.engine import _pending_signal_is_fresh
from trading_paper.simulation.models import Bar, Signal


KST = ZoneInfo("Asia/Seoul")


class PendingSignalFreshnessTest(unittest.TestCase):
    def test_only_later_bars_within_ttl_can_fill(self):
        start = datetime(2026, 11, 4, 10, 0, tzinfo=KST)
        signal = Signal("A", "KRW-BTC", start, "test")

        def bar(seconds):
            return Bar(start + timedelta(seconds=seconds), "KRW-BTC", "KRW", 1, 1, 1, 1, 1)

        self.assertFalse(_pending_signal_is_fresh(signal, bar(0), 120))
        self.assertTrue(_pending_signal_is_fresh(signal, bar(60), 120))
        self.assertTrue(_pending_signal_is_fresh(signal, bar(120), 120))
        self.assertFalse(_pending_signal_is_fresh(signal, bar(121), 120))

    def test_signal_does_not_carry_across_session_boundary(self):
        start = datetime(2026, 11, 4, 23, 59, tzinfo=KST)
        signal = Signal("A", "KRW-BTC", start, "test")
        next_session = Bar(start + timedelta(minutes=2), "KRW-BTC", "KRW", 1, 1, 1, 1, 1)
        self.assertFalse(_pending_signal_is_fresh(signal, next_session, 120))


if __name__ == "__main__":
    unittest.main()
