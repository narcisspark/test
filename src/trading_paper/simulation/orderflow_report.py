"""Render the running F experiment report and stop watching at completion."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import time
from zoneinfo import ZoneInfo


def local(value):
    if not value:
        return '-'
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')


def money(value):
    return f'{float(value):,.2f}' if value is not None else '시세 부족으로 미평가'


def render(data):
    lines = ['# F 전략 1시간 모의매매', '',
             f"상태: **{data['status']}** · 기준 시각: {local(data['as_of'])}",
             f"시작: {local(data['started_at'])} · 예정 종료: {local(data['ends_at'])}",
             '가상자금: 전략별 10,000,000원 · 대상: BTC/KRW · 실제 주문 없음', '',
             '| 전략 | 평가액(원) | 수익률(%) | 실현 손익(원) | 수수료(원) | 추가 슬리피지(원) | 종료 거래 | 보유 |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for name, a in data['accounts'].items():
        ret = f"{float(a['net_return_pct']):+.4f}" if a['net_return_pct'] is not None else '미평가'
        lines.append(f"| {name} | {money(a['equity_after_estimated_exit_costs_krw'])} | {ret} | "
                     f"{money(a['realized_pnl_krw'])} | {money(a['fees_paid_krw'])} | "
                     f"{money(a['additional_slippage_paid_krw'])} | {a['closed_trades']} | "
                     f"{'있음' if a['open_position'] else '없음'} |")
    lines += ['', f"연결: {data['connected']} · 최신 호가 유효: {data['fresh_book']} · "
              f"수신 메시지: {data['websocket_messages']:,} · 유효 평가: {data['sampled_seconds']:,} · "
              f"연결 끊김: {data['disconnects']}",
              '가상 체결은 매수·매도 호가 깊이, 양방향 수수료, 편도 0.05% 추가 슬리피지와 0.5초 지연을 가정한다.',
              '보유 중 평가액은 해당 시점의 예상 청산 비용을 포함하며, 유효한 호가가 없으면 표시하지 않는다.',
              'F1은 규칙만 적용하고 F2는 통계 모델의 순수익 예상 조건을 추가한다. 결과는 실거래 성과가 아니다.', '']
    for name, a in data['accounts'].items():
        lines += [f'## {name} 거래 내역', '']
        if not a['trades']:
            lines += ['종료된 거래 없음.', '']
        for trade in a['trades']:
            lines.append(f"- {local(trade['entry_at'])} 매수 → {local(trade['exit_at'])} 매도: "
                         f"{money(trade['pnl_krw'])}원, 사유 {trade['reason']}")
        if a['trades']:
            lines.append('')
    if data.get('failure'):
        lines += [f"실행 오류: {data['failure']}", '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--watch', action='store_true')
    args=parser.parse_args()
    while True:
        source=args.run_dir/'report.json'
        if source.exists():
            data=json.loads(source.read_text(encoding='utf-8'))
            target=args.run_dir/'report.md'
            tmp=target.with_suffix('.md.tmp')
            tmp.write_text(render(data),encoding='utf-8')
            tmp.replace(target)
            if not args.watch or data.get('status') in ('complete','failed'):
                print(f"Report saved: {target}",flush=True)
                return
        elif not args.watch:
            raise FileNotFoundError(source)
        time.sleep(10)


if __name__ == '__main__':
    main()
