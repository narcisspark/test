#!/usr/bin/env python3.11
"""Deploy (idempotently) or delete the volatility-breakout live paper bot on AWS (docs/vbo-live-paper.md).

Creates, all tagged project=trading-paper:
  - IAM role  trading-paper-vbo-lambda   (Lambda trust; S3 get/put on BUCKET/paper/vbo/*; CloudWatch Logs)
  - Lambda    trading-paper-vbo-paper    (python3.12, 128 MB, 60 s, code = tools/vbo_live_lambda.py)
  - Log group /aws/lambda/trading-paper-vbo-paper (14-day retention)
  - EventBridge rule trading-paper-vbo-every-5min (rate(5 minutes)) -> the Lambda
State goes to s3://BUCKET/paper/vbo/ in an existing bucket. Nothing places orders.

Usage: python3.11 tools/deploy_vbo_lambda.py [--invoke] | --delete
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import boto3

REGION = "ap-northeast-2"
BUCKET = "trading-paper-archive-380821404749-ap-northeast-2"
PREFIX = "paper/vbo/"
ROLE = "trading-paper-vbo-lambda"
FUNC = "trading-paper-vbo-paper"
RULE = "trading-paper-vbo-every-5min"
TAGS = {"project": "trading-paper", "purpose": "vbo-live-paper"}


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(Path(__file__).with_name("vbo_live_lambda.py"), "vbo_live_lambda.py")
    return buf.getvalue()


def deploy(invoke: bool) -> None:
    s = boto3.Session(region_name=REGION)
    iam, lam, ev, logs = s.client("iam"), s.client("lambda"), s.client("events"), s.client("logs")
    account = s.client("sts").get_caller_identity()["Account"]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]; print(f"role exists: {role_arn}")
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                                   Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        print(f"role created: {role_arn}")
    log_arn = f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{FUNC}"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{BUCKET}/{PREFIX}*"},
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{BUCKET}", "Condition": {"StringLike": {"s3:prefix": [f"{PREFIX}*"]}}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"{log_arn}:*"}]}
    iam.put_role_policy(RoleName=ROLE, PolicyName="vbo-paper-minimal", PolicyDocument=json.dumps(policy))
    try:
        logs.create_log_group(logGroupName=f"/aws/lambda/{FUNC}", tags=TAGS)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=f"/aws/lambda/{FUNC}", retentionInDays=14)
    env = {"Variables": {"BUCKET": BUCKET, "PREFIX": PREFIX}}
    code = _zip()
    try:
        lam.get_function(FunctionName=FUNC)
        lam.update_function_code(FunctionName=FUNC, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNC)
        lam.update_function_configuration(FunctionName=FUNC, Environment=env, Timeout=60, MemorySize=128)
        print("function updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(8):  # a new role takes a few seconds to become assumable
            try:
                lam.create_function(FunctionName=FUNC, Runtime="python3.12", Role=role_arn, Handler="vbo_live_lambda.handler",
                                    Code={"ZipFile": code}, Timeout=60, MemorySize=128, Environment=env, Tags=TAGS,
                                    Description="Upbit volatility breakout live PAPER trading (no orders)")
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)
        else:
            raise SystemExit("could not create the function (role not assumable yet?)")
        print("function created")
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNC)
    fn_arn = lam.get_function(FunctionName=FUNC)["Configuration"]["FunctionArn"]
    rule_arn = ev.put_rule(Name=RULE, ScheduleExpression="rate(5 minutes)", State="ENABLED",
                           Description="Every 5 min: VBO live paper", Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["RuleArn"]
    try:
        lam.add_permission(FunctionName=FUNC, StatementId="events-invoke", Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com", SourceArn=rule_arn)
    except lam.exceptions.ResourceConflictException:
        pass
    ev.put_targets(Rule=RULE, Targets=[{"Id": "vbo", "Arn": fn_arn}])
    print(f"schedule: {RULE} rate(5 minutes) -> {FUNC}")
    if invoke:
        r = lam.invoke(FunctionName=FUNC, LogType="Tail")
        print("test invoke:", r["StatusCode"], r.get("FunctionError"), r["Payload"].read().decode()[:500])


def delete() -> None:
    s = boto3.Session(region_name=REGION)
    iam, lam, ev, logs = s.client("iam"), s.client("lambda"), s.client("events"), s.client("logs")
    for fn, kw in ((ev.remove_targets, dict(Rule=RULE, Ids=["vbo"])), (ev.delete_rule, dict(Name=RULE)),
                   (lam.delete_function, dict(FunctionName=FUNC)), (logs.delete_log_group, dict(logGroupName=f"/aws/lambda/{FUNC}")),
                   (iam.delete_role_policy, dict(RoleName=ROLE, PolicyName="vbo-paper-minimal")), (iam.delete_role, dict(RoleName=ROLE))):
        try:
            fn(**kw); print(f"deleted: {fn.__name__} {kw}")
        except Exception as exc:
            print(f"skip {fn.__name__}: {type(exc).__name__}")
    print(f"S3 records under s3://{BUCKET}/{PREFIX} are kept.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--invoke", action="store_true", help="invoke once right after deploying")
    a = ap.parse_args()
    delete() if a.delete else deploy(a.invoke)
