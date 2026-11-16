from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from trading_paper.simulation.models import Bar
from trading_paper.simulation.ohlcv_pullback_replay import _simulate_window


KST = ZoneInfo("Asia/Seoul")


def make_bars():
    start = datetime(2026, 1, 1, tzinfo=KST)
    bars = []
    for index in range(69):
        price = 10_000.0
        bars.append(Bar(
            timestamp=start + timedelta(minutes=index),
            symbol="KRW-BTC", market="KRW", open=price, high=price + 5,
            low=price - 5, close=price, volume=10.0,
        ))
    bars[64] = Bar(bars[64].timestamp, "KRW-BTC", "KRW", 10_080, 10_100, 10_080, 10_090, 10)
    bars[65] = Bar(bars[65].timestamp, "KRW-BTC", "KRW", 10_080, 10_090, 10_050, 10_080, 10)
    bars[66] = Bar(bars[66].timestamp, "KRW-BTC", "KRW", 10_080, 10_130, 10_100, 10_120, 20)
    bars[67] = Bar(bars[67].timestamp, "KRW-BTC", "KRW", 10_120, 10_200, 10_100, 10_150, 10)
    # Both stop and target are touched; the simulator must conservatively choose the stop.
    bars[68] = Bar(bars[68].timestamp, "KRW-BTC", "KRW", 10_120, 10_400, 9_900, 10_000, 20)
    return bars


class OhlcvPullbackReplayTests(unittest.TestCase):
    def test_next_bar_entry_and_stop_first_when_stop_and_target_both_touch(self):
        bars = make_bars()
        epochs = [bar.timestamp.timestamp() for bar in bars]
        ema20 = [10_100 + index for index in range(len(bars))]
        ema60 = [10_000.0] * len(bars)
        atr = [30.0] * len(bars)

        report = _simulate_window(bars, 0, len(bars), (epochs, ema20, ema60, atr))

        self.assertEqual(report["positions_opened"], 1)
        self.assertEqual(report["closed_trades"], 1)
        trade = report["trades"][0]
        self.assertEqual(trade["entry_time"], bars[67].timestamp.isoformat())
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertLess(trade["net_pnl_krw"], 0)
        self.assertGreater(trade["total_costs_krw"], 0)
        self.assertGreaterEqual(report["max_drawdown_pct"], abs(report["net_return_pct"]))
