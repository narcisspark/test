# 모의 운영: S&P 500 monthly MA(12) via SPY, cash in T-bills

가상 자금만 사용한다(달러 기준, 세금·환율 미반영). 실제 주문은 없다. 연구: [us-trend-research.md](../../docs/us-trend-research.md).

- 시작: 2026-11-18 종가 (초기 가상자금 $10,000)
- 마지막 반영 일봉: 2026-11-19 (미국 장 마감 기준)
- 전략 평가액: $9,793.75 (-2.06%), 최대낙폭 -2.06%
- SPY 매수 후 보유: $9,793.75 (-2.06%), 최대낙폭 -2.06%
- 다음 거래일 포지션: SPY 100% (판단은 월말 종가에서만)
- 비용 가정: 편도 0.10%

## 매매 기록

| 날짜 | 동작 | SPY 수정종가 | 비고 |
|---|---|---:|---|
| 2026-11-18 | BUY | 730.20 | start: adopt current rule state |

일별 기록: [daily.csv](daily.csv) · 매매: [trades.csv](trades.csv) · 상태: [state.json](state.json)
