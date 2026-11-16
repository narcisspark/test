#!/usr/bin/env python3
"""Deploy the minute REST polling simulation without EC2, NAT or load balancers."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', default='jyp')
    parser.add_argument('--region', default='ap-northeast-2')
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    destination = ROOT / 'outputs/aws' / args.run_id
    destination.mkdir(parents=True, exist_ok=True)

    def aws(*parts, allow_missing=False):
        r = subprocess.run(['aws', '--profile', args.profile, '--region', args.region, *parts, '--output', 'json'], capture_output=True, text=True)
        if r.returncode:
            if allow_missing and any(word in r.stderr for word in ('does not exist', 'Not Found', '(404)')):
                return None
            raise RuntimeError(r.stderr.strip())
        return json.loads(r.stdout) if r.stdout.strip() else {}

    account = aws('sts', 'get-caller-identity')['Account']
    bucket = f'trading-paper-{account}-{args.region}'
    name = 'trading-paper-' + args.run_id
    prefix = f'runs/{args.run_id}/'
    print(f'Lambda deployment: {name}; region={args.region}', flush=True)
    wheels = destination / 'wheels'
    wheels.mkdir(exist_ok=True)
    subprocess.run(['python3.11', '-m', 'pip', 'download', '--disable-pip-version-check', '--only-binary=:all:',
                    '--platform', 'manylinux2014_aarch64', '--implementation', 'cp', '--python-version', '311',
                    '--abi', 'cp311', '--no-deps', '--dest', str(wheels), 'websockets==16.0'], check=True)
    release = destination / 'lambda.zip'
    with zipfile.ZipFile(release, 'w', zipfile.ZIP_DEFLATED) as archive:
        for source in sorted((ROOT / 'src/trading_paper').rglob('*.py')):
            if '__pycache__' not in source.parts:
                archive.write(source, source.relative_to(ROOT / 'src'))
        archive.write(ROOT / 'config/crypto-paper.json', 'config/crypto-paper.json')
        for wheel in wheels.glob('*.whl'):
            with zipfile.ZipFile(wheel) as source:
                for item in source.infolist():
                    if not item.is_dir():
                        archive.writestr(item.filename, source.read(item))
    sha = hashlib.sha256(release.read_bytes()).hexdigest()
    release_key = f'releases/{args.run_id}/{sha}.zip'
    aws('s3api', 'put-object', '--bucket', bucket, '--key', release_key, '--body', str(release), '--server-side-encryption', 'AES256')
    tags = [{'Key': 'Project', 'Value': 'trading-paper-system'}, {'Key': 'RunId', 'Value': args.run_id}]
    assume = lambda service: {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}]}
    schedule_arn = f'arn:aws:scheduler:{args.region}:{account}:schedule/default/{name}'
    template = {'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'Seven-day minute-polling Upbit paper trading: Lambda, EventBridge, S3, DynamoDB lease; no EC2',
        'Parameters': {'PollingEnabled': {'Type': 'String', 'Default': 'DISABLED', 'AllowedValues': ['ENABLED', 'DISABLED']},
                       'EndAtUTC': {'Type': 'String', 'Default': (datetime.now(timezone.utc) + timedelta(days=7, hours=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}},
        'Resources': {
            'LogGroup': {'Type': 'AWS::Logs::LogGroup', 'DeletionPolicy': 'Retain', 'Properties': {'LogGroupName': '/aws/lambda/' + name, 'RetentionInDays': 14, 'Tags': tags}},
            'LeaseTable': {'Type': 'AWS::DynamoDB::Table', 'Properties': {
                'BillingMode': 'PAY_PER_REQUEST', 'AttributeDefinitions': [{'AttributeName': 'pk', 'AttributeType': 'S'}],
                'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}], 'TimeToLiveSpecification': {'AttributeName': 'lease_until', 'Enabled': True},
                'SSESpecification': {'SSEEnabled': True}, 'Tags': tags}},
            'FunctionRole': {'Type': 'AWS::IAM::Role', 'Properties': {'AssumeRolePolicyDocument': assume('lambda.amazonaws.com'), 'Tags': tags,
                'Policies': [{'PolicyName': 'OnlyThisPaperRun', 'PolicyDocument': {'Version': '2012-10-17', 'Statement': [
                    {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject'], 'Resource': f'arn:aws:s3:::{bucket}/{prefix}*'},
                    {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': f'arn:aws:s3:::{bucket}'},
                    {'Effect': 'Allow', 'Action': ['dynamodb:PutItem', 'dynamodb:DeleteItem'], 'Resource': {'Fn::GetAtt': ['LeaseTable', 'Arn']}},
                    {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'], 'Resource': f'arn:aws:logs:{args.region}:{account}:log-group:/aws/lambda/{name}:*'},
                    {'Effect': 'Allow', 'Action': 'scheduler:DeleteSchedule', 'Resource': schedule_arn}
                ]}}]}},
            'Function': {'Type': 'AWS::Lambda::Function', 'DependsOn': 'LogGroup', 'Properties': {
                'FunctionName': name, 'Runtime': 'python3.11', 'Architectures': ['arm64'], 'MemorySize': 512, 'Timeout': 120,
                'Handler': 'trading_paper.simulation.lambda_runner.handler', 'Role': {'Fn::GetAtt': ['FunctionRole', 'Arn']},
                'Code': {'S3Bucket': bucket, 'S3Key': release_key}, 'Tags': tags,
                'Environment': {'Variables': {'RUN_ID': args.run_id, 'RESULT_BUCKET': bucket, 'RESULT_PREFIX': prefix,
                    'PAPER_CONFIG': '/var/task/config/crypto-paper.json', 'SCHEDULE_NAME': name, 'LOCK_TABLE': {'Ref': 'LeaseTable'}}}}},
            'AsyncPolicy': {'Type': 'AWS::Lambda::EventInvokeConfig', 'Properties': {'FunctionName': {'Ref': 'Function'}, 'Qualifier': '$LATEST', 'MaximumEventAgeInSeconds': 60, 'MaximumRetryAttempts': 0}},
            'SchedulerRole': {'Type': 'AWS::IAM::Role', 'Properties': {'AssumeRolePolicyDocument': assume('scheduler.amazonaws.com'), 'Tags': tags,
                'Policies': [{'PolicyName': 'InvokeOnlyThisPaperFunction', 'PolicyDocument': {'Version': '2012-10-17', 'Statement': [
                    {'Effect': 'Allow', 'Action': 'lambda:InvokeFunction', 'Resource': {'Fn::GetAtt': ['Function', 'Arn']}}]}}]}},
            'MinuteSchedule': {'Type': 'AWS::Scheduler::Schedule', 'Properties': {
                'Name': name, 'Description': 'Poll public Upbit prices once per minute for seven days', 'ScheduleExpression': 'rate(1 minute)',
                'FlexibleTimeWindow': {'Mode': 'OFF'}, 'State': {'Ref': 'PollingEnabled'}, 'EndDate': {'Ref': 'EndAtUTC'},
                'Target': {'Arn': {'Fn::GetAtt': ['Function', 'Arn']}, 'RoleArn': {'Fn::GetAtt': ['SchedulerRole', 'Arn']},
                    'Input': '{"scheduled_at":"<aws.scheduler.scheduled-time>"}', 'RetryPolicy': {'MaximumEventAgeInSeconds': 60, 'MaximumRetryAttempts': 0}}}}
        }, 'Outputs': {'FunctionName': {'Value': {'Ref': 'Function'}}, 'ScheduleName': {'Value': {'Ref': 'MinuteSchedule'}},
                       'LockTable': {'Value': {'Ref': 'LeaseTable'}}, 'Bucket': {'Value': bucket}, 'ResultsPrefix': {'Value': prefix}}}
    template_path = destination / 'cloudformation.json'
    manifest_path = destination / 'deployment.json'
    existing = aws('cloudformation', 'describe-stacks', '--stack-name', name, allow_missing=True)
    if existing and existing['Stacks'][0]['StackStatus'] == 'ROLLBACK_COMPLETE':
        aws('cloudformation', 'delete-stack', '--stack-name', name)
        aws('cloudformation', 'wait', 'stack-delete-complete', '--stack-name', name)
        # Retained empty logs from this failed, never-started function conflict
        # with recreating its explicitly named log group.
        groups = aws('logs', 'describe-log-groups', '--log-group-name-prefix', '/aws/lambda/' + name)['logGroups']
        for group in groups:
            if group['logGroupName'] == '/aws/lambda/' + name:
                if group.get('storedBytes', 0):
                    raise RuntimeError('Failed deployment log group contains data; preserve it and use another run ID')
                aws('logs', 'delete-log-group', '--log-group-name', group['logGroupName'])
        existing = None
    if not existing:
        template_path.write_text(json.dumps(template, indent=2))
        aws('cloudformation', 'validate-template', '--template-body', f'file://{template_path}')
        aws('cloudformation', 'create-stack', '--stack-name', name, '--template-body', f'file://{template_path}', '--capabilities', 'CAPABILITY_IAM', '--tags', 'Key=Project,Value=trading-paper-system', f'Key=RunId,Value={args.run_id}')
        print('Creating Lambda, schedule (disabled for initial validation), S3 permissions and DynamoDB lease', flush=True)
        aws('cloudformation', 'wait', 'stack-create-complete', '--stack-name', name)
    info = aws('cloudformation', 'describe-stacks', '--stack-name', name)['Stacks'][0]
    manifest = {'run_id': args.run_id, 'stack': name, 'profile': args.profile, 'account': account, 'region': args.region,
                'architecture': 'LAMBDA_MINUTE_REST_POLLING', 'release_key': release_key, 'release_sha256': sha, 'stack_status': info['StackStatus']}
    manifest.update({item['OutputKey']: item['OutputValue'] for item in info['Outputs']})
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
