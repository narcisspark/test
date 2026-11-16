"""One-shot AWS validation of N2 quotation access; never invokes broker actions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3


def handler(event, context):
    parameter_name = os.environ["N2_CREDENTIALS_PARAMETER"]
    symbol = (event or {}).get("symbol", "005930")
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        return {"status": "rejected", "reason": "invalid_symbol"}

    try:
        value = boto3.client("ssm", region_name=os.environ["AWS_REGION"]).get_parameter(
            Name=parameter_name, WithDecryption=True
        )["Parameter"]["Value"]
        credentials = json.loads(value)
        os.environ["NHPLUG_APP_KEY"] = credentials["app_key"]
        os.environ["NHPLUG_APP_SECRET"] = credentials["app_secret"]
        credentials.clear()
        del value
        os.environ["NHPLUG_BASE_URL"] = "https://api.n2plug.com:8443"
        os.environ["NHPLUG_AUTH_URL"] = "https://api.n2plug.com:8443"
        os.environ["NHPLUG_INSTRUMENTS_BASE"] = "https://www.n2plug.com/instruments"
        os.environ["NHPLUG_TOKEN_CACHE"] = "0"

        from trading_paper.providers.n2_readonly import current_price, period_five_minute

        quote = current_price(symbol)["Output_0"]
        period = period_five_minute(symbol)
        rows = period.get("Output_1") or []
        if not isinstance(rows, list):
            raise RuntimeError("unexpected_period_rows")
        stamps = [
            f"{row.get('bsop_date')}T{row.get('bsop_time')}"
            for row in rows
            if isinstance(row, dict) and row.get("bsop_date") and row.get("bsop_time")
        ]
        now = datetime.now(timezone.utc)
        result = {
            "status": "read_only_data_received",
            "checked_at_utc": now.isoformat(),
            "checked_at_kst": now.astimezone(ZoneInfo("Asia/Seoul")).isoformat(),
            "symbol": symbol,
            "market": "KRX",
            "price_krw": str(quote.get("stck_prpr")),
            "bid_1_krw": str(quote.get("bidp1")) if quote.get("bidp1") is not None else None,
            "ask_1_krw": str(quote.get("askp1")) if quote.get("askp1") is not None else None,
            "quote_time": quote.get("hoga_bsop_hour"),
            "market_status": quote.get("market_status"),
            "period_5m_rows": len(rows),
            "period_5m_first_timestamp": stamps[0] if stamps else None,
            "period_5m_last_timestamp": stamps[-1] if stamps else None,
            "bar_completion_verified": False,
            "orders_called": False,
            "accounts_or_balance_called": False,
        }
        print(json.dumps(result, ensure_ascii=False))
        return result
    except Exception as exc:
        result = {"status": "validation_failed", "error_type": type(exc).__name__}
        print(json.dumps(result))
        return result
    finally:
        os.environ.pop("NHPLUG_APP_KEY", None)
        os.environ.pop("NHPLUG_APP_SECRET", None)
