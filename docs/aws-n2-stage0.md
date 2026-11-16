# N2 AWS Stage 0

## 배포 상태

- AWS 서울 리전 `ap-northeast-2`
- CloudFormation stack / Lambda: `trading-paper-n2-stage0`
- Lambda: `python3.11`, ARM64, 512 MB, timeout 45초
- 호출 방식: 수동 invoke만
- EventBridge schedule: 없음
- 결과: [`outputs/aws/n2-stage0/deployment.json`](../outputs/aws/n2-stage0/deployment.json)

## 실행 흐름

```text
수동 Lambda 호출
  → 전용 IAM role로 SSM SecureString 1개 읽기
  → 메모리에서 NH 공식 SDK 설정
  → N2 현재가/호가 + 5분 데이터 읽기
  → 가격·시각·행 수만 응답 및 CloudWatch 로그에 기록
```

IAM은 지정한 SSM 파라미터의 `ssm:GetParameter`와 이 함수 로그 기록만 허용합니다. Lambda는 VPC/NAT를 사용하지 않고 외부 N2 API에 연결합니다. SDK 토큰 파일 캐시는 끄고 함수 메모리 안에서만 사용합니다. 로그·응답에는 앱 키, 시크릿, 토큰, 계좌 데이터, 원시 응답을 쓰지 않습니다.

## 다음 단계 전 게이트

이번 주말 응답은 장외 시세여서 매매 시뮬레이션 입력으로 쓸 수 없습니다. 다음 거래일 장중에 호가 시각의 freshness, `market_status` 코드, 5분봉 타임스탬프가 봉 시작/종료 중 무엇인지와 최신 진행봉을 확인해야 합니다. 이 Stage 0 함수는 계속 수동 읽기 전용이며, 준비한 주식 페이퍼 경로는 별도 `trading-paper-n2-paper` stack과 비활성 스케줄로 분리합니다.

후속 주식 페이퍼 경로는 기존 업비트 Lambda와 별도 `trading-paper-n2-paper` stack/function으로 배포했습니다. 장중 데이터와 고정 후보군 검증이 끝날 때까지 EventBridge 스케줄 두 개는 비활성입니다.
