#!/usr/bin/env python3
"""Deploy a private-data, no-inbound-port EC2 paper run using the saved AWS CLI profile.

All code/artifacts and resulting resource IDs are retained locally. No credential
files, local environment, .git directory, or previous runs enter the release.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', default='jyp')
    parser.add_argument('--region', default='ap-northeast-2')
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    destination = ROOT / 'outputs' / 'aws' / args.run_id
    destination.mkdir(parents=True, exist_ok=True)

    def aws(*parts, allow_missing=False):
        process = subprocess.run(['aws', '--profile', args.profile, '--region', args.region, *parts, '--output', 'json'], capture_output=True, text=True)
        if process.returncode:
            if allow_missing and any(word in process.stderr for word in ('does not exist', 'Not Found', '(404)')):
                return None
            raise RuntimeError(process.stderr.strip())
        return json.loads(process.stdout) if process.stdout.strip() else {}

    account = aws('sts', 'get-caller-identity')['Account']
    bucket = f'trading-paper-{account}-{args.region}'
    stack = 'trading-paper-' + args.run_id.removeprefix('aws-')
    manifest_path = destination / 'deployment.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest['account'] != account:
            raise RuntimeError('Saved deployment belongs to another account')
    else:
        manifest = {'run_id': args.run_id, 'account': account, 'profile': args.profile,
                    'region': args.region, 'stack': stack, 'bucket': bucket,
                    'created_at': datetime.now(timezone.utc).isoformat()}
        manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f'Account={account} region={args.region} stack={stack}', flush=True)
    if aws('s3api', 'head-bucket', '--bucket', bucket, allow_missing=True) is None:
        aws('s3api', 'create-bucket', '--bucket', bucket, '--create-bucket-configuration', json.dumps({'LocationConstraint': args.region}))
    aws('s3api', 'put-public-access-block', '--bucket', bucket, '--public-access-block-configuration', json.dumps({key: True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}))
    aws('s3api', 'put-bucket-encryption', '--bucket', bucket, '--server-side-encryption-configuration', json.dumps({'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}))
    aws('s3api', 'put-bucket-tagging', '--bucket', bucket, '--tagging', json.dumps({'TagSet': [{'Key': 'Project', 'Value': 'trading-paper-system'}]}))
    release = destination / 'release.tar.gz'
    if not release.exists() or 'InstanceId' not in manifest:
        with tarfile.open(release, 'w:gz') as archive:
            for name in ('src', 'config', 'deploy', 'docs', 'README.md', 'pyproject.toml'):
                archive.add(ROOT / name, arcname=name, filter=lambda info: None if '__pycache__' in info.name or info.name.endswith('.pyc') else info)
    sha = hashlib.sha256(release.read_bytes()).hexdigest()
    release_key = f'releases/{args.run_id}/{sha}.tar.gz'
    aws('s3api', 'put-object', '--bucket', bucket, '--key', release_key, '--body', str(release), '--server-side-encryption', 'AES256')
    template_path = destination / 'cloudformation.json'
    if not template_path.exists() or 'InstanceId' not in manifest:
        vpc = aws('ec2', 'describe-vpcs', '--filters', 'Name=is-default,Values=true')['Vpcs'][0]['VpcId']
        subnets = aws('ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc}', 'Name=default-for-az,Values=true')['Subnets']
        subnet = sorted(subnets, key=lambda item: item['AvailabilityZone'])[0]['SubnetId']
        ami = aws('ssm', 'get-parameter', '--name', '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64')['Parameter']['Value']
        bootstrap = (ROOT / 'deploy/aws/bootstrap.sh').read_text()
        for key, value in {'BUCKET': bucket, 'RELEASE_KEY': release_key, 'REGION': args.region, 'RUN_ID': args.run_id}.items():
            bootstrap = bootstrap.replace(f'__{key}__', value)
        tags = [{'Key': 'Project', 'Value': 'trading-paper-system'}, {'Key': 'RunId', 'Value': args.run_id}]
        assume = lambda service: {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}]}
        instance_arn = {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/${PaperInstance}'}
        template = {
            'AWSTemplateFormatVersion': '2010-09-09',
            'Description': 'Seven-day Upbit public quotation paper trading, retained encrypted EBS and private S3 results',
            'Parameters': {'StopAtUTC': {'Type': 'String', 'Default': (datetime.now(timezone.utc) + timedelta(days=7, minutes=30)).strftime('%Y-%m-%dT%H:%M:%S')}},
            'Resources': {
                'PaperRole': {'Type': 'AWS::IAM::Role', 'Properties': {
                    'AssumeRolePolicyDocument': assume('ec2.amazonaws.com'),
                    'ManagedPolicyArns': ['arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'],
                    'Policies': [{'PolicyName': 'PaperArtifactsAndResults', 'PolicyDocument': {'Version': '2012-10-17', 'Statement': [
                        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': f'arn:aws:s3:::{bucket}', 'Condition': {'StringLike': {'s3:prefix': [f'releases/{args.run_id}/*', f'runs/{args.run_id}/*']}}},
                        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': f'arn:aws:s3:::{bucket}/{release_key}'},
                        {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': f'arn:aws:s3:::{bucket}/runs/{args.run_id}/*'}
                    ]}}], 'Tags': tags}},
                'PaperProfile': {'Type': 'AWS::IAM::InstanceProfile', 'Properties': {'Roles': [{'Ref': 'PaperRole'}]}},
                'PaperSecurityGroup': {'Type': 'AWS::EC2::SecurityGroup', 'Properties': {
                    'GroupDescription': 'Paper trading outbound only; administration through SSM', 'VpcId': vpc, 'SecurityGroupIngress': [],
                    'SecurityGroupEgress': [{'IpProtocol': '-1', 'CidrIp': '0.0.0.0/0'}], 'Tags': tags}},
                'PaperInstance': {'Type': 'AWS::EC2::Instance', 'Properties': {
                    'ImageId': ami, 'InstanceType': 't4g.small', 'IamInstanceProfile': {'Ref': 'PaperProfile'},
                    'NetworkInterfaces': [{'DeviceIndex': '0', 'AssociatePublicIpAddress': True, 'SubnetId': subnet, 'GroupSet': [{'Ref': 'PaperSecurityGroup'}]}],
                    'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 16, 'VolumeType': 'gp3', 'Encrypted': True, 'DeleteOnTermination': False}}],
                    'MetadataOptions': {'HttpTokens': 'required', 'HttpPutResponseHopLimit': 1},
                    'CreditSpecification': {'CPUCredits': 'standard'}, 'InstanceInitiatedShutdownBehavior': 'stop',
                    'UserData': base64.b64encode(bootstrap.encode()).decode(),
                    'Tags': tags + [{'Key': 'Name', 'Value': stack}]}},
                'StopRole': {'Type': 'AWS::IAM::Role', 'Properties': {
                    'AssumeRolePolicyDocument': assume('scheduler.amazonaws.com'),
                    'Policies': [{'PolicyName': 'StopOnlyThisPaperInstance', 'PolicyDocument': {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': 'ec2:StopInstances', 'Resource': instance_arn}]}}], 'Tags': tags}},
                'StopSchedule': {'Type': 'AWS::Scheduler::Schedule', 'Properties': {
                    'Name': stack + '-stop', 'Description': 'One-time stop safeguard for the seven-day paper run',
                    'ScheduleExpression': {'Fn::Sub': 'at(${StopAtUTC})'}, 'ScheduleExpressionTimezone': 'UTC',
                    'FlexibleTimeWindow': {'Mode': 'OFF'}, 'State': 'ENABLED',
                    'Target': {'Arn': 'arn:aws:scheduler:::aws-sdk:ec2:stopInstances', 'RoleArn': {'Fn::GetAtt': ['StopRole', 'Arn']},
                               'Input': {'Fn::Sub': '{"InstanceIds":["${PaperInstance}"]}'},
                               'RetryPolicy': {'MaximumEventAgeInSeconds': 3600, 'MaximumRetryAttempts': 3}}}}
            },
            'Outputs': {'InstanceId': {'Value': {'Ref': 'PaperInstance'}}, 'StopScheduleName': {'Value': {'Ref': 'StopSchedule'}},
                        'Bucket': {'Value': bucket}, 'ResultsPrefix': {'Value': f'runs/{args.run_id}/'}}
        }
        template_path.write_text(json.dumps(template, indent=2))
    existing = aws('cloudformation', 'describe-stacks', '--stack-name', stack, allow_missing=True)
    if existing and existing['Stacks'][0]['StackStatus'] == 'ROLLBACK_COMPLETE':
        aws('cloudformation', 'delete-stack', '--stack-name', stack)
        aws('cloudformation', 'wait', 'stack-delete-complete', '--stack-name', stack)
        existing = None
    aws('cloudformation', 'validate-template', '--template-body', f'file://{template_path}')
    if existing is None:
        aws('cloudformation', 'create-stack', '--stack-name', stack, '--template-body', f'file://{template_path}', '--capabilities', 'CAPABILITY_IAM', '--tags', 'Key=Project,Value=trading-paper-system', f'Key=RunId,Value={args.run_id}')
        print('CloudFormation creating EC2, IAM, security group and automatic stop schedule', flush=True)
    aws('cloudformation', 'wait', 'stack-create-complete', '--stack-name', stack)
    info = aws('cloudformation', 'describe-stacks', '--stack-name', stack)['Stacks'][0]
    manifest.update({item['OutputKey']: item['OutputValue'] for item in info['Outputs']})
    manifest.update({'release_key': release_key, 'release_sha256': sha, 'stack_status': info['StackStatus']})
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
