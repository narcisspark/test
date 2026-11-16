"""A fixed-deadline paper run that resumes portfolios after a service restart."""
from __future__ import annotations

import asyncio
import csv
import json
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_paper.providers.upbit_public import PublicQuotationClient
from trading_paper.simulation.live_engine import LivePaperEngine
from trading_paper.simulation.live_runner import _append_candle, _init_candle_file, _latest_complete_bar, _markdown
from trading_paper.simulation.service_state import EventLog, atomic_json, engine_state, restore_engine


def _refresh(engine: LivePaperEngine, market: str, rows: list[dict[str, Any]]) -> None:
    for account in engine.accounts.values():
        account.histories[market] = []
        account.pending.pop(market, None)
    engine.forming.pop(market, None)
    engine.seed_history(market, rows)


def _apply(engine: LivePaperEngine, event: dict[str, Any], candles: Path | None = None, candle_keys: set | None = None) -> None:
    received = datetime.fromisoformat(event["received_at"])
    if event["kind"] == "connection":
        engine.set_connection(event["connected"], received, event.get("detail"))
    elif event["kind"] == "history_refresh":
        _refresh(engine, event["market"], event["rows"])
    elif event["kind"] == "websocket":
        payload = event["payload"]
        if payload.get("type") == "candle.1m":
            before = dict(engine.last_complete_at)
            engine.on_candle(payload, received)
            if candles:
                for market, value in engine.last_complete_at.items():
                    if before.get(market) != value:
                        bar = _latest_complete_bar(engine, market)
                        key = (bar.symbol, bar.timestamp.isoformat()) if bar else None
                        if bar and (candle_keys is None or key not in candle_keys):
                            _append_candle(candles, bar)
                            if candle_keys is not None:
                                candle_keys.add(key)
        elif payload.get("type") in {"ticker", "trade", "orderbook"}:
            engine.on_quote(payload, received)


def _session_history(client: PublicQuotationClient, market: str, minimum: int) -> list[dict[str, Any]]:
    midnight = datetime.now(ZoneInfo("Asia/Seoul")).replace(hour=0, minute=0, second=0, microsecond=0)
    rows: dict[str, dict[str, Any]] = {}
    before = None
    for _ in range(9):
        page = client.minute_candles(market, 200, to=before)
        if not page:
            break
        for row in page:
            rows[row["candle_date_time_utc"]] = row
        oldest = min(page, key=lambda row: row["candle_date_time_utc"])
        first = datetime.fromisoformat(oldest["candle_date_time_kst"]).replace(tzinfo=ZoneInfo("Asia/Seoul"))
        if first <= midnight and len(rows) >= minimum:
            break
        before = oldest["candle_date_time_utc"] + "Z"
    if not rows:
        raise RuntimeError(f"No public REST history for {market}")
    return sorted(rows.values(), key=lambda row: row["candle_date_time_utc"])


