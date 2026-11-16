#!/usr/bin/env python3
"""Package and deploy the N2 paper cycle with its minute schedule disabled."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STACK = "trading-paper-n2-paper"
FUNCTION = "trading-paper-n2-paper"
SCHEDULE_OPEN = "trading-paper-n2-paper-open"
SCHEDULE_CLOSE = "trading-paper-n2-paper-close"
PARAMETER = "/trading-paper/n2/app-credentials"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="jyp")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--run-id", default="n2-stock-" + datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower())
    args = parser.parse_args()
    out = ROOT / "outputs/aws/n2-paper"
    out.mkdir(parents=True, exist_ok=True)
    config_path = ROOT / "config/n2-stock-paper.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    package = out / "package"
    shutil.rmtree(package, ignore_errors=True)
    package.mkdir(parents=True)

    def run(command: list[str], *, parse_json: bool = True, allow_missing: bool = False,
            allow_no_updates: bool = False):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            if allow_missing and any(word in result.stderr for word in ("does not exist", "Not Found", "(404)")):
                return None
            if allow_no_updates and "No updates are to be performed" in result.stderr:
                return {"no_updates": True}
            raise RuntimeError(f"command_failed:{Path(command[0]).name}:{result.returncode}")
        return json.loads(result.stdout) if parse_json and result.stdout.strip() else {}

    requirements = out / "requirements.txt"
    uv_cache = Path(tempfile.mkdtemp(prefix="trading-paper-n2-uv-"))
    run(["uv", "export", "--locked", "--no-dev", "--format", "requirements.txt",
         "--no-emit-project", "--no-hashes", "--output-file", str(requirements),
         "--cache-dir", str(uv_cache)], parse_json=False)
    shutil.rmtree(uv_cache, ignore_errors=True)
    run(["python3.11", "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
         "--only-binary=:all:", "--platform", "manylinux2014_aarch64", "--implementation", "cp",
         "--python-version", "311", "--abi", "cp311", "--target", str(package),
         "--requirement", str(requirements)], parse_json=False)
    for source in sorted((ROOT / "src/trading_paper").rglob("*.py")):
        if "__pycache__" not in source.parts:
            target = package / source.relative_to(ROOT / "src")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    (package / "n2_paper_handler.py").write_bytes((ROOT / "deploy/aws/n2_paper_handler.py").read_bytes())
    (package / "config").mkdir(exist_ok=True)
    (package / "config/n2-stock-paper.json").write_bytes(config_path.read_bytes())

    archive = out / "lambda.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for source in sorted(package.rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts and not source.name.endswith(".pyc"):
                bundle.write(source, source.relative_to(package))
    release_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()

    aws = ["aws", "--profile", args.profile, "--region", args.region]
    account = run(aws + ["sts", "get-caller-identity"])["Account"]
    bucket = f"trading-paper-{account}-{args.region}"
    prefix = f"n2-paper/runs/{args.run_id}/"
    release_key = f"releases/n2-paper/{release_hash}.zip"
    run(aws + ["s3api", "put-object", "--bucket", bucket, "--key", release_key,
               "--body", str(archive), "--server-side-encryption", "AES256"])

    tags = [{"Key": "Project", "Value": "trading-paper-system"},
            {"Key": "Purpose", "Value": "N2-paper-only"}]
    lambda_permissions = [
        {"Effect": "Allow", "Action": "ssm:GetParameter",
         "Resource": f"arn:aws:ssm:{args.region}:{account}:parameter{PARAMETER}"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{bucket}/{prefix}*"},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
         "Resource": {"Fn::GetAtt": ["LeaseTable", "Arn"]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{args.region}:{account}:log-group:/aws/lambda/{FUNCTION}:*"},
    ]
    llm = config.get("llm", {})
    if llm.get("enabled") is True:
        if not llm.get("model_id") or not llm.get("iam_resource_arn"):
            raise ValueError("Enabled Bedrock requires model_id and a precise iam_resource_arn")
        lambda_permissions.append({"Effect": "Allow", "Action": "bedrock:InvokeModel",
                                   "Resource": llm["iam_resource_arn"]})
    trust = lambda service: {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "N2 KRX stock paper cycle, durable S3 replay state, DynamoDB lease; schedule starts disabled.",
        "Resources": {
            "LogGroup": {"Type": "AWS::Logs::LogGroup", "DeletionPolicy": "Retain",
                         "Properties": {"LogGroupName": f"/aws/lambda/{FUNCTION}", "RetentionInDays": 14}},
            "LeaseTable": {"Type": "AWS::DynamoDB::Table", "Properties": {
                "BillingMode": "PAY_PER_REQUEST",
                "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
                "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                "TimeToLiveSpecification": {"AttributeName": "lease_until", "Enabled": True},
                "SSESpecification": {"SSEEnabled": True}, "Tags": tags}},
            "FunctionRole": {"Type": "AWS::IAM::Role", "Properties": {
                "AssumeRolePolicyDocument": trust("lambda.amazonaws.com"), "Tags": tags,
                "Policies": [{"PolicyName": "N2PaperReadQuoteWritePaperState", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": lambda_permissions}}]}},
            "Function": {"Type": "AWS::Lambda::Function", "DependsOn": "LogGroup", "Properties": {
                "FunctionName": FUNCTION, "Runtime": "python3.11", "Architectures": ["arm64"],
                "MemorySize": 512, "Timeout": 120, "Handler": "n2_paper_handler.handler",
                "Role": {"Fn::GetAtt": ["FunctionRole", "Arn"]},
                "Code": {"S3Bucket": bucket, "S3Key": release_key}, "Tags": tags,
                "Environment": {"Variables": {
                    "RUN_ID": args.run_id, "RESULT_BUCKET": bucket, "RESULT_PREFIX": prefix,
                    "N2_CREDENTIALS_PARAMETER": PARAMETER, "LOCK_TABLE": {"Ref": "LeaseTable"},
                    "STOCK_CONFIG": "/var/task/config/n2-stock-paper.json"}}}},
            "AsyncPolicy": {"Type": "AWS::Lambda::EventInvokeConfig", "Properties": {
                "FunctionName": {"Ref": "Function"}, "Qualifier": "$LATEST",
                "MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}},
            "SchedulerRole": {"Type": "AWS::IAM::Role", "Properties": {
                "AssumeRolePolicyDocument": trust("scheduler.amazonaws.com"), "Tags": tags,
                "Policies": [{"PolicyName": "InvokeOnlyN2PaperFunction", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                        "Action": "lambda:InvokeFunction", "Resource": {"Fn::GetAtt": ["Function", "Arn"]}}]}}]}},
            "MarketHoursOpenSchedule": {"Type": "AWS::Scheduler::Schedule", "Properties": {
                "Name": SCHEDULE_OPEN, "Description": "N2 KRX paper polling 09:00–14:59 KST; disabled until data gates pass.",
                "ScheduleExpression": "cron(* 9-14 ? * MON-FRI *)", "ScheduleExpressionTimezone": "Asia/Seoul",
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "State": "DISABLED",
                "Target": {"Arn": {"Fn::GetAtt": ["Function", "Arn"]},
                           "RoleArn": {"Fn::GetAtt": ["SchedulerRole", "Arn"]},
                           "Input": "{}", "RetryPolicy": {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}}}},
            "MarketHoursCloseSchedule": {"Type": "AWS::Scheduler::Schedule", "Properties": {
                "Name": SCHEDULE_CLOSE, "Description": "N2 KRX paper polling 15:00–15:30 KST; disabled until data gates pass.",
                "ScheduleExpression": "cron(0-30 15 ? * MON-FRI *)", "ScheduleExpressionTimezone": "Asia/Seoul",
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "State": "DISABLED",
                "Target": {"Arn": {"Fn::GetAtt": ["Function", "Arn"]},
                           "RoleArn": {"Fn::GetAtt": ["SchedulerRole", "Arn"]},
                           "Input": "{}", "RetryPolicy": {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}}}},
        },
        "Outputs": {"FunctionName": {"Value": {"Ref": "Function"}},
                    "OpenScheduleName": {"Value": {"Ref": "MarketHoursOpenSchedule"}},
                    "CloseScheduleName": {"Value": {"Ref": "MarketHoursCloseSchedule"}},
                    "LockTable": {"Value": {"Ref": "LeaseTable"}},
                    "Bucket": {"Value": bucket}, "ResultsPrefix": {"Value": prefix}},
    }
    template_path = out / "cloudformation.json"
    template_path.write_text(json.dumps(template, indent=2), encoding="utf-8")
    run(aws + ["cloudformation", "validate-template", "--template-body", f"file://{template_path}"])
    existing = run(aws + ["cloudformation", "describe-stacks", "--stack-name", STACK], allow_missing=True)
    if existing:
        update = run(aws + ["cloudformation", "update-stack", "--stack-name", STACK,
                            "--template-body", f"file://{template_path}", "--capabilities", "CAPABILITY_IAM"],
                     allow_no_updates=True)
        waiter = None if update.get("no_updates") else "stack-update-complete"
    else:
        run(aws + ["cloudformation", "create-stack", "--stack-name", STACK,
                   "--template-body", f"file://{template_path}", "--capabilities", "CAPABILITY_IAM"])
        waiter = "stack-create-complete"
    if waiter:
        run(aws + ["cloudformation", "wait", waiter, "--stack-name", STACK], parse_json=False)
    info = run(aws + ["cloudformation", "describe-stacks", "--stack-name", STACK])["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in info["Outputs"]}
    schedules = [run(aws + ["scheduler", "get-schedule", "--name", name])
                 for name in (SCHEDULE_OPEN, SCHEDULE_CLOSE)]
    manifest = {
        "stack": STACK, "function": FUNCTION, "profile": args.profile,
        "region": args.region, "runtime": "python3.11-arm64", "run_id": args.run_id,
        "release_sha256": release_hash, "config_sha256": config_hash,
        "credential_parameter": PARAMETER, "schedule_names": [SCHEDULE_OPEN, SCHEDULE_CLOSE],
        "schedule_states": [schedule["State"] for schedule in schedules],
        "schedule_expressions": [schedule["ScheduleExpression"] for schedule in schedules],
        "market_api_invoked_during_deploy": False, "orders_called": False,
        "stack_status": info["StackStatus"], **outputs,
    }
    (out / "deployment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
