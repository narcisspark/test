"""Local, side-effect-free project commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from importlib.resources import files


def load_capabilities() -> dict[str, Any]:
    path = files("trading_paper").joinpath("data/provider-capabilities.json")
    with path.open("r", encoding="utf-8") as source:
        data = json.load(source)
    if data.get("schema_version") != "1.0":
        raise ValueError("Unsupported provider capability schema_version")
    allowed = set(data.get("status_values", []))
    for capability in data.get("capabilities", []):
        if capability.get("status") not in allowed:
            raise ValueError(f"Invalid status for capability {capability.get('id')!r}")
    return data


def _stage0_summary(_: argparse.Namespace) -> int:
    data = load_capabilities()
    capabilities = data["capabilities"]
    counts = {status: 0 for status in data["status_values"]}
    for capability in capabilities:
        counts[capability["status"]] += 1
    print(f"Provider: {data['provider']}")
    print(f"Public material checked: {data['checked_on']}")
    print(f"Capabilities: {len(capabilities)}")
    print("Status: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    for capability in capabilities:
        print(f"- {capability['id']}: {capability['status']} — {capability['name']}")
    print("No network, credentials, or broker endpoints were accessed.")
    return 0


def _load_project_json(path: str | None, default_name: str) -> dict[str, Any]:
    target = Path(path) if path else Path("config") / default_name
    with target.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {target}")
    return value


def _live_run(args: argparse.Namespace) -> int:
    from trading_paper.simulation.live_runner import run_live_paper

    config = _load_project_json(args.config, "crypto-paper.json")
    result = run_live_paper(config, args.markets, args.output_dir, args.duration_seconds)
    print(f"Run {result['run_id']} saved: {result['output']['report_markdown']}")
    print(f"Public WebSocket messages: {result['websocket_message_count']}; closed paper trades: " + ", ".join(f"{key}={len(result['portfolios'][key]['closed_trades'])}" for key in ("A", "B", "C")))
    return 0


def _live_replay(args: argparse.Namespace) -> int:
    from trading_paper.simulation.live_runner import replay_live_events

    config = _load_project_json(args.config, "crypto-paper.json")
    result = replay_live_events(config, args.events)
    result["classification"] = "OFFLINE REPLAY OF SAVED PUBLIC MARKET EVENTS — PAPER ONLY"
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "replay-report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Replay report saved: {path}")
    return 0


def _service_run(args: argparse.Namespace) -> int:
    from datetime import datetime
    from trading_paper.simulation.service_runner import run_service

    config = _load_project_json(args.config, "crypto-paper.json")
    run_service(config, args.markets, args.output_dir, args.run_id,
                datetime.fromisoformat(args.deadline), args.checkpoint_seconds)
    return 0


def _offline_simulate(args: argparse.Namespace) -> int:
    from trading_paper.simulation.csvio import load_bars
    from trading_paper.simulation.demo import generate_demo_bars, write_demo_csv
    from trading_paper.simulation.engine import simulate
    from trading_paper.simulation.reports import write_reports

    config = _load_project_json(args.config, "simulation.json")
    output = Path(args.output_dir)
    if args.demo:
        bars = generate_demo_bars()
        write_demo_csv(bars, output / "synthetic_ohlcv.csv")
        result = simulate(load_bars(output / "synthetic_ohlcv.csv"), config, synthetic=True, continuous=True)
    else:
        if not args.input:
            raise ValueError("Provide --input CSV or explicitly select --demo")
        bars = load_bars(args.input)
        result = simulate(bars, config, continuous=all(bar.market == "KRW" for bar in bars))
    paths = write_reports(result, output)
    print("; ".join(f"{key}={value}" for key, value in paths.items()))
    return 0


def _history_download(args: argparse.Namespace) -> int:
    from datetime import datetime
    from trading_paper.simulation.history import download_minute_history

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)

    result = download_minute_history(args.market, parse(args.start), parse(args.end), args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _history_download_liquid_universe(args: argparse.Namespace) -> int:
    from datetime import datetime
    from trading_paper.simulation.universe_history import download_liquid_universe_history

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)

    result = download_liquid_universe_history(
        parse(args.start), parse(args.end), args.output, top_n=args.top_n,
        progress=lambda message: print(message, flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _learn(args: argparse.Namespace) -> int:
    from trading_paper.simulation.csvio import load_bars
    from trading_paper.simulation.optimizer import optimize_strategies, write_optimizer_report

    config = _load_project_json(args.config, "crypto-paper.json")
    report = optimize_strategies(
        load_bars(args.input),
        config,
        minimum_sessions=args.minimum_sessions,
        minimum_train_trades=args.minimum_trades,
        max_holdout_drawdown_pct=args.max_holdout_drawdown_pct,
    )
    paths = write_optimizer_report(report, args.output_dir)
    print(json.dumps({
        "any_candidate_eligible": report["any_candidate_eligible"],
        "promotions": {key: row["promotion_eligible"] for key, row in report["strategies"].items()},
        **paths,
    }, ensure_ascii=False, indent=2))
    return 0


def _stock_replay(args: argparse.Namespace) -> int:
    from trading_paper.simulation.stock_csv import load_llm_decisions, load_stock_bars
    from trading_paper.simulation.stock_engine import simulate_stock

    config = _load_project_json(args.config, "n2-stock-paper.json")
    bars = load_stock_bars(args.input)
    decisions = load_llm_decisions(args.decisions)
    report = simulate_stock(bars, config, llm_decisions=decisions)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "stock-replay-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"KRX stock replay report saved: {path}")
    return 0


def _orderflow_replay(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from trading_paper.simulation.orderflow_replay import replay_orderflow, write_replay_report

    report = replay_orderflow(
        args.events,
        fee_rate=Decimal(str(args.fee_bps_per_side)) / Decimal("10000"),
        slippage_rate=Decimal(str(args.slippage_bps_per_side)) / Decimal("10000"),
    )
    path = write_replay_report(report, args.output)
    account = report["account"]
    print(json.dumps({"report": str(path), "net_return_pct": account["net_return_pct"],
                      "closed_trades": account["closed_trades"], "wins": account["wins"],
                      "losses": account["losses"]}, ensure_ascii=False, indent=2))
    return 0


def _pullback_replay(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from trading_paper.simulation.pullback_orderflow_replay import replay_pullback, write_pullback_report

    report = replay_pullback(
        args.events,
        args.candles,
        fee_rate=Decimal(str(args.fee_bps_per_side)) / Decimal("10000"),
        slip_rate=Decimal(str(args.slippage_bps_per_side)) / Decimal("10000"),
        flow_profile=args.flow_profile,
    )
    path = write_pullback_report(report, args.output)
    account = report["account"]
    print(json.dumps({
        "report": str(path),
        "net_return_pct": account["net_return_pct"],
        "closed_trades": account["closed_trades"],
        "signals": account["signals"],
        "model_gate_enabled": report["model_gate"]["enabled"],
    }, ensure_ascii=False, indent=2))
    return 0


def _pullback_ohlcv_replay(args: argparse.Namespace) -> int:
    from trading_paper.simulation.ohlcv_pullback_replay import (
        replay_ohlcv_pullback,
        write_ohlcv_pullback_report,
    )

    report = replay_ohlcv_pullback(args.input)
    path = write_ohlcv_pullback_report(report, args.output)
    print(json.dumps({
        "report": str(path),
        "classification": report["classification"],
        "windows": {
            name: {
                "net_return_pct": row["net_return_pct"],
                "buy_hold_10pct_net_return_pct": row["buy_hold_10pct"]["net_return_pct"],
                "closed_trades": row["closed_trades"],
                "positions_opened": row["positions_opened"],
                "max_drawdown_pct": row["max_drawdown_pct"],
            }
            for name, row in report["windows"].items()
        },
        "promotion_eligible": report["promotion_eligible"],
    }, ensure_ascii=False, indent=2))
    return 0


def _short_window_hypothesis_replay(args: argparse.Namespace) -> int:
    from trading_paper.simulation.short_window_hypothesis_replay import (
        replay_short_window_hypotheses,
        write_short_window_report,
    )

    config = _load_project_json(args.config, "crypto-paper.json")
    report = replay_short_window_hypotheses(args.input, config)
    path = write_short_window_report(report, args.output)
    print(json.dumps({
        "report": str(path),
        "windows": report["windows"],
        "strategies": {
            key: {
                "selected_on_train": value["selected_on_train"],
                "baseline_train_return_pct": value["train"]["baseline"]["net_return_pct"],
                "variant_train_return_pct": value["train"]["variant"]["net_return_pct"],
                "selected_holdout_return_pct": value["holdout"]["selected"]["net_return_pct"],
                "selected_holdout_trades": value["holdout"]["selected"]["closed_trades"],
                "promotion_eligible": value["promotion_eligible"],
            }
            for key, value in report["strategies"].items()
        },
        "any_candidate_eligible": report["any_candidate_eligible"],
    }, ensure_ascii=False, indent=2))
    return 0


def _cost_aware_screen(args: argparse.Namespace) -> int:
    from trading_paper.simulation.cost_aware_screen import (
        screen_tick_cost_filter,
        write_tick_cost_screen,
    )

    config = _load_project_json(args.config, "crypto-paper.json")
    report = screen_tick_cost_filter(args.input, config)
    path = write_tick_cost_screen(report, args.output)
    print(json.dumps({
        "report": str(path),
        "classification": report["classification"],
        "selected_on_train": {
            key: {
                "cap_pct": value["selected_cost_cap_pct"],
                "holdout_return_pct": value["candidates"][value["selected_on_train"]]["holdout"]["net_return_pct"],
                "holdout_trades": value["candidates"][value["selected_on_train"]]["holdout"]["closed_trades"],
                "holdout_stress_return_pct": value["candidates"][value["selected_on_train"]]["holdout_double_cost_stress"]["net_return_pct"],
            }
            for key, value in report["strategies"].items()
        },
        "promotion_eligible": False,
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-paper")
    commands = parser.add_subparsers(dest="group", required=True)
    stage0 = commands.add_parser("stage0", help="Review N2 stock capability evidence")
    stage0_commands = stage0.add_subparsers(dest="action", required=True)
    summary = stage0_commands.add_parser("summary", help="Summarize the local capability register")
    summary.set_defaults(handler=_stage0_summary)
    paper = commands.add_parser("paper", help="Run the public-market, internal-only Paper Trading system")
    paper_commands = paper.add_subparsers(dest="action", required=True)
    run = paper_commands.add_parser("run", help="Default live mode: public Upbit KRW quotation WebSocket")
    run.add_argument("--markets", nargs="+", help="KRW pair codes; defaults to config/crypto-paper.json")
    run.add_argument("--duration-seconds", type=float, help="Stop after this time; omit to run until Ctrl-C")
    run.add_argument("--config", help="JSON config path")
    run.add_argument("--output-dir", default="outputs/live-paper")
    run.set_defaults(handler=_live_run)
    service = paper_commands.add_parser("run-service", help="Durable cloud run with a fixed deadline and resumable portfolios")
    service.add_argument("--markets", nargs="+")
    service.add_argument("--run-id", required=True)
    service.add_argument("--deadline", required=True, help="Timezone-aware ISO-8601 stop time; preserved across restarts")
    service.add_argument("--checkpoint-seconds", type=float, default=30)
    service.add_argument("--config")
    service.add_argument("--output-dir", default="outputs/service-paper")
    service.set_defaults(handler=_service_run)
    replay = paper_commands.add_parser("replay-live", help="Replay a saved public WebSocket JSONL event log")
    replay.add_argument("--events", required=True)
    replay.add_argument("--config")
    replay.add_argument("--output-dir", default="outputs/live-replay")
    replay.set_defaults(handler=_live_replay)
    offline = commands.add_parser("simulate", help="Offline CSV replay or explicit SYNTHETIC/DEMO sample")
    offline.add_argument("--input", help="CSV path matching docs/csv-schema.md")
    offline.add_argument("--demo", action="store_true", help="Generate deterministic dummy data; never treat as evidence")
    offline.add_argument("--config", help="JSON simulation config path")
    offline.add_argument("--output-dir", default="outputs/offline-replay")
    offline.set_defaults(handler=_offline_simulate)
    history = commands.add_parser("history", help="Download public Upbit historical candles for offline replay")
    history_commands = history.add_subparsers(dest="action", required=True)
    download = history_commands.add_parser("download-minute", help="Download a time-bounded KRW 1-minute candle CSV")
    download.add_argument("--market", required=True, help="KRW pair, for example KRW-BTC")
    download.add_argument("--start", required=True, help="Inclusive ISO-8601 time with timezone, for example 2026-07-01T00:00:00+09:00")
    download.add_argument("--end", required=True, help="Exclusive ISO-8601 time with timezone")
    download.add_argument("--output", required=True, help="Destination CSV matching docs/csv-schema.md")
    download.set_defaults(handler=_history_download)
    liquid = history_commands.add_parser(
        "download-liquid-universe",
        help="Rank KRW markets by prior-24h turnover and download a multi-coin paper dataset",
    )
    liquid.add_argument("--start", required=True, help="Historical window start, timezone-aware ISO-8601")
    liquid.add_argument("--end", required=True, help="Historical window end, timezone-aware ISO-8601")
    liquid.add_argument("--top-n", type=int, default=10, help="Top markets by prior-24h turnover; BTC is always included")
    liquid.add_argument("--output", required=True, help="Combined multi-market candle CSV destination")
    liquid.set_defaults(handler=_history_download_liquid_universe)
    learning = commands.add_parser("learn", help="Search strategy parameters with chronological historical validation")
    learning.add_argument("--input", required=True, help="Historical OHLCV CSV, usually from history download-minute")
    learning.add_argument("--config", help="Base config; defaults to config/crypto-paper.json")
    learning.add_argument("--output-dir", default="outputs/learning")
    learning.add_argument("--minimum-sessions", type=int, default=30)
    learning.add_argument("--minimum-trades", type=int, default=5, help="Minimum closed trades in the training window")
    learning.add_argument("--max-holdout-drawdown-pct", type=float, default=5.0)
    learning.set_defaults(handler=_learn)
    stock = commands.add_parser("simulate-stock", help="Offline KRX stock replay; incomplete bars are rejected")
    stock.add_argument("--input", required=True, help="CSV with complete KRX 5-minute bars")
    stock.add_argument("--decisions", help="Optional JSON array of fresh, schema-validated LLM decisions for B")
    stock.add_argument("--config", help="JSON config path; defaults to config/n2-stock-paper.json")
    stock.add_argument("--output-dir", default="outputs/stock-replay")
    stock.set_defaults(handler=_stock_replay)
    orderflow = commands.add_parser("replay-orderflow", help="Costed offline replay of saved public Upbit orderbook/trade events")
    orderflow.add_argument("--events", nargs="+", required=True, help="Saved public websocket JSONL/JSONL.GZ event files")
    orderflow.add_argument("--output", required=True, help="Destination JSON report")
    orderflow.add_argument("--fee-bps-per-side", type=float, default=5.0)
    orderflow.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    orderflow.set_defaults(handler=_orderflow_replay)
    pullback = commands.add_parser(
        "replay-pullback",
        help="Paper replay of the trend-pullback strategy against saved candles and public orderflow",
    )
    pullback.add_argument("--candles", required=True, help="Complete KRW-BTC 1-minute candle CSV")
    pullback.add_argument("--events", nargs="+", required=True, help="Saved public websocket JSONL/JSONL.GZ event files")
    pullback.add_argument("--output", required=True, help="Destination JSON report")
    pullback.add_argument("--fee-bps-per-side", type=float, default=5.0)
    pullback.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    pullback.add_argument("--flow-profile", choices=("v1", "strict", "strict2"), default="v1",
                          help="strict requires OFI/microprice/spread/depth gates for 3 seconds; strict2 uses the same gates for 2 seconds")
    pullback.set_defaults(handler=_pullback_replay)
    pullback_ohlcv = commands.add_parser(
        "replay-pullback-ohlcv",
        help="Exploratory OHLCV-only proxy replay; not an orderflow validation",
    )
    pullback_ohlcv.add_argument("--input", required=True, help="Complete KRW-BTC 1-minute candle CSV")
    pullback_ohlcv.add_argument("--output", required=True, help="Destination JSON report")
    pullback_ohlcv.set_defaults(handler=_pullback_ohlcv_replay)
    short_research = commands.add_parser(
        "research-seven-day",
        help="Compare crypto hypotheses on a costed chronological replay of one or more KRW markets",
    )
    short_research.add_argument("--input", required=True, help="Recent one- or multi-market KRW minute candle CSV")
    short_research.add_argument("--config", help="Base paper config; defaults to config/crypto-paper.json")
    short_research.add_argument("--output", required=True, help="Destination JSON report")
    short_research.set_defaults(handler=_short_window_hypothesis_replay)
    cost_filter = commands.add_parser(
        "research-cost-filter",
        help="Explore entry filters for modeled KRW tick-and-fee round-trip costs",
    )
    cost_filter.add_argument("--input", required=True, help="Historical multi-market KRW minute candle CSV")
    cost_filter.add_argument("--config", help="Base paper config; defaults to config/crypto-paper.json")
    cost_filter.add_argument("--output", required=True, help="Destination JSON report")
    cost_filter.set_defaults(handler=_cost_aware_screen)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"Command failed: {exc}")
        return 2
