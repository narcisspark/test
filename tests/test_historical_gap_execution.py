from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from trading_paper.simulation.engine import simulate
from trading_paper.simulation.models import Bar


class HistoricalGapExecutionTests(unittest.TestCase):
    def test_open_position_is_closed_at_next_observed_open_after_candle_gap(self):
        start = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        bars = [
            Bar(start, "KRW-BTC", "KRW", 10_000, 10_010, 9_990, 10_000, 1),
            Bar(start + timedelta(minutes=1), "KRW-BTC", "KRW", 9_800, 9_810, 9_790, 9_800, 1),
            Bar(start + timedelta(minutes=2), "KRW-BTC", "KRW", 9_800, 9_820, 9_790, 9_810, 2),
            Bar(start + timedelta(minutes=3), "KRW-BTC", "KRW", 9_810, 9_820, 9_805, 9_815, 1),
            # No candles for three minutes. The next observed open is below the intended stop.
            Bar(start + timedelta(minutes=7), "KRW-BTC", "KRW", 9_700, 9_740, 9_650, 9_700, 2),
        ]
        config = {
            "initial_cash_krw": 10_000_000,
            "execution": {
                "spread_bps_total": 10, "slippage_bps": 5,
                "commission_bps_per_side": 5, "sell_tax_bps": {"KRW": 0},
            },
            "risk": {
                "stop_loss_pct": 0.01, "take_profit_pct": 0.02,
                "max_holding_minutes": 30, "max_position_equity_pct": 0.2,
                "max_total_exposure_pct": 0.6, "max_positions": 3,
                "risk_per_trade_pct": 0.005, "daily_loss_stop_pct": 0.02,
                "max_entries_per_day": 10, "max_consecutive_stops": 3,
            },
            "feed": {"pending_signal_max_seconds": 120},
            "strategies": {
                "A": {"ema_fast": 5, "ema_slow": 20, "breakout_bars": 6,
                      "volume_lookback": 20, "volume_multiple": 1.5},
                "B": {"deviation_below_vwap_pct": 0.004,
                      "require_close_above_previous": True},
            },
        }

        report = simulate(bars, config, continuous=True, strategy_ids=("B",), liquidate_at_end=True)

        trades = report["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "DATA_GAP")
        self.assertEqual(trades[0]["exit_time"], bars[-1].timestamp.isoformat())
