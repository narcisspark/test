from decimal import Decimal
import unittest

from trading_paper.simulation.quote_cost_audit import snapshot_cost_bps


class QuoteCostAuditTests(unittest.TestCase):
    def test_same_book_cost_includes_spread_and_both_fees_once(self):
        book = [
            {"ask_price": 101 + index, "bid_price": 100 - index,
             "ask_size": 100_000, "bid_size": 100_000}
            for index in range(5)
        ]
        result = snapshot_cost_bps(book, Decimal("1000000"))
        expected = (1 - (100 * (1 - 0.0005)) / (101 * (1 + 0.0005))) * 10_000
        self.assertAlmostEqual(result, expected, places=2)

    def test_insufficient_visible_depth_is_not_assumed_filled(self):
        book = [
            {"ask_price": 101 + index, "bid_price": 100 - index,
             "ask_size": 0.000001, "bid_size": 0.000001}
            for index in range(5)
        ]
        self.assertIsNone(snapshot_cost_bps(book, Decimal("1000000")))
