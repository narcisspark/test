from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import unittest

from trading_paper.simulation.engine import _price_tick


class UpbitKrwPriceTickTests(unittest.TestCase):
    def test_fractional_tick_below_one_hundred_won(self):
        self.assertEqual(_price_tick(Decimal("15.01"), ROUND_CEILING, "KRW-ALT"), Decimal("15.1"))
        self.assertEqual(_price_tick(Decimal("15.09"), ROUND_FLOOR, "KRW-ALT"), Decimal("15.0"))

    def test_usdt_uses_half_won_tick_in_its_special_price_tier(self):
        self.assertEqual(_price_tick(Decimal("1500.01"), ROUND_CEILING, "KRW-USDT"), Decimal("1500.5"))
        self.assertEqual(_price_tick(Decimal("1500.49"), ROUND_FLOOR, "KRW-USDT"), Decimal("1500.0"))
        self.assertEqual(_price_tick(Decimal("7000.01"), ROUND_CEILING, "KRW-USDT"), Decimal("7000.5"))

    def test_btc_price_uses_hundred_won_tick_in_its_tier(self):
        self.assertEqual(_price_tick(Decimal("100123"), ROUND_FLOOR, "KRW-BTC"), Decimal("100100"))
