import unittest
from decimal import Decimal

from trading_paper.simulation.orderflow_features import Features, buy_fill, sell_fill
from trading_paper.simulation.orderflow_live import Account, CAPITAL


def book():
    return [{'bid_price': 100_000_000-i*1000, 'ask_price': 100_010_000+i*1000,
             'bid_size': 2, 'ask_size': 1} for i in range(5)]


class OrderflowPaperTest(unittest.TestCase):
    def test_round_trip_deducts_fees_slippage_and_spread(self):
        buy = buy_fill(book(), Decimal('500000'))
        self.assertIsNotNone(buy)
        sell = sell_fill(book(), buy['qty'])
        self.assertIsNotNone(sell)
        self.assertLessEqual(buy['total'], Decimal('500000'))
        self.assertGreater(buy['fee'], 0)
        self.assertGreater(sell['fee'], 0)
        self.assertGreater(buy['slippage'], 0)
        self.assertGreater(sell['slippage'], 0)
        self.assertGreater(buy['total'] - sell['total'], Decimal('1000'))

    def test_higher_fee_and_slippage_reduce_fill_capacity_and_exit_value(self):
        base = buy_fill(book(), Decimal('500000'))
        stressed = buy_fill(book(), Decimal('500000'), Decimal('0.001'), Decimal('0.001'))
        self.assertLess(stressed['qty'], base['qty'])
        base_exit = sell_fill(book(), base['qty'])
        stressed_exit = sell_fill(book(), base['qty'], Decimal('0.001'), Decimal('0.001'))
        self.assertLess(stressed_exit['total'], base_exit['total'])

    def test_account_waits_for_latency_and_reconciles_cash(self):
        a = Account('F1_rules')
        entries = []
        a.pending = {'side':'BUY','due':100.5,'requested_at':100,'prediction':0}
        a.on_book(book(),100.4,entries.append)
        self.assertIsNone(a.position)
        a.on_book(book(),100.5,entries.append)
        self.assertIsNotNone(a.position)
        self.assertLess(a.cash,CAPITAL)
        a.pending = {'side':'SELL','due':131.0,'requested_at':130.5,'reason':'MAX_HOLD_30S'}
        a.on_book(book(),131.0,entries.append)
        self.assertIsNone(a.position)
        self.assertEqual(len(a.closed),1)
        self.assertEqual(a.cash - CAPITAL, Decimal(a.closed[0]['pnl_krw']))
        self.assertLess(a.cash,CAPITAL)

    def test_stale_or_crossed_orderbook_cannot_refresh_features(self):
        f = Features()
        f.on_event({'code':'KRW-BTC','type':'orderbook','timestamp':100000,'orderbook_units':book()},100)
        self.assertIsNotNone(f.book)
        crossed=book(); crossed[0]['bid_price']=crossed[0]['ask_price']
        f.on_event({'code':'KRW-BTC','type':'orderbook','timestamp':101000,'orderbook_units':crossed},101)
        self.assertEqual(f.book_at,100)
        self.assertEqual(f.invalid,1)
        self.assertIsNone(f.snapshot(102))

    def test_duplicate_trade_is_ignored(self):
        f=Features()
        trade={'code':'KRW-BTC','type':'trade','trade_timestamp':100000,'trade_volume':0.2,
               'ask_bid':'BID','sequential_id':123}
        f.on_event(trade,100)
        f.on_event(trade,100.01)
        self.assertEqual(len(f.trades),1)


if __name__ == '__main__':
    unittest.main()
