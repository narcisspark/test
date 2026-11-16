"""Stage-0, read-only N2 authentication and current-price check.

Loads app credentials from SSM Parameter Store SecureString in memory, invokes
only the official SDK's currentPrice quotation endpoint, and prints a small
sanitized result. No account or order endpoint is imported or called.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _credentials(profile: str, region: str, parameter: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "aws", "ssm", "get-parameter", "--name", parameter,
            "--with-decryption", "--query", "Parameter.Value", "--output", "text",
            "--profile", profile, "--region", region,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("app_key"), str) or not isinstance(value.get("app_secret"), str):
        raise ValueError("SecureString must contain app_key and app_secret")
    if not value["app_key"] or not value["app_secret"]:
        raise ValueError("SecureString credentials are empty")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="jyp")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--parameter", default="/trading-paper/n2/app-credentials")
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--include-period-bars", action="store_true",
                        help="Also query documented KRX 5-minute bars; outputs only count and time range")
    args = parser.parse_args()

    try:
        credentials = _credentials(args.profile, args.region, args.parameter)
        os.environ["NHPLUG_APP_KEY"] = credentials["app_key"]
        os.environ["NHPLUG_APP_SECRET"] = credentials["app_secret"]
        credentials.clear()
        os.environ["NHPLUG_BASE_URL"] = "https://api.n2plug.com:8443"
        os.environ["NHPLUG_AUTH_URL"] = "https://api.n2plug.com:8443"
        os.environ["NHPLUG_INSTRUMENTS_BASE"] = "https://www.n2plug.com/instruments"
        os.environ["NHPLUG_TOKEN_CACHE"] = "0"

        from trading_paper.providers.n2_readonly import current_price, period_five_minute

        response = current_price(args.symbol)
        output = response["Output_0"]
        message = response.get("rsp_msg")
        price = Decimal(str(output["stck_prpr"]))
        def optional_decimal(field: str) -> str | None:
            value = output.get(field)
            if value in (None, ""):
                return None
            try:
                return str(Decimal(str(value)))
            except Exception:
                return None

        result = {
            "status": "quote_received",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": args.symbol,
            "market": "KRX",
            "price_krw": str(price),
            "bid_1_krw": optional_decimal("bidp1"),
            "ask_1_krw": optional_decimal("askp1"),
            "bid_1_size": optional_decimal("bidp_rsqn1"),
            "ask_1_size": optional_decimal("askp_rsqn1"),
            "quote_time": output.get("hoga_bsop_hour"),
            "market_status": output.get("market_status"),
            "business_message_present": isinstance(message, str) and bool(message.strip()),
            "orders_called": False,
        }
        if args.include_period_bars:
            period = period_five_minute(args.symbol)
            rows = period.get("Output_1") or []
            if not isinstance(rows, list):
                raise RuntimeError("N2 period response has an unexpected Output_1 type")
            stamps = [
                f"{row.get('bsop_date')}T{row.get('bsop_time')}"
                for row in rows
                if isinstance(row, dict) and row.get("bsop_date") and row.get("bsop_time")
            ]
            result["period_5m"] = {
                "status": "rows_received" if rows else "no_rows",
                "row_count": len(rows),
                "first_provider_timestamp": stamps[0] if stamps else None,
                "last_provider_timestamp": stamps[-1] if stamps else None,
                "complete_bar_semantics_verified": False,
            }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except subprocess.CalledProcessError as exc:
        error_match = re.search(r"An error occurred \(([^)]+)\)", exc.stderr or "")
        print(json.dumps({
            "status": "credential_read_failed",
            "exit_code": exc.returncode,
            "aws_error_code": error_match.group(1) if error_match else "unclassified",
        }), file=sys.stderr)
        return 2
    except Exception as exc:
        # Do not print SDK exception text or raw response bodies: they may carry
        # authentication or account context. The exception class is sufficient
        # for an initial safe validation report.
        print(json.dumps({"status": "validation_failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
