"""Fail-closed one-minute N2 stock paper cycle.

The function only reads N2 quotation APIs. Its default configuration is
disabled and the data gates must be explicitly validated before it will read
credentials or market data. No broker account or order endpoint is implemented.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")
INTERVAL = timedelta(minutes=5)
LOCK_TTL_SECONDS = 180
STATE_VERSION = 1


def handler(event, context):
    """One EventBridge cycle; safely exits while deployment gates are closed."""
    config = _load_config()
    reasons = _gate_reasons(config)
    if reasons:
        result = {"status": "blocked_by_data_gate", "reasons": reasons,
                  "market_api_calls": 0, "orders_called": False}
        print(json.dumps(result, ensure_ascii=False))
        return result

    now = datetime.now(KST)
    owner = str(uuid.uuid4())
    dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
    lease = dynamodb.Table(os.environ["LOCK_TABLE"])
    now_epoch = int(now.timestamp())
    if not _acquire_lease(lease, owner, now_epoch):
        return {"status": "duplicate_cycle_skipped", "orders_called": False}
    try:
        result = _run_cycle(config, now)
        print(json.dumps({key: result.get(key) for key in (
            "status", "checked_at", "symbols", "completed_bars", "rule_signal_count",
            "fills", "market_api_calls", "orders_called")}, ensure_ascii=False))
        return result
    except Exception as exc:
        result = {"status": "cycle_failed_closed", "error_type": type(exc).__name__,
                  "orders_called": False}
        print(json.dumps(result))
        return result
    finally:
        _release_lease(lease, owner)


def _load_config() -> dict:
    path = Path(os.environ.get("STOCK_CONFIG", "/var/task/config/n2-stock-paper.json"))
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("stock config must be a JSON object")
    return value


def _gate_reasons(config: dict) -> list[str]:
    reasons: list[str] = []
    if config.get("mode") != "PAPER":
        reasons.append("paper_mode_required")
    if config.get("enabled") is not True:
        reasons.append("stock_paper_disabled")
    required = ("quote_freshness_verified", "market_status_codes_verified",
                "five_minute_bar_completion_verified", "instrument_master_filter_verified")
    gates = config.get("data_gate", {})
    if not isinstance(gates, dict):
        reasons.append("data_gate_invalid")
    else:
        reasons.extend(f"{name}_false" for name in required if gates.get(name) is not True)
    universe = config.get("universe", {})
    symbols = universe.get("symbols", []) if isinstance(universe, dict) else []
    verified = universe.get("verified_symbols", []) if isinstance(universe, dict) else []
    if not isinstance(symbols, list) or not symbols or len(symbols) > int(universe.get("max_symbols", 0) or 0):
        reasons.append("bounded_watchlist_required")
    elif not set(symbols).issubset(set(verified)):
        reasons.append("watchlist_not_verified_against_instrument_master")
    market_data = config.get("market_data", {})
    if not isinstance(market_data, dict):
        reasons.append("market_data_config_invalid")
    else:
        if not market_data.get("accepted_market_status_codes"):
            reasons.append("accepted_market_status_codes_missing")
        if market_data.get("bar_time_label") not in {"start", "end"}:
            reasons.append("bar_time_label_unverified")
        if not isinstance(market_data.get("complete_bar_lag_seconds"), int):
            reasons.append("complete_bar_lag_missing")
    if config.get("market") != "KRX" or int(config.get("bar_interval_minutes", 0)) != 5:
        reasons.append("unsupported_market_or_interval")
    return reasons


def _acquire_lease(table, owner: str, now_epoch: int) -> bool:
    try:
        table.put_item(
            Item={"pk": "n2-stock-minute-cycle", "owner": owner,
                  "lease_until": now_epoch + LOCK_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(pk) OR lease_until < :now",
            ExpressionAttributeValues={":now": now_epoch},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _release_lease(table, owner: str) -> None:
    try:
        table.delete_item(Key={"pk": "n2-stock-minute-cycle"},
                          ConditionExpression="#owner = :owner",
                          ExpressionAttributeNames={"#owner": "owner"},
                          ExpressionAttributeValues={":owner": owner})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _run_cycle(config: dict, now: datetime) -> dict:
    state = _load_state()
    started_at = state.get("started_at") or now.isoformat()
    deadline = state.get("deadline") or (now + timedelta(days=int(config.get("run_days", 7)))).isoformat()
    if now >= datetime.fromisoformat(deadline).astimezone(KST):
        simulation = state.get("last_simulation")
        trades = simulation.get("trades", []) if isinstance(simulation, dict) else []
        final = {"status": "complete", "mode": "PAPER", "run_id": os.environ["RUN_ID"],
                 "started_at": started_at, "deadline": deadline, "checked_at": now.isoformat(),
                 "simulation": simulation,
                 "fills": {name: sum(1 for trade in trades if trade.get("strategy") == name)
                           for name in ("A", "B")},
                 "trades": trades, "orders_called": False,
                 "accounts_or_balance_called": False}
        _save_report(final)
        return {key: final[key] for key in ("status", "checked_at", "started_at", "deadline", "orders_called")}
    if now.weekday() >= 5 or not time(9, 0) <= now.time().replace(tzinfo=None) <= time(15, 30):
        return {"status": "market_session_closed", "checked_at": now.isoformat(),
                "market_api_calls": 0, "orders_called": False}

    parameter = boto3.client("ssm", region_name=os.environ["AWS_REGION"]).get_parameter(
        Name=os.environ["N2_CREDENTIALS_PARAMETER"], WithDecryption=True
    )["Parameter"]["Value"]
    credentials = json.loads(parameter)
    os.environ["NHPLUG_APP_KEY"] = credentials["app_key"]
    os.environ["NHPLUG_APP_SECRET"] = credentials["app_secret"]
    credentials.clear()
    del parameter
    os.environ["NHPLUG_BASE_URL"] = "https://api.n2plug.com:8443"
    os.environ["NHPLUG_AUTH_URL"] = "https://api.n2plug.com:8443"
    os.environ["NHPLUG_INSTRUMENTS_BASE"] = "https://www.n2plug.com/instruments"
    os.environ["NHPLUG_TOKEN_CACHE"] = "0"
    try:
        from trading_paper.providers.n2_readonly import current_price, period_five_minute
        from trading_paper.simulation.stock_engine import LLMDecision, StockBar, simulate_stock

        symbols = config["universe"]["symbols"]
        status_codes = {str(code) for code in config["market_data"]["accepted_market_status_codes"]}
        quotes: dict[str, dict] = {}
        new_bars: list[StockBar] = []
        api_calls = 0
        for symbol in symbols:
            quote_response = current_price(symbol)
            api_calls += 1
            quote = quote_response.get("Output_0")
            if not isinstance(quote, dict):
                raise ValueError("quote_output_missing")
            status = str(quote.get("market_status", ""))
            quote_age = _quote_age_seconds(quote.get("hoga_bsop_hour"), now)
            if status not in status_codes or quote_age is None or quote_age > int(config["market_data"]["quote_freshness_seconds"]):
                raise ValueError("market_quote_not_fresh_or_status_unapproved")
            quotes[symbol] = {
                "price_krw": _safe_number(quote.get("stck_prpr")),
                "bid_1_krw": _safe_number(quote.get("bidp1")),
                "ask_1_krw": _safe_number(quote.get("askp1")),
                "quote_time": quote.get("hoga_bsop_hour"),
                "market_status": status,
                "age_seconds": round(quote_age, 3),
            }

            period_response = period_five_minute(symbol)
            api_calls += 1
            rows = period_response.get("Output_1") or []
            if not isinstance(rows, list):
                raise ValueError("period_output_invalid")
            new_bars.extend(_parse_period_rows(rows, symbol, config["market_data"], now))

        bar_map = {f"{bar.symbol}|{bar.timestamp.isoformat()}": _bar_to_json(bar)
                   for bar in _state_bars(state, StockBar)}
        for bar in new_bars:
            bar_map[f"{bar.symbol}|{bar.timestamp.isoformat()}"] = _bar_to_json(bar)
        all_bars = _state_bars({"bars": list(bar_map.values())}, StockBar)
        all_bars.sort(key=lambda item: (item.timestamp, item.symbol))
        max_bars = max(1000, int(config["universe"].get("max_symbols", 5)) * 2100)
        all_bars = all_bars[-max_bars:]

        decision_payloads = state.get("llm_decisions", {})
        decisions: dict[str, LLMDecision] = {}
        for snapshot_id, payload in decision_payloads.items():
            try:
                decisions[snapshot_id] = LLMDecision.from_payload(
                    payload, snapshot_id, payload.get("symbol", ""))
            except (ValueError, TypeError, KeyError):
                continue

        simulation = simulate_stock(all_bars, config, llm_decisions=decisions) if all_bars else None
        # Optional model admission is fail-closed. The deployment's model id and
        # InvokeModel IAM grant remain absent until the user-selected model is set.
        llm = config.get("llm", {})
        if simulation and llm.get("enabled") is True and llm.get("model_id"):
            latest_by_symbol = {symbol: max((bar.timestamp for bar in all_bars if bar.symbol == symbol),
                                             default=None) for symbol in symbols}
            candidates = [signal for signal in simulation["entry_signals"]
                          if signal["snapshot_id"] not in decisions
                          and datetime.fromisoformat(signal["timestamp"]) == latest_by_symbol.get(signal["symbol"])]
            for signal in candidates[:int(llm.get("max_candidates_per_batch", 5))]:
                decision = _bedrock_decision(signal, all_bars, llm, now)
                if decision:
                    decision_payloads[signal["snapshot_id"]] = decision
                    decisions[signal["snapshot_id"]] = LLMDecision.from_payload(
                        decision, signal["snapshot_id"], signal["symbol"])
            simulation = simulate_stock(all_bars, config, llm_decisions=decisions)

        trade_rows = simulation.get("trades", []) if simulation else []
        fill_counts = {name: sum(1 for trade in trade_rows if trade.get("strategy") == name)
                       for name in ("A", "B")}
        report = {
            "status": "running",
            "mode": "PAPER",
            "run_id": os.environ["RUN_ID"],
            "checked_at": now.isoformat(),
            "started_at": started_at,
            "deadline": deadline,
            "symbols": symbols,
            "market_api_calls": api_calls,
            "quote_snapshots": quotes,
            "new_complete_bars": len(new_bars),
            "completed_bars": len(all_bars),
            "simulation": simulation,
            "fills": fill_counts,
            "trades": trade_rows,
            "orders_called": False,
            "accounts_or_balance_called": False,
        }
        new_state = {"schema_version": STATE_VERSION, "bars": [_bar_to_json(bar) for bar in all_bars],
                     "llm_decisions": decision_payloads, "started_at": started_at,
                     "deadline": deadline, "last_simulation": simulation, "updated_at": now.isoformat()}
        _save_state(new_state)
        _save_report(report)
        return {key: report[key] for key in (
            "status", "checked_at", "symbols", "completed_bars", "market_api_calls",
            "fills", "orders_called")}
    finally:
        os.environ.pop("NHPLUG_APP_KEY", None)
        os.environ.pop("NHPLUG_APP_SECRET", None)


def _parse_period_rows(rows: list, symbol: str, market_data: dict, now: datetime) -> list:
    from trading_paper.simulation.stock_engine import StockBar

    result = []
    label = market_data["bar_time_label"]
    lag = timedelta(seconds=int(market_data["complete_bar_lag_seconds"]))
    cutoff = now - lag
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_text = _digits(_first(row, "bsop_date", "stck_bsop_date"))
        time_text = _digits(_first(row, "bsop_time", "stck_cntg_hour"))
        if len(date_text) != 8 or len(time_text) != 6:
            raise ValueError("period_timestamp_schema_unknown")
        timestamp = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        if label == "start":
            timestamp += INTERVAL
        if timestamp.date() != now.date() or timestamp > cutoff:
            continue
        open_price = _decimal(_first(row, "oprc", "stck_oprc"))
        high = _decimal(_first(row, "hgpr", "stck_hgpr"))
        low = _decimal(_first(row, "lwpr", "stck_lwpr"))
        close = _decimal(_first(row, "prpr", "stck_prpr"))
        volume = _decimal(_first(row, "vol", "cntg_vol", "acml_vol"))
        turnover = _optional_decimal(_first(row, "tr_pbmn", "acml_tr_pbmn"))
        result.append(StockBar(timestamp, symbol, "KRX", open_price, high, low, close,
                               volume, turnover, True))
    return result


def _bedrock_decision(signal: dict, bars: list, llm: dict, now: datetime) -> dict | None:
    try:
        client = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])
        tail = [bar for bar in bars if bar.symbol == signal["symbol"]][-20:]
        recent = [{"time": item.timestamp.isoformat(), "open": str(item.open),
                   "high": str(item.high), "low": str(item.low), "close": str(item.close),
                   "volume": str(item.volume)} for item in tail]
        prompt = {
            "schema_version": "1.0", "snapshot_id": signal["snapshot_id"],
            "symbol": signal["symbol"], "rule_signal": signal["reason"],
            "recent_completed_bars": recent,
            "task": "For a paper-only experiment, decide whether to admit this existing rule signal. Return exactly one JSON object with schema_version, snapshot_id, symbol, action (ALLOW_ENTRY, WAIT, or REJECT_ENTRY), and a short reason. Do not propose another symbol, price, order, or investment advice. Default to WAIT when evidence is uncertain.",
        }
        response = client.converse(
            modelId=llm["model_id"],
            messages=[{"role": "user", "content": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            inferenceConfig={"maxTokens": 220, "temperature": 0.0},
        )
        text = "".join(block.get("text", "") for block in
                       response["output"]["message"]["content"] if isinstance(block, dict))
        payload = json.loads(text)
        if (payload.get("schema_version") != "1.0"
                or payload.get("snapshot_id") != signal["snapshot_id"]
                or payload.get("symbol") != signal["symbol"]
                or payload.get("action") not in {"ALLOW_ENTRY", "WAIT", "REJECT_ENTRY"}):
            return _wait_decision(signal, llm)
        received = datetime.now(KST)
        ttl = timedelta(minutes=int(llm.get("decision_ttl_minutes", 5)))
        return {**payload, "received_at": received.isoformat(),
                "expires_at": (received + ttl).isoformat(),
                "reason": str(payload.get("reason", ""))[:240]}
    except Exception:
        # Invalid, unavailable, or unauthorized model output never admits B and
        # is persisted as WAIT so a failing model is not called every minute.
        return _wait_decision(signal, llm)


def _wait_decision(signal: dict, llm: dict) -> dict:
    received = datetime.now(KST)
    ttl = timedelta(minutes=int(llm.get("decision_ttl_minutes", 5)))
    return {"schema_version": "1.0", "snapshot_id": signal["snapshot_id"],
            "symbol": signal["symbol"], "action": "WAIT",
            "received_at": received.isoformat(), "expires_at": (received + ttl).isoformat(),
            "reason": "LLM response invalid or unavailable; fail closed"}


def _quote_age_seconds(value, now: datetime) -> float | None:
    digits = _digits(value)
    if len(digits) != 6:
        return None
    try:
        quote_time = datetime.strptime(now.strftime("%Y%m%d") + digits, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None
    age = (now - quote_time).total_seconds()
    return age if 0 <= age <= 86400 else None


def _load_state() -> dict:
    s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
    try:
        body = s3.get_object(Bucket=os.environ["RESULT_BUCKET"],
                             Key=os.environ["RESULT_PREFIX"] + "state.json")["Body"].read()
        state = json.loads(body)
        return state if state.get("schema_version") == STATE_VERSION else {}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return {}
        raise


def _state_bars(state: dict, bar_type):
    result = []
    for item in state.get("bars", []):
        try:
            result.append(bar_type(
                timestamp=datetime.fromisoformat(item["timestamp"]), symbol=item["symbol"],
                market=item["market"], open=Decimal(item["open"]), high=Decimal(item["high"]),
                low=Decimal(item["low"]), close=Decimal(item["close"]),
                volume=Decimal(item["volume"]),
                turnover_krw=Decimal(item["turnover_krw"]) if item.get("turnover_krw") else None,
                complete=True,
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return result


def _bar_to_json(bar) -> dict:
    return {"timestamp": bar.timestamp.isoformat(), "symbol": bar.symbol, "market": bar.market,
            "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
            "volume": str(bar.volume), "turnover_krw": str(bar.turnover_krw) if bar.turnover_krw is not None else None}


def _save_state(state: dict) -> None:
    boto3.client("s3", region_name=os.environ["AWS_REGION"]).put_object(
        Bucket=os.environ["RESULT_BUCKET"], Key=os.environ["RESULT_PREFIX"] + "state.json",
        Body=json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode(),
        ContentType="application/json", ServerSideEncryption="AES256")


def _save_report(report: dict) -> None:
    boto3.client("s3", region_name=os.environ["AWS_REGION"]).put_object(
        Bucket=os.environ["RESULT_BUCKET"], Key=os.environ["RESULT_PREFIX"] + "report.json",
        Body=json.dumps(report, ensure_ascii=False, indent=2).encode(),
        ContentType="application/json", ServerSideEncryption="AES256")


def _first(row: dict, *keys: str):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    raise ValueError("period_numeric_schema_unknown")


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _decimal(value) -> Decimal:
    number = Decimal(str(value).replace(",", "").strip())
    if not number.is_finite():
        raise ValueError("non_finite_market_value")
    return number


def _optional_decimal(value) -> Decimal | None:
    try:
        return _decimal(value) if value not in (None, "") else None
    except (ValueError, ArithmeticError):
        return None


def _safe_number(value) -> str | None:
    try:
        return str(_decimal(value)) if value not in (None, "") else None
    except (ValueError, ArithmeticError):
        return None
