"""One-hour, isolated F1/F2 paper experiment. Public quotation only."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
import time
from pathlib import Path

from trading_paper.providers.upbit_public import PublicQuotationClient
from trading_paper.simulation.orderflow_features import Features, buy_fill, sell_fill

CAPITAL = Decimal('10000000')
THRESHOLD = 0.0003
DELAY = 0.5


def stop_loss_triggered(account_name, best_bid, entry_price):
    """F1 keeps its -0.3% rule; F2's fixed 30s model target has no early stop."""
    return account_name == 'F1_rules' and float(best_bid) <= float(entry_price) * 0.997


def exit_reason(account_name, held_seconds, negative_flow_evaluations):
    """Keep model-gated F2 exits aligned with its fixed 30s training label."""
    if held_seconds >= 30:
        return 'MAX_HOLD_30S'
    if account_name == 'F1_rules' and negative_flow_evaluations >= 2:
        return 'FLOW_REVERSAL'
    return None


def stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


class Account:
    def __init__(self, name):
        self.name, self.cash = name, CAPITAL
        self.position = None
        self.pending = None
        self.closed = []
        self.fees, self.slippage = Decimal(0), Decimal(0)
        self.cooldown = 0.0
        self.entries = 0
        self.signals = 0
        self.rejected = 0
        self.day_limit = False
        self.peak, self.max_drawdown = CAPITAL, Decimal(0)

    def equity(self, book):
        if not self.position:
            return self.cash
        fill = sell_fill(book, self.position['qty']) if book else None
        return self.cash + fill['total'] if fill else None

    def observe(self, book):
        equity = self.equity(book)
        if equity is None:
            return
        self.peak = max(self.peak, equity)
        self.max_drawdown = max(self.max_drawdown, (self.peak-equity)/self.peak)
        if equity <= CAPITAL * Decimal('0.995'):
            self.day_limit = True

    def on_book(self, book, at, event_log):
        self.observe(book)
        p = self.pending
        if not p or at < p['due']:
            return
        if at - p['requested_at'] > 2:
            self.pending = None; self.rejected += 1
            event_log({'type':'order_expired', 'account':self.name, 'at':stamp(at), 'side':p['side']})
            return
        if p['side'] == 'BUY':
            equity = self.equity(book)
            budget = min(self.cash, equity * Decimal('0.05'))
            fill = buy_fill(book, budget)
            if fill is None or fill['notional'] < Decimal('5000'):
                self.pending = None; self.rejected += 1
                event_log({'type':'insufficient_depth','account':self.name,'at':stamp(at),'side':'BUY'})
                return
            self.cash -= fill['total']
            self.fees += fill['fee']; self.slippage += fill['slippage']
            self.position = {'qty':fill['qty'], 'entry_total':fill['total'], 'entry_notional':fill['notional'],
                             'entry_fee':fill['fee'], 'entry_slippage':fill['slippage'], 'entry_at':at,
                             'entry_price':fill['notional']/fill['qty'], 'prediction':p.get('prediction')}
            self.entries += 1
            event_log({'type':'paper_buy','account':self.name,'at':stamp(at),'qty':str(fill['qty']),
                       'price':str(fill['notional']/fill['qty']),'fee_krw':str(fill['fee']),
                       'slippage_krw':str(fill['slippage']),'prediction_net_pct':p.get('prediction')})
        elif self.position:
            fill = sell_fill(book, self.position['qty'])
            if fill is None:
                self.pending = None; self.rejected += 1
                event_log({'type':'insufficient_depth','account':self.name,'at':stamp(at),'side':'SELL'})
                return
            pos = self.position
            self.cash += fill['total']; self.fees += fill['fee']; self.slippage += fill['slippage']
            pnl = fill['total'] - pos['entry_total']
            trade = {'entry_at':stamp(pos['entry_at']), 'exit_at':stamp(at), 'qty':str(pos['qty']),
                     'entry_price':str(pos['entry_price']), 'exit_price':str(fill['notional']/pos['qty']),
                     'entry_fee_krw':str(pos['entry_fee']), 'exit_fee_krw':str(fill['fee']),
                     'entry_slippage_krw':str(pos['entry_slippage']), 'exit_slippage_krw':str(fill['slippage']),
                     'pnl_krw':str(pnl), 'net_return_on_trade_pct':str(pnl/pos['entry_total']*100),
                     'reason':p['reason'], 'prediction_net_pct':pos['prediction']}
            self.closed.append(trade); self.position = None; self.cooldown = at + 60
            event_log({'type':'paper_sell','account':self.name,**trade})
        self.pending = None
        self.observe(book)

    def report(self, book, fresh):
        mark = self.equity(book) if fresh else (self.cash if not self.position else None)
        realized = sum((Decimal(x['pnl_krw']) for x in self.closed), Decimal(0))
        return {'initial_krw':str(CAPITAL),'cash_krw':str(self.cash),
                'equity_after_estimated_exit_costs_krw':str(mark) if mark is not None else None,
                'net_return_pct':str((mark/CAPITAL-1)*100) if mark is not None else None,
                'realized_pnl_krw':str(realized),'fees_paid_krw':str(self.fees),
                'additional_slippage_paid_krw':str(self.slippage),'closed_trades':len(self.closed),
                'wins':sum(Decimal(x['pnl_krw'])>0 for x in self.closed),
                'losses':sum(Decimal(x['pnl_krw'])<0 for x in self.closed),
                'open_position':self.position is not None,'pending_order':self.pending is not None,
                'entries':self.entries,'signals':self.signals,'unfilled_or_expired':self.rejected,
                'max_drawdown_pct':str(self.max_drawdown*100),'daily_loss_limit_hit':self.day_limit,
                'trades':self.closed}


