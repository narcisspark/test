"""Conservative chronological parameter search for crypto paper strategies."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any

from trading_paper.simulation.engine import simulate
from trading_paper.simulation.models import Bar


def optimize_strategies(
    bars: list[Bar],
    config: dict[str, Any],
    *,
    minimum_sessions: int = 30,
    minimum_train_trades: int = 5,
    max_holdout_drawdown_pct: float = 5.0,
) -> dict[str, Any]:
    """Select on the oldest segment, confirm on validation, gate on untouched holdout."""
    sessions: dict[Any, list[Bar]] = {}
    for bar in bars:
        sessions.setdefault(bar.session, []).append(bar)
    days = [sessions[key] for key in sorted(sessions)]
    if len(days) < minimum_sessions:
        raise ValueError(f"Need at least {minimum_sessions} KST sessions; found {len(days)}")
    train_end = max(1, int(len(days) * 0.60))
    validation_end = max(train_end + 1, int(len(days) * 0.80))
    if validation_end >= len(days):
        raise ValueError("Need enough sessions for separate train, validation, and holdout periods")
    train = [bar for day in days[:train_end] for bar in day]
    validation = [bar for day in days[train_end:validation_end] for bar in day]
    holdout = [bar for day in days[validation_end:] for bar in day]

    report: dict[str, Any] = {
        "classification": "OFFLINE HISTORICAL PARAMETER SEARCH — PAPER ONLY",
        "method": "60% chronological training / 20% validation / 20% untouched holdout",
        "data": {
            "bars": len(bars),
            "sessions": len(days),
            "first_timestamp": bars[0].timestamp.isoformat(),
            "last_timestamp": bars[-1].timestamp.isoformat(),
        },
        "windows": {
            "train": _window(train),
            "validation": _window(validation),
            "holdout": _window(holdout),
        },
        "selection_score": "net_return_pct - 1.5 * max_drawdown_pct; candidates need sufficient closed trades",
        "strategies": {},
        "candidate_config": copy.deepcopy(config),
    }
    simulation_config = _to_simulation_config(config)
    minimum_validation_trades = max(10, minimum_train_trades)
    minimum_holdout_trades = max(20, minimum_train_trades * 2)
    stressed_source_config = _double_execution_costs(config)
    stressed_config = _to_simulation_config(stressed_source_config)
    report["promotion_gates"] = {
        "minimum_validation_closed_trades": minimum_validation_trades,
        "minimum_holdout_closed_trades": minimum_holdout_trades,
        "holdout_stress": "double configured fee, slippage, and fallback spread",
        "stress_must_remain_positive_and_beat_stressed_baseline": True,
    }

    for strategy_id in ("A", "B", "C"):
        baseline_train = _evaluate(train, simulation_config, strategy_id)
        baseline_validation = _evaluate(validation, simulation_config, strategy_id)
        baseline_holdout = _evaluate(holdout, simulation_config, strategy_id)
        baseline_stress_holdout = _evaluate(holdout, stressed_config, strategy_id)
        candidates = _candidates(strategy_id, config)
        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for settings in candidates:
            candidate_config = copy.deepcopy(config)
            candidate_config["strategies"][strategy_id].update(settings)
            result = _evaluate(train, _to_simulation_config(candidate_config), strategy_id)
            summary = result["strategies"][strategy_id]
            if summary["closed_trades"] >= minimum_train_trades:
                scored.append((_score(summary), settings, summary))

        if scored:
            best_score, best_settings, best_train = max(scored, key=lambda row: row[0])
            selected_config = copy.deepcopy(config)
            selected_config["strategies"][strategy_id].update(best_settings)
            selected_config = _to_simulation_config(selected_config)
            selected_validation = _evaluate(validation, selected_config, strategy_id)["strategies"][strategy_id]
            selected_holdout = _evaluate(holdout, selected_config, strategy_id)["strategies"][strategy_id]
            selected_stress_config = copy.deepcopy(stressed_source_config)
            selected_stress_config["strategies"][strategy_id].update(best_settings)
            selected_stress_holdout = _evaluate(
                holdout, _to_simulation_config(selected_stress_config), strategy_id
            )["strategies"][strategy_id]
            selected_validation_score = _score(selected_validation)
            selected_holdout_score = _score(selected_holdout)
            selected_stress_holdout_score = _score(selected_stress_holdout)
        else:
            best_score = None
            best_settings = None
            best_train = None
            selected_validation = None
            selected_holdout = None
            selected_stress_holdout = None
            selected_validation_score = None
            selected_holdout_score = None
            selected_stress_holdout_score = None

        eligible = _promotion_eligible(
            has_selected_settings=best_settings is not None and best_score is not None,
            selected_train_score=best_score,
            baseline_train_score=_score(baseline_train["strategies"][strategy_id]),
            validation=selected_validation,
            baseline_validation=baseline_validation["strategies"][strategy_id],
            holdout=selected_holdout,
            baseline_holdout=baseline_holdout["strategies"][strategy_id],
            stressed_holdout=selected_stress_holdout,
            baseline_stressed_holdout=baseline_stress_holdout["strategies"][strategy_id],
            minimum_validation_trades=minimum_validation_trades,
            minimum_holdout_trades=minimum_holdout_trades,
            max_drawdown_pct=max_holdout_drawdown_pct,
        )
        if eligible:
            report["candidate_config"]["strategies"][strategy_id].update(best_settings)
        report["strategies"][strategy_id] = {
            "candidate_count": len(candidates),
            "train_candidates_with_enough_trades": len(scored),
            "baseline": {
                "train": baseline_train["strategies"][strategy_id],
                "validation": baseline_validation["strategies"][strategy_id],
                "holdout": baseline_holdout["strategies"][strategy_id],
                "stress_holdout": baseline_stress_holdout["strategies"][strategy_id],
            },
            "selected_settings": best_settings,
            "selected": {
                "train": best_train,
                "validation": selected_validation,
                "holdout": selected_holdout,
                "stress_holdout": selected_stress_holdout,
            },
            "scores": {
                "train": best_score,
                "validation": selected_validation_score,
                "holdout": selected_holdout_score,
                "stress_holdout": selected_stress_holdout_score,
            },
            "promotion_eligible": eligible,
            "gate_reason": "Passed chronological return, trade-count, and drawdown gates" if eligible else "Keep baseline: candidate did not pass all out-of-sample gates",
        }

    report["any_candidate_eligible"] = any(row["promotion_eligible"] for row in report["strategies"].values())
    return report


def write_optimizer_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "learning-report.json"
    candidate_path = destination / "candidate-config.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_path.write_text(json.dumps(report["candidate_config"], ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": str(report_path), "candidate_config": str(candidate_path)}


def _evaluate(bars: list[Bar], config: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    return simulate(bars, config, continuous=True, strategy_ids=(strategy_id,), liquidate_at_end=True)


def _to_simulation_config(config: dict[str, Any]) -> dict[str, Any]:
    execution = config["execution"]
    if "fee_bps_per_side" not in execution:
        return config
    return {
        "initial_cash_krw": config["initial_cash_krw"],
        "execution": {
            "spread_bps_total": execution["fallback_spread_bps_total"],
            "slippage_bps": execution["slippage_bps"],
            "commission_bps_per_side": execution["fee_bps_per_side"],
            "sell_tax_bps": {"KRW": 0.0},
        },
        "risk": copy.deepcopy(config["risk"]),
        "feed": copy.deepcopy(config.get("feed", {})),
        "strategies": copy.deepcopy(config["strategies"]),
    }


def _window(bars: list[Bar]) -> dict[str, Any]:
    return {
        "bars": len(bars),
        "start": bars[0].timestamp.isoformat(),
        "end": bars[-1].timestamp.isoformat(),
        "sessions": len({bar.session for bar in bars}),
    }


def _score(summary: dict[str, Any]) -> float:
    return float(summary["net_return_pct"]) - 1.5 * float(summary["max_drawdown_pct"])


def _double_execution_costs(config: dict[str, Any]) -> dict[str, Any]:
    """Create a pessimistic execution scenario without mutating the base config."""
    stressed = copy.deepcopy(config)
    execution = stressed["execution"]
    for key in ("fee_bps_per_side", "slippage_bps", "fallback_spread_bps_total"):
        if key in execution:
            execution[key] = float(execution[key]) * 2
    return stressed


def _promotion_eligible(
    *, has_selected_settings: bool, selected_train_score: float | None,
    baseline_train_score: float, validation: dict[str, Any] | None,
    baseline_validation: dict[str, Any], holdout: dict[str, Any] | None,
    baseline_holdout: dict[str, Any], stressed_holdout: dict[str, Any] | None,
    baseline_stressed_holdout: dict[str, Any], minimum_validation_trades: int,
    minimum_holdout_trades: int, max_drawdown_pct: float,
) -> bool:
    """Require sufficient OOS evidence and survive twice the assumed costs."""
    return bool(
        has_selected_settings
        and selected_train_score is not None
        and selected_train_score > baseline_train_score
        and validation is not None
        and validation["closed_trades"] >= minimum_validation_trades
        and validation["net_return_pct"] > baseline_validation["net_return_pct"]
        and holdout is not None
        and holdout["closed_trades"] >= minimum_holdout_trades
        and holdout["net_return_pct"] > 0
        and holdout["net_return_pct"] > baseline_holdout["net_return_pct"]
        and holdout["max_drawdown_pct"] <= max_drawdown_pct
        and stressed_holdout is not None
        and stressed_holdout["closed_trades"] >= minimum_holdout_trades
        and stressed_holdout["net_return_pct"] > 0
        and stressed_holdout["net_return_pct"] > baseline_stressed_holdout["net_return_pct"]
        and stressed_holdout["max_drawdown_pct"] <= max_drawdown_pct
    )


def _candidates(strategy_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    current = config["strategies"][strategy_id]
    if strategy_id == "A":
        pairs = [(3, 12), (5, 20), (8, 30)]
        return [
            {"ema_fast": fast, "ema_slow": slow, "breakout_bars": breakout, "volume_multiple": multiple}
            for (fast, slow), breakout, multiple in itertools.product(pairs, (3, 6, 12), (1.0, 1.5))
        ]
    if strategy_id == "B":
        return [
            {"deviation_below_vwap_pct": deviation, "require_close_above_previous": confirm}
            for deviation, confirm in itertools.product((0.002, 0.004, 0.006, 0.008), (True, False))
        ]
    if strategy_id == "C":
        return [{"opening_range_bars": width} for width in (5, 15, 30, 60)]
    return [copy.deepcopy(current)]
