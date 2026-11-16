# AWS 일주일 페이퍼 트레이딩

저장된 AWS CLI `jyp` 프로필로 서울(`ap-northeast-2`)에 배포한다. 로컬 Mac이 꺼져도 EC2의 실행은 계속된다. AWS 인증 키를 배포 파일에 포함하지 않으며, EC2는 자체 IAM 역할로 해당 실행의 코드 읽기와 결과 업로드만 수행한다.

## 구성

- Amazon Linux 2023 ARM64, EC2 `t4g.small` 한 대, 암호화된 gp3 EBS 16GB.
- 열린 인바운드 포트 없이 Systems Manager로 관리한다.
- `paper-trading.service`가 실행하고 실패 시 10초 뒤 재시작한다.
- `run-service`가 시작 시각부터 7일 뒤의 절대 종료 시각을 고정한다. 재시작으로 기간이 연장되지 않는다.
- 30초마다 JSON 체크포인트와 중간 리포트를 원자적으로 저장한다.
- 시세를 시간별 JSONL로 기록하고 지난 시간의 파일은 gzip 압축한다. 복구 때 체크포인트 이후 기록만 재생한다.
- 가상 잔고·포지션·신호·거래 원장·일일 손실 제한을 복원한다. 설정이 다르면 잔고를 초기화하지 않고 실행을 거부한다.
- 5분마다 비공개 S3에 리포트·체크포인트·완성봉·압축 시세·실행 로그를 백업한다. 현재 쓰는 비압축 JSONL은 EBS에 유지하고 시간 단위로 압축해 올린다.
- 정상 완료 후 최종 결과를 S3에 올리고 서버를 정지한다. 별도의 EventBridge Scheduler도 해당 인스턴스만 정지할 수 있게 설정한다.
- EC2 정지 후 EBS와 S3 결과는 보존한다. 저장 비용은 계속 발생한다.

2026-10-17 AWS Price List API 확인 기준 서울 `t4g.small` Linux 온디맨드 요금은 시간당 USD 0.0208, 168시간이면 USD 3.4944다. 공인 IPv4는 시간당 USD 0.005(7일 USD 0.84)이며 EBS·S3를 포함한 이번 실행은 세금 제외 대략 USD 5를 예상한다. CPU 크레딧은 Standard 모드로 설정한다. 실제 저장량, 환율, 세금에 따라 청구액은 달라진다.

공식 문서: [EC2 요금](https://aws.amazon.com/ec2/pricing/on-demand/), [공인 IPv4 요금](https://aws.amazon.com/vpc/pricing/), [EventBridge Scheduler의 AWS API 대상](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-universal.html).

## 배포와 결과 확인

```bash
python3.11 deploy/aws/deploy.py --profile jyp --region ap-northeast-2 --run-id <실행-ID>
```

배포 증거는 `outputs/aws/<실행-ID>/deployment.json`, CloudFormation 템플릿, SHA-256로 식별한 소스 묶음에 남긴다. `deployment.json`의 `InstanceId`, `Bucket`, `ResultsPrefix`로 리소스를 찾는다.

서버의 결과는 `/var/lib/trading-paper/runs/<실행-ID>/`, S3에서는 `s3://<버킷>/runs/<실행-ID>/runs/<실행-ID>/`에 저장된다. `report.md`는 사람이 읽는 중간/최종 요약이고 `report.json`은 모든 전략의 가상 거래 원장을 포함한다. 완료되면 `complete.json`이 생성된다.

```bash
aws --profile jyp --region ap-northeast-2 ssm start-session --target <인스턴스-ID>
aws --profile jyp s3 sync s3://<버킷>/runs/<실행-ID>/ outputs/aws-downloaded/<실행-ID>/
```

매수·매도 규칙은 [쉬운 설명](how-it-trades.md)을 참고한다. 서비스 재시작은 `systemctl restart paper-trading`이며, 중도 종료는 `systemctl stop paper-trading` 후 `systemctl start paper-backup.service`로 보존하고 EC2를 정지한다.
