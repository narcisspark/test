# 서울 리전 실행 비용 비교

2026-10-17 AWS Price List API에서 조회한 `ap-northeast-2` 공개 온디맨드 가격을 사용했다. 7일은 168시간, 한 달은 30일이다. 무료 티어·크레딧·약정 할인·세금은 적용하지 않았다. S3와 로그는 BTC 한 종목의 작은 부하를 가정한 예상 범위이며 실제 사용량 측정 전이다.

| 구성 | 사양과 실행 방식 | 7일 총 예상 | 30일 총 예상 | 현재 모형과의 관계 |
|---|---|---:|---:|---|
| EC2 | t4g.small, 2 vCPU burstable, 2GB RAM, gp3 16GB, 공인 IPv4 | 약 USD 5 | 약 USD 20~22 | 연속 WebSocket 유지 |
| ECS Fargate | ARM 0.25 vCPU / 0.5GB RAM, 공인 IPv4, S3 상태 보관 | 약 USD 3~4 | 약 USD 12~15 | 연속 WebSocket 유지, 서버 관리 불필요 |
| Lambda 주기 조회 | ARM 512MB, 1분마다 실행, 회당 2~5초 가정, S3/상태 저장 | 약 USD 0.2~0.8 | 약 USD 1~3 | 분 사이의 가격 경로를 받지 않으므로 체결 모형 변경 필요 |
| Lambda 연속 수신 | ARM 512MB로 7일 분량 실행시간을 여러 호출로 분할 | 약 USD 4.2~5 | 약 USD 18~20 | 일반 Lambda 최대 15분 제약 때문에 연결 교체·중복 방지·복구 설계 추가 |

사양이 동일한 서버끼리의 벤치마크 비교는 아니다. BTC 한 종목의 작은 프로그램에 Fargate 최소 사양을 적용한 비용 비교다. Fargate를 1GB RAM으로 높이면 7일 계산 자원+IPv4 합계는 USD 3.09162이며 로그와 저장비가 별도로 조금 추가된다. NAT Gateway와 로드밸런서는 사용하지 않는 구성을 가정했다.

## 단가와 산식

- EC2 t4g.small Linux: USD 0.0208 / 시간. `168 × 0.0208 = USD 3.4944`.
- Fargate ARM vCPU: USD 0.03725 / vCPU·시간.
- Fargate ARM RAM: USD 0.00409 / GB·시간.
- Fargate 0.25 vCPU / 0.5GB: `168 × (0.25 × 0.03725 + 0.5 × 0.00409) = USD 1.90806`.
- 공인 IPv4: USD 0.005 / 시간, 7일 USD 0.84. EC2와 해당 Fargate 구성 모두 적용.
- Fargate의 기본 임시 저장공간 20GB는 별도 저장공간 비용이 없다. 재시작으로 사라지는 데이터이므로 상태와 시세를 S3로 옮기는 구현이 필요하다.
- Lambda ARM 첫 구간: USD 0.0000133334 / GB·초, 요청당 USD 0.0000002.
- 1분 간격 Lambda: 7일 10,080회. `10,080 × 0.5GB × (2~5초) × 0.0000133334 + 10,080 × 0.0000002 = USD 0.1364~0.3380` 계산 비용. 상태 저장·로그·스케줄 비용을 추가했다.
- 7일 연속 실행에 해당하는 Lambda 512MB 계산시간: `604,800초 × 0.5GB × 0.0000133334 = USD 4.03202`.
- Lambda 무료 제공량이 해당 계정에 남아 있으면 더 낮아질 수 있다. 이 계정의 남은 무료 사용량은 확인하지 않았다.

Fargate에는 임시 디스크만 있으므로 EC2용 디스크 체크포인트를 그대로 운영하면 안 된다. S3에 체크포인트와 아직 처리하지 않은 시세 조각을 먼저 저장하고, 새 작업이 시작되면 복원하는 방식으로 바꿔야 한다. 같은 실행을 두 작업이 동시에 거래하지 않게 단일 실행과 상태 소유권도 관리한다.

1분 주기 Lambda는 `EventBridge → Lambda → Upbit REST → A/B/C 계산 → S3/상태 저장` 흐름이다. 손절·익절을 분봉의 고가·저가로 판단할 수 있지만 같은 분 안에서 어느 가격을 먼저 찍었는지 알 수 없다. 현재처럼 호가가 들어오는 순간 매도하는 평가와 구분해 보고해야 한다.

실시간 WebSocket 구조를 유지하려면 ECS Fargate가 적합하다. 사용자는 비용을 줄이는 1분 주기 Lambda 방식을 선택했고, EC2 생성 중이던 스택은 삭제했다. Lambda 실행은 2026-10-17 12:30:35 KST에 시작했다. 첫 호출은 4.349초 과금, 다음 자동 호출은 1.172초 과금 및 최대 메모리 113MB로 확인했다. 짧은 초기 관측이며 7일 비용 확정치는 아니다. 비공개 S3 소스 버킷과 로컬 기록은 보존했다.

공식 출처: [EC2 가격](https://aws.amazon.com/ec2/pricing/on-demand/), [Fargate 가격](https://aws.amazon.com/fargate/pricing/), [Lambda 가격](https://aws.amazon.com/lambda/pricing/), [일반 Lambda 실행시간 제한](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html), [공인 IPv4 가격](https://aws.amazon.com/vpc/pricing/).
