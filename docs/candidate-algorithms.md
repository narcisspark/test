# 후보 알고리즘 사전 등록 (pre-registration)

작성: 2026-11-14. 데이터가 쌓인 뒤 전부 같은 규칙으로 검증한다. **결과를 보기 전에** 후보와 파라미터를 고정해 두는 문서다. 어떤 후보도 수익이 입증된 것이 아니다.

## 검증 규약 (모든 후보 공통)

- 목적 함수는 **비용 차감 후 평균 순수익(bp)**. 승률은 제약(≥55%)일 뿐 목표가 아니다. 승률 90%여도 기대값이 음수일 수 있다.
- 체결: 30만 원어치 호가 5단계 소진 가격, 결정 후 1초 지연, 편도 수수료 0.05%(계정 실제 요율 확인 필요), 청산도 1초 지연. 같은 코인 재진입은 청산 후 60초 쿨다운.
- 날짜(UTC) 순서 60/20/20 분할(학습/검증/홀드아웃). 홀드아웃은 최종 후보 1개만 1회 열람. 같은 익절·손절 규칙의 **무작위 진입** 기준선과 비교.
- 통과 조건: 학습 거래 ≥100·승률 ≥55%·평균−1SE>0·비용 2배에서도 평균>0 → 검증 양수 → 홀드아웃 양수(비용 2배 포함), 서로 다른 거래일 ≥10.
- 시도한 조합 수를 리포트에 기록한다(현재 18신호×27청산=**486**). 조합이 늘면 우연히 통과할 확률도 늘므로, 통과해도 미래 7일 이상 라이브 모의 운영을 거친 뒤에만 신뢰한다.
- 구현 위치: [search_strategies.py](../tools/search_strategies.py). 데이터: `data/market`(업비트 30종), `data/binance_ref`(바이낸스 5종 참조).

## 근거 수준 요약

| 계열 | 근거 | 비용 후 수익 근거 | 구현 상태 |
|---|---|---|---|
| A 호가 불균형+체결 흐름 추세 (`mom`) | 호가·체결 흐름이 단기 가격을 일부 예측 | **약함.** 5bp+ 왕복 비용 후 단독 신호로는 거래 불가라는 실무 보고 | 구현 (8신호) |
| B 하락 후 반등 (`rev`) | 코인 단기 역전 효과 다수 보고. 유동성 낮은 코인에서 강하고 유동성 큰 코인은 모멘텀 | 비용 미검증 | 구현 (4신호) |
| C BTC 선행 추종 (`lead`) | BTC가 알트를 초~분 단위로 선행. 소형·비유동 코인일수록 지연 큼 | 비용 미검증, 유동성 높을 땐 약화 | 구현 (2신호) |
| D 급락 충격 후 반등 (`shock`) | 개별 코인 급락의 과잉반응, 동종 코인 동조 | 시간 단위가 길어(주 단위) 1~5분 효과는 미확인 | 구현 (2신호) |
| E 거래대금 폭증 추세 (`burst`) | 정보거래·유동성 쇼크 흐름 | VPIN류는 "예측"보다 "동행" 논쟁 | 구현 (2신호) |
| F 시간대·분 단위 패턴 | 정시·15분·5분마다 변동성 피크(선물 펀딩 연관 추정) | 방향이 아니라 **변동성** 패턴 | 진단용(필터), 미구현 |
| G 바이낸스→업비트 선행 | 거래소 간 초 단위 선행·지연 존재 | 업비트 우위 증명 논문 없음, 김치 프리미엄은 구조적·변동 | 데이터 수집 중, 신호 미구현 |
| H 지정가(메이커) 진입 | 체결 확률과 체결 후 손익(markout) 사이 역선택 | 큐 위치 모델 필요 | 미구현(2단계) |

## 계열별 정의와 고정 파라미터

공통 필터: 스프레드 ≤15bp, 유효 호가·체결 있음, 5호가로 30만 원 체결 가능. 신호는 조건이 거짓→참이 되는 순간에만 발화.

- **A `mom`**: 5호가 매수·매도 대금 불균형 ≥{0.2,0.4}, 10초 체결 흐름(매수−매도)/(합) ≥{0.2,0.5}, 60초 수익률 ≥{0.05%,0.15%}.
- **B `rev`**: 60초 수익률 ≤−{0.1%,0.2%}, 불균형 ≥{0,0.2}, 10초 흐름 ≥0(매도 압력 소진).
- **C `lead`**: BTC의 10초 수익률 ≥{0.08%,0.15%}인데 해당 코인 10초 수익률이 BTC의 30% 이하, 불균형 ≥0. 대상은 BTC 제외 전 종목.
- **D `shock`**: 300초 수익률 ≤−{0.4%,0.8%}, 10초 흐름 ≥0, 최근 10초 상승, 불균형 ≥0.
- **E `burst`**: 최근 10초 거래대금이 직전 10분 평균 10초 거래대금의 ≥{5,10}배, 10초 흐름 ≥0.5, 최근 10초 상승.
- **청산(모든 계열)**: 익절 {0.3,0.5,0.8}% × 손절 {0.3,0.5,0.8}% × 최대 보유 {60,180,300}초.