def run_service(config: dict[str, Any], markets: list[str] | None, output_dir: str | Path,
                run_id: str, deadline: datetime, checkpoint_seconds: float = 30) -> dict[str, Any]:
    if config.get("mode") != "PAPER" or deadline.tzinfo is None or checkpoint_seconds < 1:
        raise ValueError("Expected PAPER mode, a timezone-aware deadline, and checkpoint interval >= 1")
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
        raise ValueError("Invalid run ID")
    markets = sorted(set(markets or config["default_markets"]))
    destination = Path(output_dir) / run_id
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "run.json"
    checkpoint_path = destination / "checkpoint.json"
    candles_path = destination / "completed_candles.csv"
    complete_path = destination / "complete.json"
    if complete_path.exists():
        return json.loads((destination / "report.json").read_text(encoding="utf-8"))
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["config"] != config or manifest["markets"] != markets or datetime.fromisoformat(manifest["deadline"]) != deadline:
            raise ValueError("Run metadata changed; refusing to reset the experiment")
    else:
        manifest = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(),
                    "deadline": deadline.isoformat(), "markets": markets, "config": config}
        atomic_json(manifest_path, manifest)
    if not candles_path.exists():
        _init_candle_file(candles_path)
    else:
        with candles_path.open("rb+") as stream:
            data = stream.read()
            if data and not data.endswith(b"\n"):
                stream.truncate(data.rfind(b"\n") + 1)
    with candles_path.open(newline="", encoding="utf-8") as stream:
        candle_keys = {(row["symbol"], row["timestamp"]) for row in csv.DictReader(stream)}
    log = EventLog(destination / "events")
    if checkpoint_path.exists():
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("schema_version") != 1:
            raise ValueError("Unsupported checkpoint schema")
        engine = restore_engine(saved["engine"], config, markets, run_id)
        log.last_cursor = saved["cursor"]
        replayed = 0
        for event in log.after(saved["cursor"]):
            _apply(engine, event, candles_path, candle_keys)
            replayed += 1
        print(f"Resumed {run_id}; recovered {replayed} events after checkpoint", flush=True)
    else:
        engine = LivePaperEngine(config, markets, run_id=run_id)
    def record(event: dict[str, Any]) -> None:
        event.setdefault("received_at", datetime.now(timezone.utc).isoformat())
        log.record(event)
        _apply(engine, event, candles_path, candle_keys)

    def save(status: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        atomic_json(checkpoint_path, {"schema_version": 1, "saved_at": now.isoformat(),
                                    "cursor": log.cursor(), "engine": engine_state(engine)})
        result = engine.report(now)
        result.update({"status": status, "as_of": now.isoformat(), "deadline": deadline.isoformat(),
                       "started_at": manifest["started_at"], "config": config, "rest_setup": "success" if engine.last_complete_at else "pending",
                       "output": {"events_directory": str(destination / "events"), "completed_candles_csv": str(candles_path),
                                  "report_json": str(destination / "report.json"), "report_markdown": str(destination / "report.md")}})
        atomic_json(destination / "report.json", result)
        temporary = destination / "report.md.tmp"
        temporary.write_text(_markdown(result), encoding="utf-8")
        temporary.replace(destination / "report.md")
        return result

    async def main() -> str:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)

        async def consume() -> None:
            backoff = 1.0
            while True:
                try:
                    client = PublicQuotationClient()
                    pairs = await asyncio.to_thread(client.list_krw_pairs)
                    if set(markets) - set(pairs):
                        raise ValueError("Requested market is unavailable or flagged by Upbit")
                    for market in markets:
                        rows = await asyncio.to_thread(_session_history, client, market, int(config["feed"]["warmup_candles_per_market"]))
                        record({"kind": "history_refresh", "market": market, "rows": rows})
                    save("running")
                    print(f"LIVE PAPER: {','.join(markets)}; history ready; deadline={deadline.isoformat()}", flush=True)
                    connected = False
                    async for payload, received_at in client.stream(markets):
                        if not connected:
                            record({"kind": "connection", "connected": True, "detail": "public websocket connected", "received_at": received_at.isoformat()})
                            connected = True
                        record({"kind": "websocket", "received_at": received_at.isoformat(), "payload": payload})
                        backoff = 1.0
                    raise RuntimeError("Public WebSocket closed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    record({"kind": "connection", "connected": False, "detail": f"{type(exc).__name__}: {exc}"})
                    print(f"Feed interrupted; retry in {backoff}s: {exc}", flush=True)
                    await asyncio.sleep(backoff)
                    backoff = min(float(config["feed"]["max_reconnect_backoff_seconds"]), backoff * 2)

        async def checkpoints() -> None:
            while True:
                await asyncio.sleep(checkpoint_seconds)
                result = save("running")
                log.compress_sealed()
                print(f"Heartbeat: messages={result['websocket_message_count']} candles={result['completed_live_candle_count']}", flush=True)

        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return "complete"
        record({"kind": "connection", "connected": False, "detail": "service starting; refresh history before accepting new entries"})
        save("starting")
        tasks = [asyncio.create_task(consume()), asyncio.create_task(checkpoints()), asyncio.create_task(stop.wait())]
        try:
            done, _ = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return "interrupted" if stop.is_set() else "complete"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    status = "failed"
    try:
        status = asyncio.run(main())
    finally:
        record({"kind": "connection", "connected": False, "detail": "run stopped; virtual positions are marked, not force-filled"})
        result = save(status)
        log.close()
        log.compress_sealed(all_files=True)
        if status == "complete":
            atomic_json(complete_path, {"finished_at": datetime.now(timezone.utc).isoformat(), "deadline": deadline.isoformat()})
        print(f"Run {run_id}: {status}; report={destination / 'report.md'}", flush=True)
    return result
