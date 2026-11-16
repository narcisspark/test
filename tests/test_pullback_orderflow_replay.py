from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trading_paper.simulation.models import Bar
from trading_paper.simulation.pullback_orderflow_replay import (
    PullbackAccount,
    _flow_confirmation,
    _indicators,
)


def make_book(mid=100.0, size=10000.0):
    asks = [mid + 0.05 + i * 0.05 for i in range(5)]
    bids = [mid - 0.05 - i * 0.05 for i in range(5)]
    return [
        {
            "ask_price": asks[i],
            "bid_price": bids[i],
            "ask_size": size,
            "bid_size": size,
        }
        for i in range(5)
    ]


class PullbackReplayTest(unittest.TestCase):
    def test_strict_flow_gate_requires_orderbook_and_liquidity_support(self):
        features = [0.15, 0.25, 0.30, 0.01, 4, 0.2, 0.3]
        self.assertTrue(_flow_confirmation("strict", features, 4, 3_000_000))
        weak_five_second_flow = [0.15, 0.25, 0.19, 0.01, 4, 0.2, 0.3]
        self.assertFalse(_flow_confirmation("strict", weak_five_second_flow, 4, 3_000_000))
        self.assertTrue(_flow_confirmation("v1", weak_five_second_flow, 20, 1))

    def test_indicators_are_causal_when_future_bars_are_appended(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar(start + timedelta(minutes=i), "KRW-BTC", "KRW",
                100 + i, 101 + i, 99 + i, 100 + i, 1.0)
            for i in range(80)
        ]
        extended = bars + [
            Bar(start + timedelta(minutes=80), "KRW-BTC", "KRW", 1000, 1010, 990, 1000, 1.0),
            Bar(start + timedelta(minutes=81), "KRW-BTC", "KRW", 900, 910, 890, 900, 1.0),
        ]
        original_indicators = _indicators(bars)
        extended_indicators = _indicators(extended)
        for original, later in zip(original_indicators, extended_indicators):
            self.assertEqual(original, later[:len(bars)])

    def test_entry_respects_cash_and_records_costs(self):
        account = PullbackAccount(Decimal("0.0005"), Decimal("0.0005"))
        account._buy(
            make_book(),
            1.0,
            {
                "signal_at": 0.5,
                "signal_mid": 100.0,
                "atr": 1.0,
                "stop": 99.8,
            },
        )
        self.assertIsNotNone(account.position)
        self.assertGreater(account.position["qty"], 0)
        self.assertLess(account.cash, Decimal("10000000"))
        self.assertGreater(account.fees, 0)
        self.assertGreater(account.slippage, 0)

    def test_entry_cancels_if_price_has_run_away(self):
        account = PullbackAccount(Decimal("0.0005"), Decimal("0.0005"))
        account._buy(
            make_book(mid=101.0),
            1.0,
            {
                "signal_at": 0.5,
                "signal_mid": 100.0,
                "atr": 1.0,
                "stop": 99.8,
            },
        )
        self.assertIsNone(account.position)
        self.assertEqual(account.rejections_by_reason, {"PRICE_CHASE_LIMIT": 1})


if __name__ == "__main__":
    unittest.main()
