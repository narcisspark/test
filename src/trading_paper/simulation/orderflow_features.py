"""Public quote features and conservative depth-based paper fills."""
from __future__ import annotations

from collections import deque
from decimal import Decimal, ROUND_DOWN
import math

FEE = Decimal('0.0005')
SLIP = Decimal('0.0005')
UNIT = Decimal('0.00000001')
WON = Decimal('0.01')


def fee(notional: Decimal, fee_rate: Decimal = FEE) -> Decimal:
    return (notional * fee_rate).quantize(WON, rounding=ROUND_DOWN)


def buy_fill(book: list, budget: Decimal, fee_rate: Decimal = FEE, slippage_rate: Decimal = SLIP):
    """Consume visible ask depth. Assumes all displayed depth is still available."""
    available = budget / (Decimal(1) + fee_rate) / (Decimal(1) + slippage_rate)
    remaining, qty = available, Decimal(0)
    for level in book[:5]:
        price, size = Decimal(str(level['ask_price'])), Decimal(str(level['ask_size']))
        take = min(size, remaining / price).quantize(UNIT, rounding=ROUND_DOWN)
        qty += take
        remaining -= price * take
        if remaining < price * UNIT:
            break
    if qty <= 0 or remaining > available * Decimal('0.001'):
        return None
    raw = _depth_cost(book, qty, 'ask')
    if raw is None:
        return None
    adjusted = raw * (Decimal(1) + slippage_rate)
    commission = fee(adjusted, fee_rate)
    if adjusted + commission > budget:
        return None
    return {'qty': qty, 'raw': raw, 'notional': adjusted, 'fee': commission,
            'slippage': adjusted - raw, 'total': adjusted + commission}


def _depth_cost(book: list, qty: Decimal, side: str):
    left, value = qty, Decimal(0)
    for level in book[:5]:
        price, size = Decimal(str(level[f'{side}_price'])), Decimal(str(level[f'{side}_size']))
        take = min(left, size)
        value += take * price
        left -= take
        if left <= 0:
            return value
    return None


def sell_fill(book: list, qty: Decimal, fee_rate: Decimal = FEE, slippage_rate: Decimal = SLIP):
    raw = _depth_cost(book, qty, 'bid')
    if raw is None:
        return None
    adjusted = raw * (Decimal(1) - slippage_rate)
    commission = fee(adjusted, fee_rate)
    return {'raw': raw, 'notional': adjusted, 'fee': commission,
            'slippage': raw - adjusted, 'total': adjusted - commission}


class Features:
    def __init__(self):
        self.book = None
        self.book_at = 0.0
        self.book_exchange = 0.0
        self.trades = deque()
        self.mids = deque()
        self.ids = set()
        self.id_queue = deque()
        self.last_trade_exchange = 0.0
        self.last_book_exchange = 0.0
        self.invalid = 0

    def on_event(self, payload: dict, received: float):
        kind = payload.get('type')
        if payload.get('code') != 'KRW-BTC' or kind not in {'orderbook', 'trade'}:
            return
        try:
            exchange = float(payload.get('trade_timestamp') if kind == 'trade' else payload['timestamp']) / 1000
            if exchange > received + 0.25 or received - exchange > 1.0:
                self.invalid += 1
                return
            if kind == 'orderbook':
                levels = payload['orderbook_units'][:5]
                if len(levels) < 5 or exchange < self.last_book_exchange:
                    self.invalid += 1
                    return
                for x in levels:
                    if (any(not math.isfinite(float(x[k])) or float(x[k]) <= 0 for k in ('ask_price','bid_price'))
                            or any(not math.isfinite(float(x[k])) or float(x[k]) < 0 for k in ('ask_size','bid_size'))):
                        self.invalid += 1
                        return
                if (float(levels[0]['bid_price']) >= float(levels[0]['ask_price'])
                        or float(levels[0]['bid_size']) <= 0 or float(levels[0]['ask_size']) <= 0):
                    self.invalid += 1
                    return
                self.book, self.book_at, self.book_exchange = levels, received, exchange
                self.last_book_exchange = exchange
                mid = (float(levels[0]['bid_price']) + float(levels[0]['ask_price'])) / 2
                self.mids.append((received, mid))
                while self.mids and self.mids[0][0] < received - 6.5:
                    self.mids.popleft()
            else:
                seq, side, volume = payload.get('sequential_id'), payload.get('ask_bid'), float(payload['trade_volume'])
                if exchange < self.last_trade_exchange or side not in ('ASK','BID') or volume <= 0 or not math.isfinite(volume):
                    self.invalid += 1
                    return
                if seq is not None and seq in self.ids:
                    return
                if seq is not None:
                    self.ids.add(seq); self.id_queue.append((received, seq))
                self.last_trade_exchange = exchange
                self.trades.append((received, volume if side == 'BID' else -volume))
                while self.trades and self.trades[0][0] < received - 6:
                    self.trades.popleft()
                while self.id_queue and self.id_queue[0][0] < received - 30:
                    _, old = self.id_queue.popleft(); self.ids.discard(old)
        except (KeyError, ValueError, TypeError, OverflowError):
            self.invalid += 1

    def snapshot(self, now: float):
        b = self.book
        if b is None or now - self.book_at > 1 or now - self.book_exchange > 1 or len(self.mids) < 2:
            return None
        if self.mids[0][0] > now - 5:
            return None
        flows = []
        for seconds in (1, 5):
            values = [v for t,v in self.trades if now-seconds <= t <= now]
            total = sum(abs(v) for v in values)
            if not total:
                return None
            flows.append(sum(values) / total)
        bids = sum(float(x['bid_size']) for x in b)
        asks = sum(float(x['ask_size']) for x in b)
        imbalance = (bids - asks) / (bids + asks)
        bid, ask = float(b[0]['bid_price']), float(b[0]['ask_price'])
        bs, a_s = float(b[0]['bid_size']), float(b[0]['ask_size'])
        mid = (bid + ask)/2
        micro = (ask*bs + bid*a_s)/(bs+a_s)
        def prior(sec):
            eligible = [p for t,p in self.mids if t <= now-sec]
            return eligible[-1] if eligible else None
        p1, p5 = prior(1), prior(5)
        if not p1 or not p5:
            return None
        spread = (ask-bid)/mid*10000
        features = [imbalance, flows[0], flows[1], (micro/mid-1)*10000,
                    spread, (mid/p1-1)*10000, (mid/p5-1)*10000]
        return {'at': now, 'book_at': self.book_at, 'book': [dict(x) for x in b],
                'features': features, 'mid': mid, 'spread_bps': spread,
                'ask_depth_krw': sum(float(x['ask_price'])*float(x['ask_size']) for x in b)}