async def run(model_path, output, duration):
    if output.exists() and (output/'run.json').exists():
        raise RuntimeError(f'Run directory already used: {output}')
    model = json.loads(model_path.read_text(encoding='utf-8'))
    if model.get('schema') != 1 or len(model.get('coef', [])) != 7:
        raise ValueError('Invalid fitted model')
    output.mkdir(parents=True, exist_ok=True)
    write_json(output/'model.json', model)
    accounts = [Account('F1_rules'), Account('F2_rules_plus_model')]
    f = Features()
    q = asyncio.Queue(maxsize=10000)
    messages = 0; disconnects = 0; snapshots = 0; connected = False
    start = None; end = None; end_monotonic = None; consecutive = 0; negative = 0
    status = 'starting'; failure = None
    datafile = gzip.open(output/'market_events.jsonl.gz', 'wt', encoding='utf-8')
    events = (output/'decisions.jsonl').open('a', encoding='utf-8', buffering=1)

    def log(item):
        events.write(json.dumps(item, ensure_ascii=False, separators=(',',':'))+'\n')

    def pred(features):
        return model['intercept'] + sum((v-m)/s*c for v,m,s,c in zip(features,model['mean'],model['scale'],model['coef']))

    def report(now):
        fresh = bool(f.book and 0 <= now-f.book_at <= 1 and 0 <= now-f.book_exchange <= 1)
        result = {'run_id':output.name,'status':status,'mode':'PAPER', 'markets':['KRW-BTC'],
                  'started_at':stamp(start) if start else None,'ends_at':stamp(end) if end else None,
                  'as_of':stamp(now),'duration_seconds':duration,'connected':connected,
                  'last_book_received_at':stamp(f.book_at) if f.book_at else None,'fresh_book':fresh,
                  'websocket_messages':messages,'sampled_seconds':snapshots,'disconnects':disconnects,
                  'invalid_events':f.invalid,'failure':failure,'fee_pct_per_side':0.05,
                  'additional_slippage_pct_per_side':0.05,'latency_assumption_ms':500,
                  'model_holdout':model.get('holdout'),'accounts':{a.name:a.report(f.book,fresh) for a in accounts}}
        write_json(output/'report.json', result)
        return result

    async def consume():
        backoff = 1
        while True:
            try:
                async for payload, received_at in PublicQuotationClient().stream(['KRW-BTC']):
                    await q.put(('message',payload,received_at.timestamp()))
                    backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await q.put(('disconnect',str(exc),time.time()))
                await asyncio.sleep(backoff)
                backoff = min(backoff*2, 15)

    task = asyncio.create_task(consume())
    next_eval = None; next_report = time.time(); initial_deadline = time.time()+60
    try:
        while True:
            now = time.time()
            if start is None and now >= initial_deadline:
                raise RuntimeError('No valid Upbit public orderbook within 60 seconds')
            if end_monotonic is not None and time.monotonic() >= end_monotonic:
                status = 'complete'
                break
            remaining = end_monotonic-time.monotonic() if end_monotonic is not None else 0.2
            timeout = min(0.2, max(0.001, remaining))
            try:
                typ, payload, at = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                typ = None
            if typ == 'disconnect':
                connected = False; disconnects += 1
                f = Features(); consecutive = negative = 0
                for a in accounts:
                    if a.pending:
                        a.rejected += 1; a.pending = None
                log({'type':'disconnect','at':stamp(at),'detail':payload})
            elif typ == 'message':
                messages += 1
                if not connected:
                    connected = True; log({'type':'connected','at':stamp(at)})
                if payload.get('type') in ('trade','orderbook'):
                    datafile.write(json.dumps({'received_at':stamp(at),'payload':payload},separators=(',',':'))+'\n')
                    f.on_event(payload,at)
                    if start is None and f.book is not None:
                        start = time.time(); end = start+duration; end_monotonic = time.monotonic()+duration
                        status = 'running'; next_eval = start+1
                        write_json(output/'run.json', {'mode':'PAPER','started_at':stamp(start),'ends_at':stamp(end),
                                   'duration_seconds':duration,'markets':['KRW-BTC'],'model_source':str(model_path),
                                   'fee_pct_per_side':0.05,'slippage_pct_per_side':0.05,'latency_ms':500})
                        log({'type':'started','at':stamp(start),'ends_at':stamp(end)})
                        print(f'STARTED {stamp(start)} ENDS {stamp(end)}', flush=True)
                    if (payload.get('type') == 'orderbook' and f.book is not None
                            and abs(at-f.book_at)<0.001 and 0 <= time.time()-at <= 1
                            and 0 <= time.time()-f.book_exchange <= 1):
                        for a in accounts:
                            a.on_book(f.book,at,log)
                            if a.position and not a.pending:
                                if stop_loss_triggered(a.name, f.book[0]['bid_price'], a.position['entry_price']):
                                    a.pending={'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'STOP_LOSS'}
                                elif at-a.position['entry_at'] >= 30:
                                    a.pending={'side':'SELL','due':at+DELAY,'requested_at':at,'reason':'MAX_HOLD_30S'}
            now = time.time()
            if start and now >= next_eval:
                if now-next_eval > 2:
                    consecutive = negative = 0
                next_eval = now+1
                s = f.snapshot(now) if connected else None
                if s:
                    snapshots += 1
                    feat=s['features']; prediction=pred(feat)
                    candidate=feat[0]>=0.20 and feat[1]>=0.20 and feat[3]>0 and s['spread_bps']<=5 and s['ask_depth_krw']>=2500000
                    consecutive=consecutive+1 if candidate else 0
                    negative=negative+1 if feat[1]<-0.20 else 0
                    log({'type':'evaluation','at':stamp(now),'features':feat,'prediction_net_pct':prediction*100,
                         'candidate':candidate,'consecutive':consecutive,'mid':s['mid'],'spread_bps':s['spread_bps']})
                    for a in accounts:
                        a.observe(s['book'])
                        if a.position and not a.pending:
                            reason = exit_reason(a.name, now-a.position['entry_at'], negative)
                            if reason:
                                a.pending={'side':'SELL','due':now+DELAY,'requested_at':now,'reason':reason}
                        if not a.position and not a.pending and not a.day_limit and now>=a.cooldown and end_monotonic-time.monotonic()>40 and consecutive>=2:
                            a.signals += 1
                            if a.name=='F1_rules' or prediction>THRESHOLD:
                                a.pending={'side':'BUY','due':now+DELAY,'requested_at':now,'prediction':prediction*100}
                                log({'type':'signal','account':a.name,'at':stamp(now),'prediction_net_pct':prediction*100})
                else:
                    consecutive = negative = 0
            if start and end_monotonic-time.monotonic() <= 4:
                for a in accounts:
                    if a.position and not a.pending:
                        a.pending={'side':'SELL','due':now+DELAY,'requested_at':now,'reason':'END_OF_RUN'}
            if now >= next_report:
                report(now); next_report = now+15
    except BaseException as exc:
        status = 'failed'; failure = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        report(time.time())
        datafile.close(); events.close()
        print(f'{status.upper()} report={output / "report.json"}',flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration-seconds',type=float,default=3600)
    args=parser.parse_args()
    if args.duration_seconds <= 0:
        raise ValueError('duration must be positive')
    asyncio.run(run(args.model,args.output,args.duration_seconds))
