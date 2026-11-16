# N2 국내주식 페이퍼 경로

## 현재 구성

- N2 시세 공급자: [n2_readonly.py](../src/trading_paper/providers/n2_readonly.py), 현재가와 5분봉 조회만 허용
- Stage 0 AWS 검증: [aws-n2-stage0.md](aws-n2-stage0.md), 수동 읽기 전용 Lambda
- AWS 페이퍼 사이클: [n2_paper_handler.py](../deploy/aws/n2_paper_handler.py), [배포 스크립트](../deploy/aws/deploy_n2_stock_paper.py)
- 오프라인/누적 재생 엔진: [stock_engine.py](../src/trading_paper/simulation/stock_engine.py)
- 기본 설정: [n2-stock-paper.json](../config/n2-stock-paper.json)

AWS 주식 페이퍼 Lambda와 S3 원장·DynamoDB 중복 호출 잠금·장중 EventBridge 스케줄 리소스를 `trading-paper-n2-paper` stack으로 배포했습니다. `trading-paper-n2-paper-open`과 `trading-paper-n2-paper-close`는 모두 `DISABLED`, config의 `enabled=false`, 네 데이터 게이트도 모두 닫혀 있어 주식 API를 자동 호출하거나 페이퍼 진입하지 않습니다. 파일럿 universe는 Stage 0에서 확인한 삼성전자 005930 한 종목입니다. 배포 기록은 [deployment.json](../outputs/aws/n2-paper/deployment.json)입니다.

## 비교 대상

- **A:** 규칙 기반 진입. 5분 완성봉 EMA5>EMA20, EMA20 상승, 종가가 세션 VWAP 위, 이전 6봉 고점 돌파, 거래량이 이전 20봉 평균의 1.5배 이상이면 신호를 냅니다.
- **B:** A와 같은 신호를 쓰며, 동일 snapshot에 대해 만료 전 `ALLOW_ENTRY` 판단이 있을 때만 가상 진입합니다. `WAIT`, `REJECT_ENTRY`, 빠진 응답, 다른 종목/snapshot, 만료 응답은 진입하지 않습니다.
- 두 계좌는 각각 1,000,000원으로 시작합니다. 주식 수량은 정수이며 종목 20%, 전체 노출 60%, 거래 위험 0.5%, 손절 1%, 익절 2%, 최대 보유 30분 제안값을 적용합니다.
- 신호가 나온 봉 안에서 체결하지 않습니다. 다음 완성봉 종가를 가상 체결 기준으로 사용합니다. 보유 중인 봉에서 손절·익절을 모두 건드린 것으로 보이면 손절을 먼저 처리합니다.

분봉 CSV는 다음 필드가 필요합니다.

```text
timestamp,symbol,market,open,high,low,close,volume,turnover_krw,complete
```

`timestamp`는 완성된 봉을 관측할 수 있게 된 시각이며 KST offset을 포함해야 합니다. N2 `bsop_time`이 이 의미와 일치하는지는 아직 검증되지 않았으므로 API 응답을 변환해 쓸 때 임의로 완료 시각을 가정하지 마세요. `complete=true`인 봉만 입력할 수 있고, 중복 `symbol,timestamp`, 음수/잘못된 OHLC, KRX 이외 시장은 거부합니다.

## 실행 인터페이스

```bash
trading-paper simulate-stock --input path/to/krx-5m.csv
```

현재 기본 config는 `enabled=false`이며 freshness, 장 상태, 완성봉, 종목마스터 필터 게이트도 닫혀 있어 이 명령은 시작 단계에서 거부합니다. 검증된 데이터와 활성 설정이 들어오기 전에는 이를 변경하지 않습니다. B의 재현 가능한 결정은 JSON 배열 파일로 `--decisions path/to/decisions.json`에 전달합니다. 동일 시각의 여러 종목은 종목코드 순서로 자본을 배정합니다.

5분 OHLCV만으로는 봉 완성 직후 LLM 응답과 다음 봉 시가 체결을 시간순으로 검증할 수 없습니다. 그래서 비교기는 신호 이후 다음 완성봉의 종가를 가상 체결 기준으로 사용하고, 체결봉의 고가·저가로 그 봉 안에서 추가 청산하지 않습니다. AWS Lambda는 KST 정규장에 1분마다 실행되도록 준비하며, 매 호출에서 받아 둔 완성 5분봉을 누적 재생합니다. 따라서 현재 구현의 보유 포지션 청산 판단도 5분봉 기준이고, 1분 호가를 이용한 조기 청산은 아직 없습니다.

Bedrock 호출 어댑터는 선택 설정으로 준비했지만 기본 `llm.enabled=false`, 모델 ID와 IAM 리소스 ARN은 비어 있습니다. 따라서 지금은 Bedrock 호출이 없고 B는 유효한 판단 없이는 진입하지 않습니다. 모델과 정확한 IAM 리소스를 정해 재배포하기 전까지 실제 A/B 동시 비교는 시작되지 않습니다.

장중 데이터가 확인된 뒤 config의 각 gate, 승인된 시장 상태 코드, 봉 시각 의미, 마스터 검증 종목을 기록하고 다시 배포합니다. [start_n2_stock_paper.py](../deploy/aws/start_n2_stock_paper.py)는 네 gate가 모두 참이고 배포 config와 일치할 때만 7일 schedule을 켭니다.

## 해석 한계

현재 수수료·세금·스프레드·슬리피지와 전체원 단위 호가 간격은 실험 가정입니다. 결과의 총 비용은 이 설정값에서 계산한 수수료·세금·가격 충격의 추정치입니다. KRX 가격구간별 호가단위, 부분체결, 시장충격, 실제 큐 위치는 모델링하지 않습니다. 이 경로의 출력은 시뮬레이션 결과이며 투자 추천이나 수익성 검증이 아닙니다.
