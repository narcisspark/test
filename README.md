# Upbit Crypto Paper Trading and N2 Stock Stage 0

업비트 **공개 Quotation 시세**로 A/B/C 세 전략을 동시에 평가하는 Paper Trading 시스템입니다. 로컬 CLI는 실시간 WebSocket을 사용하고, 현재 AWS 실행은 Lambda로 1분마다 공개 봉·호가를 조회합니다. 각 전략은 동일한 입력을 받지만 각각 독립된 가상 포트폴리오와 KRW 10,000,000 초기 자금을 사용합니다.

N2 국내주식은 별도 Stage 0와 AWS 페이퍼 스택으로 구성했습니다. 자격증명은 AWS SSM SecureString에 보관하고, 수동 호출 전용 N2 Lambda에서 현재가·호가·5분봉 읽기를 확인했습니다. 주식 Lambda·S3 원장·DynamoDB 실행 잠금·KST 장중 스케줄은 서울 리전에 배포했지만, **두 스케줄과 주식 시뮬레이션은 비활성**입니다. 파일럿 watchlist는 삼성전자 005930 한 종목이고, A/B 재생·선택적 Bedrock 어댑터를 준비했습니다. 장중 데이터 게이트와 모델 권한이 확인되기 전까지 주식 자동 실행은 시작하지 않습니다. [검증 기록](docs/provider-capabilities.md) · [N2 AWS Stage 0](docs/aws-n2-stage0.md) · [주식 페이퍼 경로](docs/n2-stock-paper.md)

매수·매도 흐름은 [쉬운 설명](docs/how-it-trades.md), 현재 선택한 AWS 구성은 [1분 간격 Lambda 운영 문서](docs/aws-lambda-run.md), 대안 비교는 [AWS 비용 비교](docs/aws-cost-comparison.md)를 참고하세요.

## 안전 경계

- `PAPER` 모드 외 실행은 거부합니다.
- 업비트 시세 클라이언트는 API 키나 자격증명을 사용하지 않고 HTTP proxy 환경도 사용하지 않습니다. 별도 N2 Stage 0 도구는 SSM SecureString에서 자격증명을 읽어 읽기 전용 시세 경로만 호출합니다.
- REST 클라이언트는 `api.upbit.com`의 공개 페어 목록, 1분 캔들, 호가 조회 경로만 허용합니다.
- WebSocket은 공개 endpoint `wss://api.upbit.com/websocket/v1`과 ticker/trade/orderbook/1분봉 구독만 사용합니다.
- 업비트 Exchange/Private API는 사용하지 않습니다. N2 Stage 0도 계좌·잔고·주문·취소·정정 경로를 호출하지 않습니다. 어떤 Paper 주문도 증권사나 거래소로 전송되지 않습니다.
- 신호 다음에 수신한 최신 유효 ask/bid로만 가상 체결합니다. 호가가 없을 때는 last quote에 설정된 보수적 스프레드를 더합니다.
- stale 시세 또는 연결 중단 시 새 진입을 막고 기존 가상 포지션은 최신 가격 없이 강제 청산하지 않습니다.

## 설치 및 실행

Python 3.11 이상이 필요합니다. 업비트 WebSocket은 `websockets==16.0`, N2 읽기 전용 Stage 0은 NH 공식 `nhplug==0.4.1`을 사용합니다.

```bash
python -m pip install -e .
```

연결과 Paper run을 60초 뒤 저장:

```bash
trading-paper paper run --markets KRW-BTC --duration-seconds 60
```

시장 목록과 REST 초기 1분봉을 받아 WebSocket을 구독합니다. `--duration-seconds`를 생략하면 Ctrl-C까지 실행하고 결과를 저장합니다.

```bash
trading-paper paper run --markets KRW-BTC KRW-ETH --output-dir outputs/live-paper
```

실시간 데이터 로그를 오프라인으로 재생:

```bash
trading-paper paper replay-live --events outputs/live-paper/<run-id>/market_events.jsonl
```

독립적인 완성봉 OHLCV CSV replay 또는 명시적 DEMO:

```bash
trading-paper simulate --input path/to/ohlcv.csv
trading-paper simulate --demo --output-dir outputs/demo
```

과거 코인 1분봉으로 기준을 탐색하려면, 먼저 공개 시세를 내려받고 시간순 학습·검증·홀드아웃 평가를 실행합니다. 시작/종료 시각은 timezone을 포함하며 종료 시각은 제외됩니다.

```bash
trading-paper history download-minute \
  --market KRW-BTC \
  --start 2026-07-01T00:00:00+09:00 \
  --end 2026-10-01T00:00:00+09:00 \
  --output outputs/history/KRW-BTC-1m.csv
trading-paper learn \
  --input outputs/history/KRW-BTC-1m.csv \
  --output-dir outputs/learning/KRW-BTC
```

`learn`은 A/B/C 진입 기준 후보를 과거 60% 구간에서 고르고, 다음 20%와 마지막 20%를 별도로 확인합니다. 순수익률에서 최대낙폭을 반영해 후보를 비교합니다. 후보는 검증 구간에서 최소 10회, 미사용 홀드아웃에서 최소 20회 청산되어야 하며, 기본 비용과 수수료·슬리피지·스프레드를 2배로 높인 홀드아웃 모두에서 양의 순수익, 기준선 초과, 낙폭 제한을 통과해야 `candidate-config.json`에 반영됩니다. `learning-report.json`에는 각 구간의 기준선·후보 성과와 통과 여부를 기록합니다. 이 명령은 결과 파일만 생성하며 AWS 설정이나 활성 실행을 변경하지 않습니다.

