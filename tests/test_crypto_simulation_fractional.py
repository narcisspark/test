import json
import unittest
import copy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.engine import Account, _close, _try_entry
from trading_paper.simulation.models import Bar, Signal


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/simulation.json").read_text())
KST = ZoneInfo("Asia/Seoul")


class FractionalCryptoQuantityTest(unittest.TestCase):
    def _enter(self, market):
        initial = Decimal(str(CONFIG["initial_cash_krw"]))
        account = Account("A", initial, initial)
        now = datetime(2026, 11, 4, 10, 0, tzinfo=KST)
        symbol = "KRW-BTC" if market == "KRW" else "005930"
        bar = Bar(now, symbol, market, 100_000_000 if market == "KRW" else 100_000,
                  100_000_000 if market == "KRW" else 100_000,
                  100_000_000 if market == "KRW" else 100_000,
                  100_000_000 if market == "KRW" else 100_000, 1, 100_000_000)
        execution = copy.deepcopy(CONFIG["execution"])
        execution["sell_tax_bps"].setdefault(market, 0.0)
        _try_entry(account, Signal("A", symbol, now, "unit test"), bar,
                   execution, CONFIG["risk"])
        return account

    def test_btc_entry_uses_fractional_eight_decimal_quantity(self):
        account = self._enter("KRW")
        position = account.positions["KRW-BTC"]
        self.assertIsInstance(position.quantity, Decimal)
        self.assertGreater(position.quantity, 0)
        self.assertLess(position.quantity, 1)
        self.assertEqual(position.quantity, position.quantity.quantize(Decimal("0.00000001")))
        _close(account, position, Bar(position.entry_time, "KRW-BTC", "KRW", 100_000_000,
                                      100_000_000, 100_000_000, 100_000_000, 1),
               100_000_000, "TEST", CONFIG["execution"])
        trade = account.trades[-1]
        self.assertIsInstance(trade["quantity"], float)
        self.assertGreater(trade["estimated_spread_slippage_krw"], 0)
        self.assertAlmostEqual(trade["gross_market_move_pnl_krw"]-trade["total_costs_krw"],
                               trade["net_pnl_krw"], places=2)

    def test_stock_entry_remains_whole_shares(self):
        account = self._enter("KOSPI")
        position = account.positions["005930"]
        self.assertEqual(position.quantity, Decimal("19"))


if __name__ == "__main__":
    unittest.main()