## 데이터가 쌓이면 추가로 할 것

1. **진단(전략 선택에 쓰지 않음):** 코인별·시간대(UTC)별·변동성 구간별 기대값 분해. 어디서 이기고 지는지 보는 용도. 진단에서 본 패턴으로 파라미터를 다시 맞추면 홀드아웃을 오염시키므로, 새 가설은 **새 문서 항목으로 등록하고 이후 새 데이터로** 검증한다.
2. **G 구현:** 바이낸스 BTCUSDT 중간가에 업비트 KRW-USDT를 곱해 KRW로 환산하고, 업비트 KRW-BTC와의 시차 상관(0~15초)을 측정한다. 선행이 확인되고 비용을 이길 때만 신호화한다.
3. **H 구현:** 보수적 큐 모델(내 가격에서 체결이 일어날 때만 대기열 전진)로 체결률과 체결 후 markout을 함께 본다. 체결이 쉬운 주문일수록 체결 후 손익이 나쁜 경향(역선택)에 주의.
4. **다중검정:** 시도 조합 수 N을 기준으로 최고 성과가 우연일 확률을 따로 평가(예: 날짜 블록 부트스트랩, 선택 편향 보정).
5. **현실성 점검:** 실제 계정 수수료율, 세금, 최소 주문금액, API 요청 제한, 주문 지연을 확인한다.

## 출처

- 호가 불균형·비용 후 거래 가능성 회의론: [Crypto Orderflow Alpha Report (2026-02)](https://delphicalpha.substack.com/p/crypto-orderflow-alpha-report-feb), [Hawkes LOB 예측 (arXiv 2312.16190)](https://arxiv.org/abs/2312.16190v1)
- 체결 확률과 체결 후 손익: [Fill probability (arXiv 2307.04863)](https://arxiv.org/pdf/2307.04863v2), [hftbacktest 큐 모델](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)
- 코인 단기 역전·유동성 의존: [Short-term reversal, momentum, and liquidity effects](https://aminer.org/pub/628d19b35aee126c0f369100/up-or-down-short-term-reversal-momentum-and-liquidity-effects-in-cryptocurrency), [Berkeley CDAR 동종 코인 동조](https://cdar.berkeley.edu/sites/default/files/cryptopeers_latestversion.pdf)
- BTC 선행(1분·틱): [Kurihara & Matsumoto 외 1분 간격 연구](https://arxiv.org/pdf/2205.00974), [BTC vs Cardano 틱 단위 선행 시차](https://businessperspectives.org/journals/investment-management-and-financial-innovations/issue-421/a-tick-by-tick-level-measurement-of-the-lead-lag-duration-between-cryptocurrencies-the-case-of-bitcoin-versus-cardano), [High Frequency Lead-Lag in the Bitcoin Market](https://research-api.cbs.dk/ws/portalfiles/portal/59795067/612646_High_Frequency_Lead_Lag_Relationships_in_The_Bitcoin_Market.pdf)
- 분·시간대 패턴: [Periodicity in Cryptocurrencies (Quantpedia 요약)](https://quantpedia.com/periodicity-in-cryptocurrencies-recurrent-patterns-in-volatility-and-volume/), [Petukhina 외 5분 데이터](https://www.wiwi.hu-berlin.de/de/forschung/irtg/results/discussion-papers/discussion-papers-2017-1/irtg1792dp2019-020-1.pdf)
- 주문 흐름 독성(VPIN): [Toxic Flow Segmentation](https://themicrostructurelab.substack.com/p/toxic-flow-segmentation-measuring), [2026 단기 예측 보편 표현 (arXiv 2602.00776)](https://arxiv.org/pdf/2602.00776)
- 김치 프리미엄·거래소 간: [CoinDesk](https://www.coindesk.com/kimchi-premium-reappears-retail-fomo-south-korea/) (엄밀한 선행 연구는 찾지 못함, 직접 측정 필요)

위 출처 중 블로그·서브스택·벤더 글은 동료 검토를 거치지 않았고, 대부분의 학술 결과도 거래비용 후 실행 가능 수익을 검증하지 않았다.
