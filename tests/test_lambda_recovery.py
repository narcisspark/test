import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.lambda_runner import poll

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config/crypto-paper.json').read_text())
CONFIG = {**CONFIG, 'feed': {**CONFIG['feed'], 'warmup_candles_per_market': 1}}


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class Store:
    def __init__(self):
        self.objects = {}
        self.fail_checkpoint = False

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {'Body': io.BytesIO(self.objects[Key])}

    def put_object(self, **kwargs):
        if self.fail_checkpoint and kwargs['Key'].endswith('checkpoint.json'):
            self.fail_checkpoint = False
            raise RuntimeError('simulated failure before state commit')
        self.objects[kwargs['Key']] = kwargs['Body']


class Scheduler:
    def __init__(self):
        self.deleted = []

    def delete_schedule(self, Name):
        self.deleted.append(Name)


class Feed:
    def __init__(self, base):
        self.base = base
        self.calls = 0
        self.second = False

    def list_krw_pairs(self):
        return ['KRW-BTC']

    def minute_candles(self, market, count, **kwargs):
        rows = []
        for offset, close in [(-3, 104000000), (-2, 103000000), (-1, 97000000), (0, 98000000), (1, 98000000)]:
            if offset == 1 and not self.second:
                continue
            stamp = self.base + timedelta(minutes=offset)
            rows.append({'market': market, 'candle_date_time_utc': stamp.replace(tzinfo=None).isoformat(),
                         'candle_date_time_kst': stamp.astimezone(ZoneInfo('Asia/Seoul')).replace(tzinfo=None).isoformat(),
                         'opening_price': close, 'high_price': close, 'low_price': close, 'trade_price': close,
                         'candle_acc_trade_volume': 1, 'candle_acc_trade_price': close})
        return list(reversed(rows))

    def orderbook(self, market):
        self.calls += 1
        return {'type': 'orderbook', 'code': market, 'timestamp': int(datetime.now(timezone.utc).timestamp() * 1000),
                'orderbook_units': [{'bid_price': 100000000, 'ask_price': 100001000, 'bid_size': 1, 'ask_size': 1}]}


class LambdaRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=2)
        self.store, self.scheduler, self.feed = Store(), Scheduler(), Feed(self.base)

    def run_poll(self, now):
        return poll(self.store, self.scheduler, self.feed, bucket='bucket', prefix='run/', run_id='minute',
                    schedule_name='minute', config=CONFIG, now=now)

    def test_duplicate_minute_does_not_repeat_fill_or_quote(self):
        initial = self.run_poll(self.base)
        self.feed.second = True
        second = self.run_poll(self.base + timedelta(minutes=1))
        self.assertEqual(second['fills']['B'], 1)
        checkpoint = self.store.objects['run/checkpoint.json']
        duplicate = self.run_poll(self.base + timedelta(minutes=1, seconds=10))
        self.assertEqual(duplicate['fills']['B'], 1)
        self.assertEqual(self.feed.calls, 2)
        self.assertEqual(checkpoint, self.store.objects['run/checkpoint.json'])
        self.assertEqual(initial['deadline'], second['deadline'])

    def test_uncommitted_source_event_recovers_once(self):
        self.store.fail_checkpoint = True
        with self.assertRaises(RuntimeError):
            self.run_poll(self.base)
        self.assertIn('run/pending.json', self.store.objects)
        result = self.run_poll(self.base)
        self.assertEqual(result['rest_quote_samples'], 1)
        self.assertEqual(self.feed.calls, 1)
        self.assertEqual(len([key for key in self.store.objects if '/events/' in key]), 1)

    def test_buy_before_failed_checkpoint_is_recovered_exactly_once(self):
        self.run_poll(self.base)
        self.feed.second = True
        self.store.fail_checkpoint = True
        with self.assertRaises(RuntimeError):
            self.run_poll(self.base + timedelta(minutes=1))
        recovered = self.run_poll(self.base + timedelta(minutes=1))
        self.assertEqual(recovered['fills']['B'], 1)
        self.assertEqual(recovered['rest_quote_samples'], 2)
        self.assertEqual(self.feed.calls, 2)
        report = json.loads(self.store.objects['run/report.json'])
        self.assertLess(int(report['portfolios']['B']['cash_krw']), 10000000)
        self.assertEqual(len(report['portfolios']['B']['open_positions']), 1)
        again = self.run_poll(self.base + timedelta(minutes=1, seconds=20))
        self.assertEqual(again['fills']['B'], 1)

    def test_deadline_stops_sampling_and_deletes_own_schedule(self):
        self.run_poll(self.base)
        result = self.run_poll(self.base + timedelta(days=7, seconds=1))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.feed.calls, 1)
        self.assertEqual(self.scheduler.deleted, ['minute'])
        self.assertIn('run/complete.json', self.store.objects)


if __name__ == '__main__':
    unittest.main()
