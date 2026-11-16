import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.live_engine import LivePaperEngine
from trading_paper.simulation.models import Bar, Signal
from trading_paper.simulation.strategies import get_signal


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/crypto-paper-alpha-scalp.json").read_text())
KST = ZoneInfo("Asia/Seoul")


class AlphaScalpPaperTests(unittest.TestCase):
    def _bars(self, closes):
        start = datetime(2026, 11, 3, 10, 0, tzinfo=KST)
        return [
            Bar(start + timedelta(minutes=index), "KRW-BTC", "KRW", close, close, close, close, 1.0, close)
            for index, close in enumerate(closes)
        ]

    def _orderbook(self, price, at):
        received = at + timedelta(milliseconds=10)
        return {
            "type": "orderbook",
            "code": "KRW-BTC",
            "timestamp": int(at.timestamp() * 1000),
            "orderbook_units": [{"bid_price": price, "ask_price": price + 1000, "bid_size": 1, "ask_size": 1}],
        }, received

    def test_rsi_oversold_requires_rebound_close(self):
        bars = self._bars([*range(100, 81, -1), 83])
        signal = get_signal("D", bars[-1], bars, CONFIG)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "D")
        self.assertIn("RSI(14)", signal.reason)

        falling = self._bars(list(range(100, 80, -1)))
        self.assertIsNone(get_signal("D", falling[-1], falling, CONFIG))
        daemon_signal = get_signal("E", falling[-1], falling, CONFIG)
        self.assertIsNotNone(daemon_signal)
        self.assertIn("without rebound confirmation", daemon_signal.reason)

    def test_entry_uses_strategy_specific_stop_and_target(self):
        engine = LivePaperEngine(CONFIG, ["KRW-BTC"], run_id="alpha-entry")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        account = engine.accounts["D"]
        account.session = now.astimezone(KST).date()
        account.session_start_equity = account.cash
        account.pending["KRW-BTC"] = Signal("D", "KRW-BTC", now - timedelta(seconds=1), "test signal")
        engine.set_connection(True, now)

        payload, received = self._orderbook(100_000_000, now)
        engine.on_quote(payload, received)
        position = account.positions["KRW-BTC"]
        self.assertEqual(position.strategy_id, "D")
        self.assertEqual(position.stop_price, position.entry_price * Decimal("0.991"))
        self.assertEqual(position.take_price, position.entry_price * Decimal("1.015"))
        self.assertEqual(position.trailing_activation_price, position.entry_price * Decimal("1.0075"))
        self.assertIsNone(position.max_holding_minutes)

        daemon = LivePaperEngine(CONFIG, ["KRW-BTC"], run_id="alpha-daemon-entry")
        daemon_account = daemon.accounts["E"]
        daemon_account.session = now.astimezone(KST).date()
        daemon_account.session_start_equity = daemon_account.cash
        daemon_account.pending["KRW-BTC"] = Signal("E", "KRW-BTC", now - timedelta(seconds=1), "test signal")
        daemon.set_connection(True, now)
        daemon.on_quote(payload, received)
        daemon_position = daemon_account.positions["KRW-BTC"]
        self.assertEqual(daemon_position.stop_price, daemon_position.entry_price * Decimal("0.99"))

    def test_trailing_exit_follows_alpha_scalp_thresholds(self):
        engine = LivePaperEngine(CONFIG, ["KRW-BTC"], run_id="alpha-trailing")
        start = datetime.now(timezone.utc).replace(microsecond=0)
        account = engine.accounts["D"]
        account.session = start.astimezone(KST).date()
        account.session_start_equity = account.cash
        account.pending["KRW-BTC"] = Signal("D", "KRW-BTC", start - timedelta(seconds=1), "test signal")
        engine.set_connection(True, start)
        payload, received = self._orderbook(100_000_000, start)
        engine.on_quote(payload, received)
        position = account.positions["KRW-BTC"]

        peak_at = start + timedelta(seconds=1)
        payload, received = self._orderbook(int(position.entry_price * Decimal("1.01")), peak_at)
        engine.on_quote(payload, received)
        self.assertIn("KRW-BTC", account.positions)

        pullback_at = start + timedelta(seconds=2)
        payload, received = self._orderbook(int(position.highest_price * Decimal("0.994")), pullback_at)
        engine.on_quote(payload, received)
        self.assertNotIn("KRW-BTC", account.positions)
        self.assertEqual(account.trades[-1]["exit_reason"], "TRAILING_STOP")


if __name__ == "__main__":
    unittest.main()
