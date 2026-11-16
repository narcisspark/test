"""Exploratory screen for avoiding entries whose modeled tick costs eat the target."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from trading_paper.simulation.engine import simulate
from trading_paper.simulation.optimizer import _to_simulation_config
from trading_paper.simulation.short_window_hypothesis_replay import (
    _complete_sessions,
    _split_sessions,
)
from trading_paper.simulation.csvio import load_bars


# Percent of notional, fixed before the replay. None is the unfiltered baseline.
COST_CAPS_PCT: tuple[float | None, ...] = (None, 0.5, 0.75, 1.0, 1.5)


def _evaluate(bars, source_config: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    sim_config = _to_simulation_config(source_config)
    result = simulate(
        bars, sim_config, continuous=True,
        strategy_ids=(strategy_id,), liquidate_at_end=True,
    )
    summary = result["strategies"][strategy_id]
    return {
        key: summary[key]
        for key in (
            "net_pnl_krw", "net_return_pct", "max_drawdown_pct", "closed_trades",
            "win_rate_pct", "profit_factor", "total_costs_krw",
        )
    }


def _score(summary: dict[str, Any]) -> float:
    return float(summary["net_return_pct"]) - 1.5 * float(summary["max_drawdown_pct"])


def _cap_name(cap: float | None) -> str:
    return "no_filter" if cap is None else f"max_{cap:g}pct"


def screen_tick_cost_filter(path: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    """Choose a tick-cost cap on train and report later periods for exploration only.

    The previous one-week holdout has already been inspected, so results from
    this follow-up are diagnostic and must not be treated as fresh validation.
    """
    bars = [bar for bar in load_bars(path) if bar.market == "KRW"]
    if not bars:
        raise ValueError("CSV contains no KRW crypto candles")
    sessions = _complete_sessions(bars)
    windows = _split_sessions(sessions)
    results: dict[str, Any] = {}
    for strategy_id in ("A", "B", "C"):
        candidates: dict[str, Any] = {}
        for cap in COST_CAPS_PCT:
            candidate = copy.deepcopy(config)
            if cap is not None:
                candidate["risk"]["max_round_trip_cost_pct"] = cap
            train = _evaluate(windows["train"], candidate, strategy_id)
            validation = _evaluate(windows["validation"], candidate, strategy_id)
            holdout = _evaluate(windows["holdout"], candidate, strategy_id)
            stressed = copy.deepcopy(candidate)
            for field in ("fee_bps_per_side", "slippage_bps", "fallback_spread_bps_total"):
                stressed["execution"][field] = float(stressed["execution"][field]) * 2
            stress_holdout = _evaluate(windows["holdout"], stressed, strategy_id)
            candidates[_cap_name(cap)] = {
                "max_round_trip_cost_pct": cap,
                "train_score": _score(train),
                "train": train,
                "validation": validation,
                "holdout": holdout,
                "holdout_double_cost_stress": stress_holdout,
            }
        selected_name = max(candidates, key=lambda name: candidates[name]["train_score"])
        results[strategy_id] = {
            "selection_rule": "highest train net_return_pct - 1.5 * max_drawdown_pct",
            "selected_on_train": selected_name,
            "selected_cost_cap_pct": candidates[selected_name]["max_round_trip_cost_pct"],
            "candidates": candidates,
            "promotion_eligible": False,
            "promotion_reason": "Exploratory only: the source holdout was inspected before defining this follow-up hypothesis.",
        }

    return {
        "classification": "EXPLORATORY TICK-COST FILTER SCREEN — NOT FRESH OUT-OF-SAMPLE EVIDENCE",
        "data": {
            "source": str(path),
            "symbols": sorted({bar.symbol for bar in bars}),
            "complete_sessions": len(sessions),
            "windows": {
                name: {
                    "start": rows[0].timestamp.isoformat(),
                    "end": rows[-1].timestamp.isoformat(),
                    "bars": len(rows),
                }
                for name, rows in windows.items()
            },
        },
        "hypothesis": (
            "Reject a signal when estimated flat-price round-trip costs, including price-tier tick rounding, "
            "exceed a fixed percentage of notional."
        ),
        "cost_caps_pct": list(COST_CAPS_PCT),
        "cost_estimator": (
            "Round up the modeled buy price and round down the modeled sell price at the current Upbit tick; "
            "include both commissions and sell tax, then divide by reference price."
        ),
        "known_limitation": (
            "A 1-minute OHLCV bar does not contain historical spread or queue depth. The price-tick estimate is a conservative proxy, not an observed fill cost."
        ),
        "strategies": results,
        "any_candidate_eligible": False,
    }


def write_tick_cost_screen(report: dict[str, Any], destination: str | Path) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
