"""Fit a frozen chronological ridge baseline from previously recorded public data."""
from __future__ import annotations

import argparse
import bisect
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
import numpy as np

from trading_paper.simulation.orderflow_features import Features, buy_fill, sell_fill


def train(source: Path, output: Path):
    paths = sorted(source.glob('*.jsonl.gz'))
    if not paths:
        raise RuntimeError('No sealed historical event files available')
    f = Features()
    snapshots, quotes = [], []
    last_second = None
    segment = 0
    skipped = 0
    for path in paths:
        print(f'Reading historical {path.name}', flush=True)
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if item.get('kind') == 'connection' and not item.get('connected'):
                        f = Features()
                        last_second = None
                        segment += 1
                        continue
                    p = item.get('payload', {})
                    if item.get('kind') != 'websocket' or p.get('type') not in ('orderbook','trade'):
                        continue
                    now = datetime.fromisoformat(item['received_at']).timestamp()
                    f.on_event(p, now)
                    if p['type'] != 'orderbook' or f.book_at != now or int(now) == last_second:
                        continue
                    last_second = int(now)
                    if quotes and now-quotes[-1]['at'] > 2:
                        segment += 1
                    quotes.append({'at':now,'book':[dict(level) for level in f.book], 'segment':segment})
                    s = f.snapshot(now)
                    if s:
                        s['segment'] = segment
                        snapshots.append(s)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    skipped += 1
    if len(snapshots) < 2000:
        raise RuntimeError(f'Insufficient valid snapshots: {len(snapshots)}')
    times = [x['at'] for x in quotes]
    if any(b < a for a,b in zip(times,times[1:])):
        raise RuntimeError('Historical receive timestamps are not monotonic')
    xs, ys, ts = [], [], []
    for s in snapshots:
        j = bisect.bisect_left(times, s['at'] + 0.5)
        if j >= len(times) or times[j] > s['at'] + 1.5:
            continue
        k = bisect.bisect_left(times, times[j] + 30.5, lo=j+1)
        if (k >= len(times) or times[k] > times[j] + 31.5
                or quotes[j]['segment'] != s['segment'] or quotes[k]['segment'] != s['segment']):
            continue
        buy = buy_fill(quotes[j]['book'], Decimal('500000'))
        if buy is None:
            continue
        sell = sell_fill(quotes[k]['book'], buy['qty'])
        if sell is None:
            continue
        xs.append(s['features']); ys.append(float(sell['total']/buy['total'] - 1)); ts.append(s['at'])
    x, y, t = np.asarray(xs), np.asarray(ys), np.asarray(ts)
    if len(y) < 1000:
        raise RuntimeError(f'Insufficient costed forward labels: {len(y)}')
    cut1, cut2 = t[int(len(t)*0.70)], t[int(len(t)*0.85)]
    train_mask = t < cut1 - 35
    val_mask = (t >= cut1 + 35) & (t < cut2 - 35)
    test_mask = t >= cut2 + 35
    if min(train_mask.sum(), val_mask.sum(), test_mask.sum()) < 100:
        raise RuntimeError('Chronological split too small after embargo')
    mu = x[train_mask].mean(axis=0)
    sigma = x[train_mask].std(axis=0)
    sigma[sigma < 1e-12] = 1
    z = (x[train_mask] - mu) / sigma
    intercept = float(y[train_mask].mean())
    alpha = 100.0
    coef = np.linalg.solve(z.T @ z + alpha*np.eye(z.shape[1]), z.T @ (y[train_mask]-intercept))
    def metrics(mask):
        pred = (x[mask]-mu)/sigma @ coef + intercept
        truth = y[mask]
        selected = pred > 0.0003
        return {'samples': int(mask.sum()), 'mae_pct': float(np.mean(np.abs(pred-truth))*100),
                'mean_actual_pct': float(np.mean(truth)*100), 'mean_prediction_pct': float(np.mean(pred)*100),
                'pred_above_threshold_count': int(selected.sum()),
                'pred_above_threshold_actual_mean_pct': float(np.mean(truth[selected])*100) if selected.any() else None}
    model = {'schema': 1, 'features': ['book_imbalance5','trade_imbalance1','trade_imbalance5',
             'microprice_bps','spread_bps','return1_bps','return5_bps'],
             'mean': mu.tolist(), 'scale': sigma.tolist(), 'coef': coef.tolist(), 'intercept': intercept,
             'ridge_alpha': alpha, 'label': '30s long return after 500ms arrival and exit delay, depth, fees and assumed slippage',
             'sample_count': len(y), 'snapshot_count': len(snapshots), 'quote_sample_count': len(quotes), 'skipped_invalid_records': skipped,
             'recorded_first_utc': datetime.fromtimestamp(float(t[0]), timezone.utc).isoformat(),
             'recorded_last_utc': datetime.fromtimestamp(float(t[-1]), timezone.utc).isoformat(),
             'train': metrics(train_mask), 'validation': metrics(val_mask), 'holdout': metrics(test_mask),
             'historical_files': [p.name for p in paths], 'decision_threshold': 0.0003}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'model': str(output), 'samples': len(y), 'train': model['train'],
                      'validation': model['validation'], 'holdout': model['holdout']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    train(args.source, args.output)
