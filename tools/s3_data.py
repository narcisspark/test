#!/usr/bin/env python3.11
"""Upload / download research data to the jyp account's S3 bucket so it survives ephemeral sessions.

Only the jyp account (380821404749) is allowed; anything else is refused.

Usage: python3.11 tools/s3_data.py check
       python3.11 tools/s3_data.py up   data/upbit5m data/upbit5m      (local dir -> s3 prefix)
       python3.11 tools/s3_data.py down data/upbit5m data/upbit5m      (s3 prefix -> local dir)
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

BUCKET = "trading-paper-archive-380821404749-ap-northeast-2"
JYP_ACCOUNT = "380821404749"
REGION = "ap-northeast-2"


def client():
    s = boto3.Session(profile_name="jyp", region_name=REGION) if "jyp" in boto3.Session().available_profiles else boto3.Session(region_name=REGION)
    acct = s.client("sts").get_caller_identity()["Account"]
    if acct != JYP_ACCOUNT:
        raise SystemExit(f"refusing: account {acct} is not the jyp account")
    return s.client("s3")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    s3 = client()
    if cmd == "check":
        print("ok: jyp account reachable")
        return
    local, prefix = Path(sys.argv[2]), sys.argv[3].strip("/") + "/"
    if cmd == "up":
        files = sorted(p for p in local.rglob("*") if p.is_file())
        for p in files:
            s3.upload_file(str(p), BUCKET, prefix + p.relative_to(local).as_posix())
        print(f"uploaded {len(files)} files to s3://{BUCKET}/{prefix}")
    elif cmd == "down":
        n = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            for o in page.get("Contents", []):
                dst = local / o["Key"][len(prefix):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(BUCKET, o["Key"], str(dst)); n += 1
        print(f"downloaded {n} files from s3://{BUCKET}/{prefix}")
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
