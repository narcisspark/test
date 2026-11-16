# N2 PLUG 국내주식 API capability 확인 기록

업비트 KRW 코인 페이퍼 실행은 기존 AWS 경로에서 계속 운영됩니다. N2 국내주식은 별도 읽기 전용 Stage 0 경로로 추가했습니다. 2026-10-17에 인증, 현재가·호가, 분봉, 종목마스터를 확인했지만 장중 시각 해석이 끝나지 않아 N2 페이퍼 신규 진입은 아직 비활성입니다.

**마지막 확인:** 2026-10-17 13:37 KST (AWS Lambda)  
**SDK:** `nhplug==0.4.1`  
**자격증명 보관:** AWS 서울 리전 SSM Parameter Store `SecureString`, `/trading-paper/n2/app-credentials`  
**기계 판독 상태:** [provider-capabilities.json](../src/trading_paper/data/provider-capabilities.json)

## Stage 0 결과

| 기능 | 확인 결과 | 현재 제한 |
|---|---|---|
| 인증 | N2 운영 API에서 토큰 발급 후 현재가와 분봉 조회 성공 | 계정 권한은 시세 읽기 외에 확인하지 않음 |
| 현재가·호가 | 005930 현재가 217,000원, 매수 216,500원, 매도 217,000원과 잔량 수신 | 토요일 장외 조회. `hoga_bsop_hour=20:00:00`, `market_status=X`; 신선한 장중 시세로 사용 불가 |
| 5분봉 | 2026-10-16 KRX 정규장 76행 수신. 시간 내림차순 `15:30:00`부터 `09:10:00` | `bsop_time`이 봉 시작인지 종료인지와 장중 진행봉 완성 여부는 명세에 없어 진입 신호에 사용하지 않음 |
| 종목마스터 | N2 `m_new_stock.mst` 4,308행 파싱 및 005930 코드 확인 | 후보군 구성·제외 규칙과 마스터 기준일을 각 실행에 저장해야 함 |
| 랭킹·시장상태 | `common`과 `krstock` OpenAPI에서 종목 랭킹 경로를 찾지 못함. 현재가/마스터에 상태 필드는 있음 | 고정 watchlist 대안과 상태코드 차단 규칙 미확정 |

검증 응답에는 비밀정보를 남기지 않았습니다. 로컬 검증은 [quote-validation.json](../outputs/n2-stage0/quote-validation.json), AWS Lambda 결과는 [deployment.json](../outputs/aws/n2-stage0/deployment.json)입니다. AWS 함수 `trading-paper-n2-stage0`은 수동 호출 전용이며 EventBridge 스케줄을 만들지 않았습니다. 두 API OpenAPI 명세의 SHA-256은 각각 `krstock e485ca6283eea4760f631b5332e36aaf066a561fc5b85ab0237ddb1a4e844a1e`, `common c0ce2676cdae92608f8f2a56567f80b99cedb537a07c61d3361eb5fdcc6e2df8`입니다. 종목마스터 구조체·자료 해시는 결과 파일에 기록했습니다.

## 기능별 상태

`partial`은 일부 문서/런타임 근거가 있으나 페이퍼 진입에 필요한 운영 조건이 전부 검증되지 않았다는 뜻입니다.

| ID | 상태 | 확인된 범위 | 남은 진입 차단 요건 |
|---|---|---|---|
| `auth` | partial | N2 운영 토큰 발급 및 시세 API 인증 성공 | 계정별 권한·키 유지 정책 |
| `instrument_master` | partial | `m_new_stock.mst` 다운로드·파싱 및 상태 필드 확인 | 후보 universe, 갱신/버전 저장, ETF·우선주 제외 규칙 |
| `current_quote` | partial | 가격·최우선 호가/잔량·호가시각·장상태 필드와 실제 응답 확인 | 장중 freshness·날짜 부여·`market_status` 코드 해석 |
| `orderbook` | partial | 현재가 응답에 10단계 호가와 잔량이 포함되고 1단계 응답 확인 | 장중 호가 freshness 및 0 잔량 처리 |
| `intraday_bars` | partial | 공식 `period` 경로에서 5분봉 스키마와 전일 76행 확인 | 시각 라벨과 완성봉 판정, 기본 건수·연속조회 |
| `daily_bars` | partial | 공식 `currentDaily`와 `period` 일봉 스키마 존재 | 조정주가 여부, 기간/단위·당일 완성 기준 |
| `ranking_search` | unverified | 확인한 N2 `common`·`krstock` API에 순위 경로 없음 | 제한 watchlist 대안 확정 및 범위 표시 |
| `market_status` | partial | 마스터의 거래정지/관리/투자유의 필드, 현재가의 장상태·VI 필드 | 코드표, 지연, VI 해제 후 대기 규칙 |
| `rate_limits` | partial | 공식 SDK throttle 기본 4 req/s, 상한 5 req/s 안내 | 서버 계정/TR 제한과 Lambda 공용 호출 예산 |
| `fees_tax` | partial | N2 온라인 수수료 적용이라는 공식 서비스 안내 | 계좌 수수료·세금·절사 규칙을 Paper 설정으로 고정 |
| `connectivity_policy` | unverified | 로컬 환경에서 N2 API 연결 확인 | AWS outbound·고정 IP·키 유지 정책 |
| `news_disclosures` | unverified | 아직 사용하지 않음 | MVP 제외 여부·제공 권리·비용 |

## 안전 경계와 다음 작업

- 이번 검증은 `POST /krstock/quote/v1/currentPrice`와 `POST /krstock/quote/v1/period`만 호출했습니다. 계좌·잔고·주문·취소·정정 API는 부르지 않았습니다.
- 인증정보는 SSM SecureString에서 검증 프로세스 메모리로만 읽습니다. 소스·로그·결과에 키, 시크릿, 토큰, Authorization 헤더를 저장하지 않습니다.
- 장중 표본으로 호가 신선도, `market_status`, 5분봉 타임스탬프와 진행봉 처리를 확인한 뒤에만 signal path에 연결합니다. 그 전에는 stale/unknown을 신규 진입 거부로 처리합니다.
- N2 주식 경로는 기존 업비트 AWS Lambda와 분리되어 있습니다. 오프라인 A/B 재생 엔진, 선택적 Bedrock 호출기, S3 상태·보고서 저장, DynamoDB 중복 실행 잠금, KST EventBridge 스케줄을 구현하고 `trading-paper-n2-paper` stack으로 배포했습니다. 두 스케줄은 `DISABLED`이고 네 데이터 검증 게이트도 닫혀 있습니다. 다음 거래일의 장중 검증 전에는 자동 시세 호출이나 주식 페이퍼 진입이 없습니다. Bedrock은 기본 비활성이고 모델 ID/IAM 권한도 설정하지 않았습니다.
- 실거래 주문·취소·정정은 구현하지 않습니다. 종목마스터나 현재가만으로 주문 가능 상태를 추정하지 않습니다.

## 공식 자료

- [N2 PLUG 서비스 안내](https://www.n2plug.com/about-openapi)
- [공식 NH PLUG Python SDK](https://github.com/PLUG-OpenAPI/nhplug-sdk)
- [N2 공통 OpenAPI](https://www.n2plug.com/openapi-docs/common/openapi.json)
- [N2 국내주식 OpenAPI](https://www.n2plug.com/openapi-docs/krstock/openapi.json)
- [N2 국내 종목마스터 구조체](https://www.n2plug.com/instruments/m_new_stock.h)
