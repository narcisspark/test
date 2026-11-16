from __future__ import annotations

import asyncio
import csv
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_paper.providers.upbit_public import PublicQuotationClient
from trading_paper.simulation.live_engine import LivePaperEngine
from trading_paper.simulation.models import Bar


def run_live_paper(config: dict[str, Any], requested_markets: list[str] | None, output_dir: str | Path, duration_seconds: float | None = None) -> dict[str, Any]:
    if config.get("mode") != "PAPER":
        raise ValueError("Only PAPER mode is supported")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    destination = Path(output_dir) / run_id
    destination.mkdir(parents=True, exist_ok=True)
    raw_path = destination / "market_events.jsonl"
    candles_path = destination / "completed_candles.csv"
    report_path = destination / "report.json"
    markdown_path = destination / "report.md"
    client = PublicQuotationClient()
    all_pairs = client.list_krw_pairs()
    markets = requested_markets or list(config["default_markets"])
    markets = sorted(set(markets))
    invalid = set(markets) - set(all_pairs)
    if invalid:
        raise ValueError(f"Not currently supported safe KRW pair(s): {', '.join(sorted(invalid))}")
    engine = LivePaperEngine(config, markets, run_id=run_id)
    started_at = datetime.now(timezone.utc)
    with raw_path.open("w", encoding="utf-8") as raw_stream:
        _record(raw_stream, {"kind": "rest_market_list", "received_at": _now(), "markets": all_pairs})
        for market in markets:
            rows = client.minute_candles(market, int(config["feed"]["warmup_candles_per_market"]))
            if not rows:
                raise RuntimeError(f"No REST candle warm-up was returned for {market}")
            engine.seed_history(market, rows)
            loaded = sorted(rows, key=lambda row: row["candle_date_time_utc"])
            for row in loaded:
                _record(raw_stream, {"kind": "rest_candle_seed", "received_at": _now(), "payload": row})
        _init_candle_file(candles_path)
        print(f"LIVE PAPER connected setup: markets={','.join(markets)}; REST warm-up complete; no private API/key used.")

        async def consume() -> None:
            backoff = 1.0
            started = time.monotonic()
            while duration_seconds is None or time.monotonic() - started < duration_seconds:
                try:
                    engine.set_connection(True, datetime.now(timezone.utc), "public websocket connected")
                    async for payload, received_at in client.stream(markets):
                        _record(raw_stream, {"kind": "websocket", "received_at": received_at.isoformat(), "payload": payload})
                        raw_stream.flush()
                        if payload.get("type") == "candle.1m":
                            before = dict(engine.last_complete_at)
                            engine.on_candle(payload, received_at)
                            new_bars = [market for market, value in engine.last_complete_at.items() if before.get(market) != value]
                            for market in new_bars:
                                completed = _latest_complete_bar(engine, market)
                                if completed:
                                    _append_candle(candles_path, completed)
                        elif payload.get("type") in {"ticker", "trade", "orderbook"}:
                            engine.on_quote(payload, received_at)
                        if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                            return
                    raise RuntimeError("Public WebSocket stream closed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    engine.set_connection(False, datetime.now(timezone.utc), f"{type(exc).__name__}: {exc}")
                    print(f"WebSocket interrupted; reconnect in {backoff:.0f}s ({type(exc).__name__}: {exc})")
                    if duration_seconds is not None:
                        remaining = duration_seconds - (time.monotonic() - started)
                        if remaining <= 0:
                            break
                        await asyncio.sleep(min(backoff, remaining))
                    else:
                        await asyncio.sleep(backoff)
                    backoff = min(float(config["feed"]["max_reconnect_backoff_seconds"]), backoff * 2 + random.random() * 0.25)

        try:
            if duration_seconds is None:
                asyncio.run(consume())
            else:
                async def bounded() -> None:
                    try:
                        async with asyncio.timeout(duration_seconds):
                            await consume()
                    except TimeoutError:
                        pass
                asyncio.run(bounded())
        except KeyboardInterrupt:
            print("Interrupted by user; saving Paper run.")
        finally:
            stopped = datetime.now(timezone.utc)
            engine.set_connection(False, stopped, "run stopped; virtual positions are marked, not force-filled")
            result = engine.report(stopped)
            result["output"] = {"raw_events_jsonl": str(raw_path), "completed_candles_csv": str(candles_path), "report_json": str(report_path), "report_markdown": str(markdown_path)}
            result["config"] = config
            result["started_at"] = started_at.isoformat()
            result["rest_setup"] = "success"
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            markdown_path.write_text(_markdown(result), encoding="utf-8")
    return result


def replay_live_events(config: dict[str, Any], input_path: str | Path) -> dict[str, Any]:
    source = Path(input_path)
    markets: set[str] = set()
    events: list[dict[str, Any]] = []
    seed_rows: dict[str, list[dict[str, Any]]] = {}
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}") from exc
        kind = item.get("kind")
        if kind == "rest_candle_seed":
            markets.add(item["payload"]["market"])
            seed_rows.setdefault(item["payload"]["market"], []).append(item["payload"])
        elif kind == "websocket":
            payload = item.get("payload", {})
            if payload.get("code", "").startswith("KRW-"):
                markets.add(payload["code"])
        events.append(item)
    if not markets:
        raise ValueError("No recorded public Upbit market events found")
    engine = LivePaperEngine(config, sorted(markets), run_id="replay-" + source.stem)
    for market, rows in seed_rows.items():
        engine.seed_history(market, rows)
    for item in events:
        kind = item.get("kind")
        if kind == "websocket":
            received = datetime.fromisoformat(item["received_at"])
            payload = item["payload"]
            if payload.get("type") == "candle.1m":
                engine.on_candle(payload, received)
            elif payload.get("type") in {"ticker", "trade", "orderbook"}:
                engine.set_connection(True, received, "recorded stream")
                engine.on_quote(payload, received)
    engine.set_connection(False, datetime.now(timezone.utc), "offline replay complete")
    return engine.report(datetime.now(timezone.utc))


