import json
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_paper.simulation.live_engine import LivePaperEngine
from trading_paper.simulation.models import Signal
from trading_paper.simulation.service_state import EventLog, engine_state, restore_engine
from trading_paper.simulation.service_runner import run_service

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config/crypto-paper.json').read_text())


class ServiceRecoveryTests(unittest.TestCase):
    def test_checkpoint_preserves_open_position_and_risk_counters(self):
        engine = LivePaperEngine(CONFIG, ['KRW-BTC'], run_id='recovery')
        now = datetime.now(timezone.utc)
        account = engine.accounts['A']
        account.session = now.date()
        account.session_start_equity = account.cash
        account.consecutive_stops = 2
        account.pending['KRW-BTC'] = Signal('A', 'KRW-BTC', now, 'recovery test')
        engine.set_connection(True, now)
        engine.on_quote({'type': 'orderbook', 'code': 'KRW-BTC', 'timestamp': int(now.timestamp() * 1000) + 1,
                         'orderbook_units': [{'bid_price': 100000000, 'ask_price': 100001000, 'bid_size': 1, 'ask_size': 1}]}, now)
        self.assertTrue(account.positions)
        self.assertLess(account.cash, Decimal('10000000'))
        self.assertEqual(account.entries_today, 1)
        restored = restore_engine(json.loads(json.dumps(engine_state(engine))), CONFIG, ['KRW-BTC'], 'recovery')
        self.assertEqual(engine.report(now), restored.report(now))
        self.assertEqual(restored.accounts['A'].consecutive_stops, 2)
        with self.assertRaises(ValueError):
            restore_engine(engine_state(engine), {**CONFIG, 'initial_cash_krw': 1}, ['KRW-BTC'], 'recovery')

    def test_log_cursor_survives_compression_and_same_hour_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory))
            log.record({'event': 1})
            cursor = log.cursor()
            log.record({'event': 2})
            log.close()
            log.compress_sealed(all_files=True)
            resumed = EventLog(Path(directory))
            self.assertEqual(list(resumed.after(cursor)), [{'event': 2}])
            resumed.record({'event': 3})
            resumed.close()
            self.assertEqual(list(resumed.after(None)), [{'event': 1}, {'event': 2}, {'event': 3}])

    def test_partial_final_event_is_repaired_without_losing_complete_events(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory))
            log.record({'event': 1})
            path = log.path
            log.close()
            with path.open('ab') as stream:
                stream.write(b'{"event":')
            resumed = EventLog(Path(directory))
            resumed.record({'event': 2})
            resumed.close()
            self.assertEqual(list(resumed.after(None)), [{'event': 1}, {'event': 2}])

    def test_deadline_finalizes_and_completed_run_cannot_start_again(self):
        class Feed:
            def list_krw_pairs(self):
                return ['KRW-BTC']

            def minute_candles(self, market, count, **kwargs):
                return [{'market': market, 'candle_date_time_utc': '2026-10-16T14:59:00',
                         'candle_date_time_kst': '2026-10-16T23:59:00', 'opening_price': 100000000,
                         'high_price': 100000000, 'low_price': 100000000, 'trade_price': 100000000,
                         'candle_acc_trade_volume': 1, 'candle_acc_trade_price': 100000000}]

            async def stream(self, markets):
                now = datetime.now(timezone.utc)
                yield {'type': 'ticker', 'code': 'KRW-BTC', 'timestamp': int(now.timestamp() * 1000), 'trade_price': 100000000}, now
                await asyncio.sleep(10)

        config = {**CONFIG, 'feed': {**CONFIG['feed'], 'warmup_candles_per_market': 1}}
        with tempfile.TemporaryDirectory() as directory, patch('trading_paper.simulation.service_runner.PublicQuotationClient', Feed):
            deadline = datetime.now(timezone.utc) + timedelta(seconds=0.3)
            result = run_service(config, ['KRW-BTC'], directory, 'deadline', deadline, 1)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['websocket_message_count'], 1)
            self.assertTrue((Path(directory) / 'deadline/complete.json').exists())
            again = run_service(config, ['KRW-BTC'], directory, 'deadline', deadline, 1)
            self.assertEqual(result, again)


if __name__ == '__main__':
    unittest.main()
