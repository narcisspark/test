#!/usr/bin/env python3
"""Enable the N2 stock schedule only after every in-session data gate is saved."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="jyp")
    parser.add_argument("--region", default="ap-northeast-2")
    args = parser.parse_args()
    out = ROOT / "outputs/aws/n2-paper"
    config_path = ROOT / "config/n2-stock-paper.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest_path = out / "deployment.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if config.get("enabled") is not True:
        raise SystemExit("Refusing to start: config.enabled must be true")
    required = ("quote_freshness_verified", "market_status_codes_verified",
                "five_minute_bar_completion_verified", "instrument_master_filter_verified")
    if any(config.get("data_gate", {}).get(name) is not True for name in required):
        raise SystemExit("Refusing to start: all four live N2 data gates must be verified")
    universe = config.get("universe", {})
    symbols = universe.get("symbols", [])
    if (not symbols or len(symbols) > int(universe.get("max_symbols", 0) or 0)
            or not set(symbols).issubset(set(universe.get("verified_symbols", [])))):
        raise SystemExit("Refusing to start: use a bounded watchlist verified against the N2 master")
    market_data = config.get("market_data", {})
    if (not market_data.get("accepted_market_status_codes")
            or market_data.get("bar_time_label") not in {"start", "end"}
            or not isinstance(market_data.get("complete_bar_lag_seconds"), int)):
        raise SystemExit("Refusing to start: bar completion, market status, and quote freshness rules are incomplete")
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != manifest.get("config_sha256"):
        raise SystemExit("Refusing to start: deployed config differs from local verified config; redeploy first")
    if args.profile != manifest.get("profile") or args.region != manifest.get("region"):
        raise SystemExit("Refusing to start: profile/region differ from the deployed stack")

    def aws(*parts):
        result = subprocess.run(["aws", "--profile", args.profile, "--region", args.region,
                                 *parts, "--output", "json"], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"aws_command_failed:{Path(parts[0]).name}:{result.returncode}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    schedules = {name: aws("scheduler", "get-schedule", "--name", name)
                 for name in manifest["schedule_names"]}
    if any(schedule.get("State") == "ENABLED" for schedule in schedules.values()):
        raise SystemExit("Refusing to restart or extend an already enabled schedule")
    end = datetime.now(timezone.utc) + timedelta(days=int(config.get("run_days", 7)), minutes=3)
    updated = []
    try:
        for schedule_name in manifest["schedule_names"]:
            schedule = schedules[schedule_name]
            target = {key: schedule["Target"][key]
                      for key in ("Arn", "RoleArn", "Input", "RetryPolicy")
                      if key in schedule.get("Target", {})}
            expression = ("cron(* 9-14 ? * MON-FRI *)" if schedule_name.endswith("-open")
                         else "cron(0-30 15 ? * MON-FRI *)")
            update = ["scheduler", "update-schedule", "--name", schedule_name,
                      "--schedule-expression", expression, "--schedule-expression-timezone", "Asia/Seoul",
                      "--flexible-time-window", '{"Mode":"OFF"}', "--state", "ENABLED",
                      "--end-date", end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                      "--target", json.dumps(target, separators=(",", ":"))]
            aws(*update)
            updated.append(schedule_name)
    except Exception:
        for schedule_name in updated:
            try:
                schedule = aws("scheduler", "get-schedule", "--name", schedule_name)
                target = {key: schedule["Target"][key]
                          for key in ("Arn", "RoleArn", "Input", "RetryPolicy")
                          if key in schedule.get("Target", {})}
                expression = ("cron(* 9-14 ? * MON-FRI *)" if schedule_name.endswith("-open")
                             else "cron(0-30 15 ? * MON-FRI *)")
                aws("scheduler", "update-schedule", "--name", schedule_name,
                    "--schedule-expression", expression, "--schedule-expression-timezone", "Asia/Seoul",
                    "--flexible-time-window", '{"Mode":"OFF"}', "--state", "DISABLED",
                    "--end-date", end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "--target", json.dumps(target, separators=(",", ":")))
            except Exception:
                pass
        raise
    manifest["schedule_states"] = ["ENABLED", "ENABLED"]
    manifest["schedule_end_utc"] = end.isoformat()
    manifest["llm_enabled"] = bool(config.get("llm", {}).get("enabled"))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": "schedule_enabled", "schedules": manifest["schedule_names"],
                      "symbols": symbols, "ends_at_utc": end.isoformat(),
                      "llm_enabled": manifest["llm_enabled"], "paper_only": True}, indent=2))


if __name__ == "__main__":
    main()
