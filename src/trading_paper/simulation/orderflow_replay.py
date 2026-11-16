"""Costed offline replay for the channel-breakout paper hypothesis."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
from typing import Iterable

from trading_paper.simulation.orderflow_features import FEE, SLIP, Features, buy_fill, sell_fill
from trading_paper.simulation.orderflow_strategy import ChannelBreakoutConfirmation

CAPITAL = Decimal('10000000')
DELAY = 0.5
STOP_PCT = 0.005
MAX_HOLD_SECONDS = 300
MAX_DRAWDOWN_PCT = Decimal('0.005')


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class _PaperAccount:
    def __init__(self, fee_rate: Decimal, slippage_rate: Decimal) -> None:
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.cash = CAPITAL
        self.position = None
        self.pending = None
        self.closed = []
        self.cooldown = 0.0
        self.signals = 0
        self.entries = 0
        self.rejected = 0
        self.fees = Decimal(0)
        self.slippage = Decimal(0)
        self.peak = CAPITAL
        self.max_drawdown = Decimal(0)
        self.loss_limit_hit = False
        self.breakdown_count = 0

    def equity(self, book):
        if not self.position:
            return self.cash
        fill = sell_fill(book, self.position['qty'], self.fee_rate, self.slippage_rate) if book else None
        return self.cash + fill['total'] if fill else None

    def observe(self, book):
        value = self.equity(book)
        if value is None:
            return
        self.peak = max(self.peak, value)
        self.max_drawdown = max(self.max_drawdown, (self.peak-value)/self.peak)
        if value <= CAPITAL * (Decimal(1)-MAX_DRAWDOWN_PCT):
            self.loss_limit_hit = True

    def on_book(self, book, at: float):
        self.observe(book)
        order = self.pending
        if order and at >= order['due']:
            if at-order['requested_at'] > 2:
                self.pending = None
                self.rejected += 1
            elif order['side'] == 'BUY':
                equity = self.equity(book)
                budget = min(self.cash, equity * Decimal('0.05')) if equity is not None else Decimal(0)
                fill = buy_fill(book, budget, self.fee_rate, self.slippage_rate)
                if fill is None or fill['notional'] < Decimal('5000'):
                    self.rejected += 1
                else:
                    self.cash -= fill['total']
                    self.fees += fill['fee']; self.slippage += fill['slippage']
                    self.position = {
                        'qty': fill['qty'], 'entry_total': fill['total'],
                        'entry_price': fill['notional']/fill['qty'], 'entry_at': at,
                        'breakout_level': order['breakout_level'],
                        'entry_fee': fill['fee'], 'entry_slippage': fill['slippage'],
                    }
                    self.entries += 1
            elif self.position:
                fill = sell_fill(book, self.position['qty'], self.fee_rate, self.slippage_rate)
                if fill is None:
                    self.rejected += 1
                else:
                    pos = self.position
                    pnl = fill['total'] - pos['entry_total']
                    self.cash += fill['total']; self.fees += fill['fee']; self.slippage += fill['slippage']
                    self.closed.append({
                        'entry_at': _stamp(pos['entry_at']), 'exit_at': _stamp(at),
                        'entry_price': str(pos['entry_price']), 'exit_price': str(fill['notional']/pos['qty']),
                        'pnl_krw': str(pnl), 'net_return_pct': str(pnl/pos['entry_total']*100),
                        'reason': order['reason'], 'entry_fee_krw': str(pos['entry_fee']),
                        'exit_fee_krw': str(fill['fee']), 'entry_slippage_krw': str(pos['entry_slippage']),
                        'exit_slippage_krw': str(fill['slippage']),
                    })
                    self.position = None; self.cooldown = at+60; self.breakdown_count = 0
            self.pending = None
            self.observe(book)

        if self.position and not self.pending:
            if float(book[0]['bid_price']) <= float(self.position['entry_price'])*(1-STOP_PCT):
                self.pending = {'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'STOP_LOSS'}
            elif at-self.position['entry_at'] >= MAX_HOLD_SECONDS:
                self.pending = {'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'MAX_HOLD_5M'}

    def summary(self, book):
        equity = self.equity(book) if book else self.cash
        realized = sum((Decimal(x['pnl_krw']) for x in self.closed), Decimal(0))
        return {
            'initial_krw': str(CAPITAL), 'cash_krw': str(self.cash),
            'final_equity_after_estimated_exit_costs_krw': str(equity) if equity is not None else None,
            'net_return_pct': str((equity/CAPITAL-1)*100) if equity is not None else None,
            'realized_pnl_krw': str(realized), 'fees_paid_krw': str(self.fees),
            'additional_slippage_paid_krw': str(self.slippage), 'closed_trades': len(self.closed),
            'wins': sum(Decimal(x['pnl_krw']) > 0 for x in self.closed),
            'losses': sum(Decimal(x['pnl_krw']) < 0 for x in self.closed),
            'signals': self.signals, 'entries': self.entries, 'rejected_or_unfilled': self.rejected,
            'open_position': self.position is not None, 'run_loss_limit_hit': self.loss_limit_hit,
            'max_drawdown_pct': str(self.max_drawdown*100), 'trades': self.closed,
        }


def replay_orderflow(paths: Iterable[str | Path], *, fee_rate: Decimal = FEE,
                     slippage_rate: Decimal = SLIP) -> dict:
    """Replay saved public Upbit events without lookahead or live credentials."""
    source_paths = sorted(Path(path) for path in paths)
    if not source_paths:
        raise ValueError('At least one event archive is required')
    if fee_rate < 0 or slippage_rate < 0:
        raise ValueError('fee and slippage rates must be non-negative')
    account = _PaperAccount(fee_rate, slippage_rate)
    features = Features()
    strategy = ChannelBreakoutConfirmation()
    connected = False
    last_event_at = None
    last_eval_second = None
    first_at = last_at = None
    first_book = last_book = None
    event_count = 0; book_count = 0; trade_count = 0; invalid_at_start = 0
    segment_count = 0

    def reset_segment(at):
        nonlocal features, strategy, connected, segment_count
        if account.pending:
            account.pending = None
            account.rejected += 1
        if account.position and not account.pending and features.book:
            account.pending = {'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'DATA_GAP'}
        features = Features(); strategy.reset(); connected = False; segment_count += 1

    for path in source_paths:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    kind = record.get('kind')
                    at = datetime.fromisoformat(record['received_at']).timestamp()
                    if first_at is None:
                        first_at = at
                    if last_event_at is not None and at-last_event_at > 2:
                        reset_segment(at)
                    last_at = at; last_event_at = at
                    if kind == 'connection':
                        if not record.get('connected'):
                            reset_segment(at)
                        continue
                    payload = record.get('payload')
                    if not isinstance(payload, dict):
                        continue
                    if payload.get('type') not in ('orderbook','trade'):
                        continue
                    if not connected:
                        connected = True
                    features.on_event(payload, at)
                    event_count += 1
                    if payload.get('type') == 'trade':
                        trade_count += 1
                    if payload.get('type') != 'orderbook' or features.book_at != at:
                        continue
                    book_count += 1
                    book = features.book
                    if first_book is None:
                        first_book = [dict(x) for x in book]
                    last_book = [dict(x) for x in book]
                    bid, ask = float(book[0]['bid_price']), float(book[0]['ask_price'])
                    mid = (bid+ask)/2
                    strategy.observe_quote(at, mid)
                    account.on_book(book, at)
                    second = int(at)
                    if second == last_eval_second:
                        continue
                    last_eval_second = second
                    snapshot = features.snapshot(at)
                    if snapshot is None:
                        continue
                    if account.position:
                        level = float(account.position['breakout_level'])
                        account.breakdown_count = account.breakdown_count+1 if snapshot['mid'] < level else 0
                        if account.breakdown_count >= 2 and not account.pending:
                            account.pending = {'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'FAILED_BREAKOUT'}
                    elif (not account.pending and not account.loss_limit_hit and at >= account.cooldown):
                        level = strategy.confirm(at, snapshot['mid'], snapshot['features'],
                                                 snapshot['spread_bps'], snapshot['ask_depth_krw'])
                        if level is not None:
                            account.signals += 1
                            account.pending = {'side':'BUY','due':at+DELAY,'requested_at':at,
                                               'breakout_level':level,'reason':'CHANNEL_BREAKOUT'}
                    account.observe(book)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError, OverflowError):
                    continue
    if account.position and features.book:
        fill = sell_fill(features.book, account.position['qty'], account.fee_rate, account.slippage_rate)
        if fill:
            pos=account.position; pnl=fill['total']-pos['entry_total']
            account.cash += fill['total']; account.fees += fill['fee']; account.slippage += fill['slippage']
            account.closed.append({'entry_at':_stamp(pos['entry_at']),'exit_at':_stamp(last_at),
                                   'entry_price':str(pos['entry_price']),'exit_price':str(fill['notional']/pos['qty']),
                                   'pnl_krw':str(pnl),'net_return_pct':str(pnl/pos['entry_total']*100),
                                   'reason':'DATA_END_FORCE','entry_fee_krw':str(pos['entry_fee']),
                                   'exit_fee_krw':str(fill['fee']),'entry_slippage_krw':str(pos['entry_slippage']),
                                   'exit_slippage_krw':str(fill['slippage'])})
            account.position=None
    benchmark = None
    if first_book is not None and last_book is not None:
        start_fill = buy_fill(first_book, CAPITAL*Decimal('0.05'), fee_rate, slippage_rate)
        end_fill = sell_fill(last_book, start_fill['qty'], fee_rate, slippage_rate) if start_fill else None
        if start_fill and end_fill:
            benchmark_pnl = end_fill['total']-start_fill['total']
            benchmark = {'name':'5% initial-capital buy-and-hold; 95% stays in cash',
                         'initial_exposure_krw':str(start_fill['total']),
                         'net_pnl_krw':str(benchmark_pnl),
                         'net_return_pct_of_10m_account':str(benchmark_pnl/CAPITAL*100),
                         'net_return_pct_of_exposure':str(benchmark_pnl/start_fill['total']*100)}
    return {
        'classification':'OFFLINE REPLAY OF SAVED PUBLIC UPBIT EVENTS — PAPER ONLY',
        'policy':{'lookback_seconds':300,'minimum_observed_history_seconds':240,
                  'flow_confirmations':2,'buy_latency_seconds':DELAY,'sell_latency_seconds':DELAY,
                  'stop_loss_pct':STOP_PCT*100,'max_hold_seconds':MAX_HOLD_SECONDS,
                  'failed_breakout_confirmations':2,'fee_pct_per_side':float(fee_rate*100),
                  'additional_slippage_pct_per_side':float(slippage_rate*100),'position_pct_of_equity':5,
                  'cooldown_seconds':60},
        'data':{'files':[str(x) for x in source_paths],'start_utc':_stamp(first_at) if first_at else None,
                'end_utc':_stamp(last_at) if last_at else None,'segments':segment_count,
                'websocket_events':event_count,'orderbooks':book_count,'trades':trade_count,
                'invalid_events':features.invalid},
        'benchmark':benchmark,
        'account':account.summary(features.book),
    }


def write_replay_report(report: dict, destination: str | Path) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return output