def _latest_complete_bar(engine: LivePaperEngine, market: str) -> Bar | None:
    history = engine.accounts["A"].histories.get(market, [])
    return history[-1] if history else None


def _init_candle_file(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerow(("timestamp", "symbol", "market", "open", "high", "low", "close", "volume", "complete", "turnover_krw"))


def _append_candle(path: Path, bar: Bar) -> None:
    with path.open("a", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerow((bar.timestamp.isoformat(), bar.symbol, "KRW", bar.open, bar.high, bar.low, bar.close, bar.volume, "true", bar.turnover_krw or ""))


def _record(stream, item: dict[str, Any]) -> None:
    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# 업비트 공개 시세 Paper Trading 리포트", "",
        "> **LIVE PUBLIC MARKET DATA — INTERNAL PAPER FILLS ONLY**  ",
        "> 이 실행은 업비트 공개 시세만 사용했습니다. 업비트 Exchange/Private API 및 실제 주문은 호출하지 않았습니다.", "",
        f"- Run: `{result['run_id']}`",
        f"- 상태: {result.get('status', 'stopped')}; 기준 시각: {result.get('as_of', result['stopped_at'])}",
        f"- REST pair/candle setup: {result.get('rest_setup', 'not recorded')}",
        f"- Pair(s): {', '.join(result['started_markets'])}",
        f"- 보고 시점 연결 상태: {result['connected']}",
        f"- WebSocket 시세 메시지 수: {result['websocket_message_count']}",
        f"- 완성 실시간 1분봉: {result.get('completed_live_candle_count', 0)}",
        f"- stale/잘못된 시세 건수: {result['stale_or_invalid_quote_count']}",
        f"- 끊김/종료 횟수: {result['disconnect_count']}",
        "", "## 포트폴리오 비교", "",
        "| 전략 | KRW 현금 | 실현 손익 | 평가 손익 | 평가 자산 | 비용 | 보유 종목 | 체결 수 | 스킵 |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for key, portfolio in result["portfolios"].items():
        skipped = portfolio["skipped_entry_count"]
        lines.append(f"| {key} | {portfolio['cash_krw']} | {portfolio['realized_pnl_krw']} | {portfolio['unrealized_pnl_krw']} | {portfolio['equity_krw']} | {portfolio['fees_paid_total_krw']} | {', '.join(item['market'] for item in portfolio['open_positions']) or '없음'} | {portfolio['fills_count']} | {skipped} |")
    lines.extend([
        "", "## 전략 정의", "",
        "- **A**: EMA5>EMA20, EMA20 상승, 세션 VWAP 위에서 직전 6개 고가를 거래량 증가와 함께 돌파.",
        "- **B**: 세션 VWAP보다 0.4% 이상 낮고 종가 반등이 확인되면 진입 후보.",
        "- **C**: KST 자정부터 15분간의 고가를 완성봉 종가가 상향 돌파.",
        "- **D (AlphaScalp UI)**: RSI14<42 또는 볼린저 하단(20,2) 0.8% 이내, 그리고 직전 종가보다 반등. 손절 0.9%, 익절 1.5%, +0.75% 도달 후 고점 대비 -0.5% 트레일링; 시간청산 없음.",
        "- **E (AlphaScalp daemon)**: D와 같은 RSI/볼린저 조건이지만 반등 확인 없음. 손절 1.0%, 익절 1.5%, 같은 트레일링; 시간청산 없음.",
        "", "## 데이터 상태", "",
        f"마지막 호가 수신 후 경과 초: `{json.dumps(result['last_quote_age_seconds'], ensure_ascii=False)}`",
        f"마지막 공개 quote: `{json.dumps(result.get('latest_quotes', {}), ensure_ascii=False)}`",
        "연결 끊김 또는 stale quote 동안에는 새 진입을 실행하지 않습니다. 새 시세가 없으면 기존 가상 포지션을 강제 청산하지 않고 이전 평가값을 유지합니다.",
        "", "## 비용과 제약", "",
        "체결은 신호 발생 후 확인한 다음 유효 ask/bid로 가상 처리하고 수수료·슬리피지를 반영합니다. 현금·포지션·손실·일일 진입 한도를 적용합니다. 호가 잔량에 따른 부분체결, 거래소 지연/순간 유동성, 체결 우선순위는 모사하지 않습니다. 수수료 기본값은 예시이며 계정별 적용률 확인이 필요합니다.",
        "",
    ])
    return "\n".join(lines)
