#!/usr/bin/env python3
"""Deploy and invoke a one-shot N2 read-only Stage 0 Lambda (no schedule)."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PARAMETER = "/trading-paper/n2/app-credentials"
STACK = "trading-paper-n2-stage0"
FUNCTION = "trading-paper-n2-stage0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="jyp")
    parser.add_argument("--region", default="ap-northeast-2")
    args = parser.parse_args()
    out = ROOT / "outputs/aws/n2-stage0"
    out.mkdir(parents=True, exist_ok=True)
    package = out / "package"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(exist_ok=True)

    def run(command: list[str], *, parse_json: bool = True, allow_missing: bool = False,
            allow_no_updates: bool = False):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            if allow_missing and "does not exist" in result.stderr:
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
    pip_args = [
        "python3.11", "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
        "--only-binary=:all:", "--platform", "manylinux2014_aarch64",
        "--implementation", "cp", "--python-version", "311", "--abi", "cp311",
        "--target", str(package), "--requirement", str(requirements),
    ]
    run(pip_args, parse_json=False)
    for source in sorted((ROOT / "src/trading_paper").rglob("*.py")):
        if "__pycache__" not in source.parts:
            target = package / source.relative_to(ROOT / "src")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    (package / "n2_stage0_handler.py").write_bytes((ROOT / "deploy/aws/n2_stage0_handler.py").read_bytes())

    archive = out / "lambda.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for source in sorted(package.rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts and not source.name.endswith(".pyc"):
                bundle.write(source, source.relative_to(package))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    account = run(["aws", "--profile", args.profile, "--region", args.region, "sts", "get-caller-identity", "--output", "json"])["Account"]
    bucket = f"trading-paper-{account}-{args.region}"
    key = f"releases/n2-stage0/{digest}.zip"
    run(["aws", "--profile", args.profile, "--region", args.region, "s3api", "put-object",
         "--bucket", bucket, "--key", key, "--body", str(archive), "--server-side-encryption", "AES256"])

    def trust(service: str):
        return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Manual N2 read-only quote and bar validation. No EventBridge schedule or broker operations.",
        "Resources": {
            "LogGroup": {"Type": "AWS::Logs::LogGroup", "DeletionPolicy": "Retain", "Properties": {
                "LogGroupName": f"/aws/lambda/{FUNCTION}", "RetentionInDays": 14}},
            "FunctionRole": {"Type": "AWS::IAM::Role", "Properties": {
                "AssumeRolePolicyDocument": trust("lambda.amazonaws.com"),
                "Policies": [{"PolicyName": "ReadN2Stage0SecretAndWriteLogs", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": "ssm:GetParameter", "Resource": f"arn:aws:ssm:{args.region}:{account}:parameter{PARAMETER}"},
                        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"arn:aws:logs:{args.region}:{account}:log-group:/aws/lambda/{FUNCTION}:*"}
                    ]}}],
                "Tags": [{"Key": "Project", "Value": "trading-paper-system"}, {"Key": "Purpose", "Value": "N2-stage0-read-only"}]}},
            "Function": {"Type": "AWS::Lambda::Function", "DependsOn": "LogGroup", "Properties": {
                "FunctionName": FUNCTION, "Runtime": "python3.11", "Architectures": ["arm64"],
                "MemorySize": 512, "Timeout": 45, "Handler": "n2_stage0_handler.handler",
                "Role": {"Fn::GetAtt": ["FunctionRole", "Arn"]},
                "Code": {"S3Bucket": bucket, "S3Key": key},
                "Environment": {"Variables": {"N2_CREDENTIALS_PARAMETER": PARAMETER}},
                "Tags": [{"Key": "Project", "Value": "trading-paper-system"}, {"Key": "Purpose", "Value": "N2-stage0-read-only"}]}}
        },
        "Outputs": {"FunctionName": {"Value": {"Ref": "Function"}}, "RoleArn": {"Value": {"Fn::GetAtt": ["FunctionRole", "Arn"]}}}
    }
    template_path = out / "cloudformation.json"
    template_path.write_text(json.dumps(template, indent=2))
    aws_base = ["aws", "--profile", args.profile, "--region", args.region]
    run(aws_base + ["cloudformation", "validate-template", "--template-body", f"file://{template_path}"])
    existing = run(aws_base + ["cloudformation", "describe-stacks", "--stack-name", STACK], allow_missing=True)
    if existing:
        update = run(aws_base + ["cloudformation", "update-stack", "--stack-name", STACK,
                                 "--template-body", f"file://{template_path}", "--capabilities", "CAPABILITY_IAM"],
                     allow_no_updates=True)
        waiter = None if update.get("no_updates") else "stack-update-complete"
    else:
        run(aws_base + ["cloudformation", "create-stack", "--stack-name", STACK, "--template-body", f"file://{template_path}", "--capabilities", "CAPABILITY_IAM"])
        waiter = "stack-create-complete"
    if waiter:
        run(aws_base + ["cloudformation", "wait", waiter, "--stack-name", STACK], parse_json=False)
    info = run(aws_base + ["cloudformation", "describe-stacks", "--stack-name", STACK])["Stacks"][0]
    function_name = next(o["OutputValue"] for o in info["Outputs"] if o["OutputKey"] == "FunctionName")
    invocation_path = out / "invocation.json"
    meta = run(aws_base + ["lambda", "invoke", "--function-name", function_name,
                           "--cli-binary-format", "raw-in-base64-out", "--payload", "{}", str(invocation_path)])
    result = json.loads(invocation_path.read_text())
    if meta.get("FunctionError") or result.get("status") != "read_only_data_received":
        raise RuntimeError(f"lambda_validation_failed:{result.get('status', 'unknown')}")
    manifest = {
        "stack": STACK, "function": function_name, "region": args.region,
        "runtime": "python3.11-arm64", "release_sha256": digest,
        "credential_parameter": PARAMETER, "schedule_created": False,
        "result": result, "stack_status": info["StackStatus"],
    }
    (out / "deployment.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
