"""Minute polling paper trading, with single-writer S3 transaction recovery.

Only current REST orderbook samples can fill a virtual trade. Minute OHLC highs
and lows never manufacture fills between two polls. A DynamoDB lease and a
durable pending event prevent duplicated fills on overlapping invocations.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from trading_paper.providers.upbit_public import PublicQuotationClient
from trading_paper.simulation.live_engine import LivePaperEngine, _bar_from_rest
from trading_paper.simulation.service_runner import _session_history
from trading_paper.simulation.service_state import engine_state, restore_engine


def _get(s3, bucket: str, key: str):
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    return json.loads(response["Body"].read())


def _put(s3, bucket: str, key: str, value: Any) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
                  ContentType="application/json", ServerSideEncryption="AES256")


def _closed_since(client, market: str, since: datetime, now: datetime) -> list[dict[str, Any]]:
    rows = {}
    before = None
    for _ in range(9):
        page = client.minute_candles(market, 200, to=before)
        if not page:
            break
        for row in page:
            close_at = _bar_from_rest(row).timestamp + timedelta(minutes=1)
            if since < close_at <= now:
                rows[row["candle_date_time_utc"]] = row
        oldest = min(page, key=lambda item: item["candle_date_time_utc"])
        if _bar_from_rest(oldest).timestamp <= since:
            break
        before = oldest["candle_date_time_utc"] + "Z"
    return sorted(rows.values(), key=lambda row: row["candle_date_time_utc"])


def _apply(engine: LivePaperEngine, event: dict[str, Any]) -> None:
    for market, rows in event.get("seed", {}).items():
        engine.seed_history(market, rows)
    for row in event["completed_candles"]:
        engine._complete_bar(_bar_from_rest(row))
    for sample in event["quotes"]:
        engine.connected = True
        engine.on_quote(sample["payload"], datetime.fromisoformat(sample["received_at"]))
    engine.connected = False


def _report(engine: LivePaperEngine, meta: dict[str, Any], now: datetime, status: str) -> dict[str, Any]:
    result = engine.report(now)
    result.update({"classification": "LIVE PUBLIC REST DATA — ONE-MINUTE POLLING PAPER FILLS",
                   "status": status, "source": "UPBIT PUBLIC REST", "sampling_interval_seconds": 60,
                   "as_of": now.isoformat(), "started_at": meta["started_at"], "deadline": meta["deadline"],
                   "rest_quote_sample_count": engine.websocket_message_count, "websocket_message_count": 0,
                   "websocket_type_counts": {}, "completed_polled_candle_count": engine.completed_candle_count,
                   "execution_model": "Signals use completed one-minute candles. Buy/sell fills use only the next sampled current ask/bid. No intraminute high/low fills are inferred.",
                   "config": engine.config})
    return result


def _markdown(result: dict[str, Any]) -> str:
    lines = ['# 1분 간격 Upbit 공개 시세 페이퍼 트레이딩', '',
             '> 실제 공개 시세 / 가상 거래. 매수·매도는 조회 시점의 호가로만 계산합니다.',
             '> 1분 사이의 고가·저가로 손절·익절 체결을 만들지 않습니다.', '',
             f"- 실행: `{result['run_id']}`", f"- 상태: {result['status']}",
             f"- 시작: {result['started_at']}", f"- 종료 예정: {result['deadline']}",
             f"- 기준 시각: {result['as_of']}", f"- 종목: {', '.join(result['started_markets'])}",
             f"- 호가 조회 샘플: {result['rest_quote_sample_count']}",
             f"- 새로 처리한 완성봉: {result['completed_polled_candle_count']}", '',
             '| 전략 | 가상 현금 | 평가 자산 | 실현 손익 | 미실현 손익 | 수익률 | 수수료 | 매수·매도 체결 수 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for key, p in result['portfolios'].items():
        lines.append(f"| {key} | {p['cash_krw']} | {p['equity_krw']} | {p['realized_pnl_krw']} | {p['unrealized_pnl_krw']} | {p['net_return_pct']}% | {p['fees_paid_total_krw']} | {p['fills_count']} |")
    lines += ['', '초기 자금은 전략별 1,000만 원입니다. 매수/매도마다 수수료 0.05%와 슬리피지 0.05%를 적용합니다.',
              '매수 신호 후 현재 ask로 사고, 손절 1%·익절 2%·보유 30분 조건을 현재 bid에서 확인합니다.',
              '손절과 익절은 조회 시점에만 확인하므로 그 사이 움직임은 놓칠 수 있습니다. 오래된 시세로 체결하지 않습니다.',
              '종료 때 남은 포지션은 마지막 유효 가격으로 평가하고 미실현 손익을 별도로 남깁니다.', '']
    return '\n'.join(lines)


def poll(s3, scheduler, client, *, bucket: str, prefix: str, run_id: str,
         schedule_name: str, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    markets = sorted(config['default_markets'])
    meta = _get(s3, bucket, prefix + 'run.json')
    if meta is None:
        meta = {'run_id': run_id, 'started_at': now.isoformat(), 'deadline': (now + timedelta(days=7)).isoformat(),
                'markets': markets, 'config': config, 'sampling_interval_seconds': 60}
        _put(s3, bucket, prefix + 'run.json', meta)
    if meta['config'] != config or meta['markets'] != markets or meta['run_id'] != run_id:
        raise ValueError('Run settings changed; refusing to reset virtual portfolios')
    saved = _get(s3, bucket, prefix + 'checkpoint.json')
    if saved:
        if saved.get('schema_version') != 1:
            raise ValueError('Unsupported checkpoint schema')
        engine = restore_engine(saved['engine'], config, markets, run_id)
        last_slot = saved['last_slot']
    else:
        engine = LivePaperEngine(config, markets, run_id=run_id)
        last_slot = ''

    def commit(event):
        nonlocal last_slot
        _apply(engine, event)
        last_slot = event['slot']
        compressed = gzip.compress(json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode(), compresslevel=3)
        s3.put_object(Bucket=bucket, Key=prefix + 'events/' + last_slot + '.json.gz', Body=compressed,
                      ContentType='application/json', ContentEncoding='gzip', ServerSideEncryption='AES256')
        _put(s3, bucket, prefix + 'checkpoint.json', {'schema_version': 1, 'last_slot': last_slot,
                                                    'saved_at': now.isoformat(), 'engine': engine_state(engine)})

    # Complete a transaction whose source data was saved before a prior timeout.
    pending = _get(s3, bucket, prefix + 'pending.json')
    if pending and pending['slot'] > last_slot:
        commit(pending)

    deadline = datetime.fromisoformat(meta['deadline'])
    slot = now.strftime('%Y%m%dT%H%MZ')
    complete = now >= deadline
    if not complete and slot > last_slot:
        seed = {}
        if not saved and not pending:
            if set(markets) - set(client.list_krw_pairs()):
                raise ValueError('Requested market is unavailable or flagged by Upbit')
            for market in markets:
                seed[market] = _session_history(client, market, int(config['feed']['warmup_candles_per_market']))
        rows = []
        if not seed:
            for market in markets:
                since = engine.last_complete_at.get(market, now - timedelta(minutes=10))
                rows.extend(_closed_since(client, market, since, now))
        samples = []
        for market in markets:
            quote = client.orderbook(market)
            samples.append({'payload': quote, 'received_at': datetime.now(timezone.utc).isoformat()})
        event = {'slot': slot, 'collected_at': now.isoformat(), 'seed': seed,
                 'completed_candles': sorted(rows, key=lambda row: row['candle_date_time_utc']), 'quotes': samples}
        _put(s3, bucket, prefix + 'pending.json', event)
        commit(event)
    result = _report(engine, meta, datetime.now(timezone.utc), 'complete' if complete else 'running')
    _put(s3, bucket, prefix + 'report.json', result)
    s3.put_object(Bucket=bucket, Key=prefix + 'report.md', Body=_markdown(result).encode(), ContentType='text/markdown; charset=utf-8', ServerSideEncryption='AES256')
    if complete:
        _put(s3, bucket, prefix + 'complete.json', {'finished_at': now.isoformat(), 'deadline': meta['deadline']})
        try:
            scheduler.delete_schedule(Name=schedule_name)
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'ResourceNotFoundException':
                raise
    summary = {'run_id': run_id, 'status': result['status'], 'as_of': result['as_of'], 'deadline': meta['deadline'],
               'slot': last_slot, 'rest_quote_samples': result['rest_quote_sample_count'],
               'completed_candles': result['completed_polled_candle_count'],
               'fills': {key: value['fills_count'] for key, value in result['portfolios'].items()}}
    print(json.dumps(summary))
    return summary


def handler(event, context):
    import boto3
    ddb = boto3.client('dynamodb')
    owner = context.aws_request_id
    run_id = os.environ['RUN_ID']
    table = os.environ['LOCK_TABLE']
    if not acquire_lease(ddb, table, run_id, owner, int(time.time())):
        print(json.dumps({'run_id': run_id, 'status': 'another invocation owns the lease'}))
        return {'status': 'busy'}
    try:
        with open(os.environ['PAPER_CONFIG'], encoding='utf-8') as source:
            config = json.load(source)
        return poll(boto3.client('s3'), boto3.client('scheduler'), PublicQuotationClient(),
                    bucket=os.environ['RESULT_BUCKET'], prefix=os.environ['RESULT_PREFIX'],
                    run_id=run_id, schedule_name=os.environ['SCHEDULE_NAME'], config=config)
    finally:
        try:
            ddb.delete_item(TableName=table, Key={'pk': {'S': run_id}},
                            ConditionExpression='lease_owner = :owner', ExpressionAttributeValues={':owner': {'S': owner}})
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                raise


def acquire_lease(ddb, table: str, run_id: str, owner: str, epoch: int) -> bool:
    try:
        ddb.put_item(TableName=table, Item={'pk': {'S': run_id}, 'lease_owner': {'S': owner}, 'lease_until': {'N': str(epoch + 150)}},
                     ConditionExpression='attribute_not_exists(pk) OR lease_until < :now',
                     ExpressionAttributeValues={':now': {'N': str(epoch)}})
        return True
    except Exception as exc:
        if getattr(exc, 'response', {}).get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False
        raise
