from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo
import unittest

from trading_paper.simulation.engine import _estimated_round_trip_cost_pct, simulate
from trading_paper.simulation.models import Bar, Signal


class CostAwareEntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.execution = {
            "spread_bps_total": 10,
            "slippage_bps": 5,
            "commission_bps_per_side": 5,
            "sell_tax_bps": {"KRW": 0},
        }

    def test_tick_rounding_cost_is_price_specific(self):
        low_price_cost = _estimated_round_trip_cost_pct(
            Decimal("105"), "KRW-MPLX", "KRW", self.execution
        )
        bitcoin_cost = _estimated_round_trip_cost_pct(
            Decimal("104448000"), "KRW-BTC", "KRW", self.execution
        )

        self.assertGreater(low_price_cost, Decimal("1.5"))
        self.assertLess(bitcoin_cost, Decimal("0.5"))

    def test_cost_cap_blocks_market_when_tick_cost_exceeds_limit(self):
        start = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        bars = [
            Bar(start, "KRW-MPLX", "KRW", 105, 105, 105, 105, 1),
            Bar(start + timedelta(minutes=1), "KRW-MPLX", "KRW", 105, 105, 105, 105, 1),
        ]
        config = {
            "initial_cash_krw": 10_000_000,
            "execution": self.execution,
            "risk": {
                "stop_loss_pct": 0.01, "take_profit_pct": 0.02,
                "max_holding_minutes": 30, "max_position_equity_pct": 0.2,
                "max_total_exposure_pct": 0.6, "max_positions": 3,
                "risk_per_trade_pct": 0.005, "daily_loss_stop_pct": 0.02,
                "max_entries_per_day": 10, "max_consecutive_stops": 3,
                "max_round_trip_cost_pct": 0.5,
            },
            "feed": {"pending_signal_max_seconds": 120},
            "strategies": {"B": {}},
        }

        def signal_once(strategy_id, current, history, _config):
            if current is bars[0]:
                return Signal(strategy_id, current.symbol, current.timestamp, "test entry")
            return None

        with patch("trading_paper.simulation.engine.get_signal", side_effect=signal_once):
            report = simulate(
                bars, config, continuous=True, strategy_ids=("B",), liquidate_at_end=True
            )

        self.assertEqual(report["strategies"]["B"]["closed_trades"], 0)
        self.assertEqual(report["strategies"]["B"]["open_positions_marked_at_last_close"], 0)

    def test_cost_cap_is_optional_for_existing_simulations(self):
        start = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        bars = [
            Bar(start, "KRW-MPLX", "KRW", 105, 105, 105, 105, 1),
            Bar(start + timedelta(minutes=1), "KRW-MPLX", "KRW", 105, 105, 105, 105, 1),
        ]
        config = {
            "initial_cash_krw": 10_000_000,
            "execution": self.execution,
            "risk": {
                "stop_loss_pct": 0.01, "take_profit_pct": 0.02,
                "max_holding_minutes": 30, "max_position_equity_pct": 0.2,
                "max_total_exposure_pct": 0.6, "max_positions": 3,
                "risk_per_trade_pct": 0.005, "daily_loss_stop_pct": 0.02,
                "max_entries_per_day": 10, "max_consecutive_stops": 3,
            },
            "feed": {"pending_signal_max_seconds": 120},
            "strategies": {"B": {}},
        }

        def signal_once(strategy_id, current, history, _config):
            if current is bars[0]:
                return Signal(strategy_id, current.symbol, current.timestamp, "test entry")
            return None

        with patch("trading_paper.simulation.engine.get_signal", side_effect=signal_once):
            report = simulate(
                bars, config, continuous=True, strategy_ids=("B",), liquidate_at_end=True
            )

        self.assertEqual(report["strategies"]["B"]["closed_trades"], 1)


if __name__ == "__main__":
    unittest.main()