`research-cost-filter`는 진입 시 가격대별 호가 반올림까지 포함한 추정 왕복비용 상한을 학습 구간에서 선택하는 탐색 명령입니다. 상한 후보는 0.5%, 0.75%, 1.0%, 1.5%이며 미적용 기준선과 검증·홀드아웃·2배 비용 결과를 나란히 기록합니다. 결과는 연구용이며 기본 설정이나 실시간 엔진에 필터를 적용하지 않습니다.

합성 결과는 **SYNTHETIC/DEMO — 성과 증거가 아님**으로 표시됩니다. 외부 API 접속을 확인하려면 `paper run`을 실행하며, DEMO 명령은 네트워크를 사용하지 않습니다.

## 전략

- **A 추세·모멘텀 돌파**: EMA5>EMA20, EMA20 상승, 종가>KST 세션 VWAP, 직전 6개 완성봉 고가 상향 돌파, 직전 20개 완성봉 평균 대비 거래량 1.5배.
- **B VWAP 평균회귀**: 종가가 KST 세션 VWAP보다 0.4% 이상 낮고 반등 종가가 확인된 경우.
- **C 시초 범위 돌파**: 코인은 24시간 시장이므로 KST 자정부터 15개의 1분 완성봉을 일일 opening range로 사용하고 그 고가를 종가가 상향 교차하면 후보로 냅니다.

전략과 비용·위험 매개변수는 [crypto-paper.json](config/crypto-paper.json), 오프라인 replay 설정은 [simulation.json](config/simulation.json)에 있습니다.

압축파일의 AlphaScalp RSI/볼린저 화면판·데몬 규칙을 실제 공개 시세로 A/B/C와 비교하는 별도 설정은 [실험 안내](docs/alpha-scalp-paper-experiment.md)와 [crypto-paper-alpha-scalp.json](config/crypto-paper-alpha-scalp.json)을 참고하세요.

## 입력 CSV 스키마

CSV 헤더는 아래 모든 컬럼을 포함해야 합니다. `complete=false`인 봉은 제외합니다. 시각은 ISO-8601 timezone offset 또는 `Z`를 포함해야 합니다. 누락봉은 합성하지 않습니다. 중복된 `symbol,timestamp`와 잘못된 OHLC 범위는 거부됩니다.

| 컬럼 | 형식 | 의미 |
|---|---|---|
| `timestamp` | ISO-8601 with timezone | 봉 시작 시각. 보고 시간대는 KST |
| `symbol` | text | 코인 페어 예: `KRW-BTC` |
| `market` | `KRW` | 자산 구분 |
| `open`,`high`,`low`,`close` | positive number | KRW 가격 |
| `volume` | non-negative number | 봉 동안 거래된 기초 코인 수량 |
| `complete` | boolean | 완성봉 여부. `true/false` 또는 `1/0` |
| `turnover_krw` | optional number | KRW 거래대금. 있으면 실제 거래대금으로 VWAP 계산, 없으면 typical-price 추정 |

실시간 run은 원본 WebSocket JSONL과 `completed_candles.csv`를 함께 저장합니다. JSONL에는 거래소 시각과 로컬 수신 시각을 보존해 `paper replay-live`에서 기록된 이벤트 순서대로 재생합니다.

## 비용·위험 기본 가정

- 매수/매도 수수료: 각 5bp, 슬리피지: 각 5bp. 거래소 계정별 요율 검증 전의 예시 설정입니다.
- 체결은 bid/ask를 쓰면 해당 가격에 슬리피지만 추가합니다. last-only quote는 half-spread를 추가합니다.
- KRW 최소 가상 주문 5,000원, 종목당 자산 20%, 총 노출 60%, 동시 포지션 3개, 거래 위험 0.5%, 일 손실 한도 2%, 일 진입 10회, 연속 손절 3회를 적용합니다.
- 손절 1%, 익절 2%, 최대 보유 30분은 실험 매개변수입니다.
- 코인 수량은 소수점 8자리로 계산합니다. quote 기반 가격은 유효 ask/bid에 설정 슬리피지를 반영합니다. 거래소별 실제 최소주문금액 변경과 부분 체결은 완전히 모사하지 않습니다.

## 저장 파일

`paper run`은 `outputs/live-paper/<run-id>/` 아래 `market_events.jsonl`, `completed_candles.csv`, `report.json`, `report.md`를 저장합니다. 종료 시 현금, 실현/평가 손익, 보유 페어/수량, 수수료, 가상 체결 원장, 스킵 사유, 시세 경과시간, WebSocket 연결 이벤트를 요약합니다. 연결이 계속 끊겨도 duration 또는 Ctrl-C 종료 시 결과를 저장합니다.

## 공식 Upbit 문서 확인

현재 적용한 공식 공개 API 계약과 제한은 [docs/upbit-capabilities.md](docs/upbit-capabilities.md)에 기록했습니다. 안전 경계와 미지원 사항은 [docs/architecture.md](docs/architecture.md)도 참고하세요.
