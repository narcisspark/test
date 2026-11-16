"""Chronological, costed research screen for a small set of crypto hypotheses.

This is an exploratory paper replay. It selects at most one fixed variant per
strategy on the earliest segment and keeps later segments for validation only.
"""
from __future__ import annotations

import copy
import json
from datetime import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from trading_paper.simulation.csvio import load_bars
from trading_paper.simulation.engine import _price_tick, simulate
from trading_paper.simulation.models import Bar
from trading_paper.simulation.optimizer import _to_simulation_config

VARIANTS = {
    "A": {
        "name": "stricter trend breakout",
        "hypothesis": "Longer trend and breakout windows plus stronger volume confirmation may filter false breakouts.",
        "settings": {"ema_fast": 8, "ema_slow": 30, "breakout_bars": 12, "volume_multiple": 2.0},
    },
    "B": {
        "name": "deeper VWAP reversion",
        "hypothesis": "Requiring a deeper discount to session VWAP may improve rebound quality after costs.",
        "settings": {"deviation_below_vwap_pct": 0.008, "require_close_above_previous": True},
    },
}

MIN_VALIDATION_TRADES = 10
MIN_HOLDOUT_TRADES = 20
MAX_HOLDOUT_DRAWDOWN_PCT = 5.0


def _complete_sessions(bars: list[Bar]) -> list[list[Bar]]:
    by_day: dict[Any, dict[str, list[Bar]]] = {}
    for bar in bars:
        by_day.setdefault(bar.session, {}).setdefault(bar.symbol, []).append(bar)
    symbols = {bar.symbol for bar in bars}
    if "KRW-BTC" not in symbols:
        raise ValueError("BTC benchmark candles are required in the multi-market CSV")
    result = []
    for day in sorted(by_day):
        by_symbol = by_day[day]
        anchor = by_symbol.get("KRW-BTC", [])
        if not anchor:
            continue
        first_time = anchor[0].timestamp.timetz().replace(tzinfo=None)
        last_time = anchor[-1].timestamp.timetz().replace(tzinfo=None)
        if (len(anchor) < 1_368 or first_time > time(0, 1) or last_time < time(23, 58)):
            continue
        session_bars = [bar for rows in by_symbol.values() for bar in rows]
        result.append(sorted(session_bars, key=lambda bar: (bar.timestamp, bar.symbol)))
    return result


def _split_sessions(days: list[list[Bar]]) -> dict[str, list[Bar]]:
    if len(days) < 6:
        raise ValueError(f"Need at least 6 complete KST sessions; found {len(days)}")
    train_end = max(3, int(len(days) * 0.50))
    validation_end = max(train_end + 1, int(len(days) * 0.67))
    if validation_end >= len(days):
        raise ValueError("Could not form separate train, validation, and holdout sessions")
    return {
        "train": [bar for day in days[:train_end] for bar in day],
        "validation": [bar for day in days[train_end:validation_end] for bar in day],
        "holdout": [bar for day in days[validation_end:] for bar in day],
    }


def _buy_hold(bars: list[Bar], source_config: dict[str, Any]) -> dict[str, float]:
    capital = Decimal(str(source_config["initial_cash_krw"]))
    execution = source_config["execution"]
    fee = Decimal(str(execution["fee_bps_per_side"])) / Decimal(10_000)
    adverse = (
        Decimal(str(execution["fallback_spread_bps_total"])) / Decimal(20_000)
        + Decimal(str(execution["slippage_bps"])) / Decimal(10_000)
    )
    money = Decimal("1")
    by_symbol: dict[str, list[Bar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    budget = capital * Decimal("0.10") / len(by_symbol)
    pnl = Decimal(0)
    for rows in by_symbol.values():
        symbol = rows[0].symbol
        buy_price = _price_tick(Decimal(str(rows[0].open)) * (1 + adverse), ROUND_CEILING, symbol)
        sell_price = _price_tick(Decimal(str(rows[-1].close)) * (1 - adverse), ROUND_FLOOR, symbol)
        qty = (budget / (buy_price * (1 + fee))).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        entry_notional, exit_notional = qty * buy_price, qty * sell_price
        entry_fee = (entry_notional * fee).quantize(money, rounding=ROUND_HALF_UP)
        exit_fee = (exit_notional * fee).quantize(money, rounding=ROUND_HALF_UP)
        pnl += exit_notional - exit_fee - entry_notional - entry_fee
    return {"net_pnl_krw": float(pnl), "net_return_pct": float(pnl / capital * 100)}


def _summary(bars: list[Bar], source_config: dict[str, Any],
             simulation_config: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    result = simulate(
        bars, simulation_config, continuous=True,
        strategy_ids=(strategy_id,), liquidate_at_end=True,
    )
    summary = result["strategies"][strategy_id]
    fields = ("net_pnl_krw", "net_return_pct", "max_drawdown_pct", "closed_trades",
              "win_rate_pct", "profit_factor", "total_costs_krw")
    trades = [trade for trade in result["trades"] if trade["strategy"] == strategy_id]
    return {
        **{key: summary[key] for key in fields},
        "wins": sum(trade["net_pnl_krw"] > 0 for trade in trades),
        "losses": sum(trade["net_pnl_krw"] < 0 for trade in trades),
        "buy_hold_10pct": _buy_hold(bars, source_config),
        "trades": trades,
    }


def _score(summary: dict[str, Any]) -> float:
    return float(summary["net_return_pct"]) - 1.5 * float(summary["max_drawdown_pct"])


def _stress_config(source_config: dict[str, Any]) -> dict[str, Any]:
    stressed = copy.deepcopy(source_config)
    for key in ("fee_bps_per_side", "slippage_bps", "fallback_spread_bps_total"):
        stressed["execution"][key] = float(stressed["execution"][key]) * 2
    return stressed


def _eligible(*, selected_name: str, selected_validation: dict[str, Any],
              baseline_validation: dict[str, Any], selected_holdout: dict[str, Any],
              baseline_holdout: dict[str, Any], selected_stress: dict[str, Any],
              baseline_stress: dict[str, Any]) -> bool:
    return bool(
        selected_name != "baseline"
        and selected_validation["closed_trades"] >= MIN_VALIDATION_TRADES
        and selected_validation["net_return_pct"] > 0
        and selected_validation["net_return_pct"] > baseline_validation["net_return_pct"]
        and selected_holdout["closed_trades"] >= MIN_HOLDOUT_TRADES
        and selected_holdout["net_return_pct"] > 0
        and selected_holdout["net_return_pct"] > baseline_holdout["net_return_pct"]
        and selected_holdout["max_drawdown_pct"] <= MAX_HOLDOUT_DRAWDOWN_PCT
        and selected_stress["closed_trades"] >= MIN_HOLDOUT_TRADES
        and selected_stress["net_return_pct"] > 0
        and selected_stress["net_return_pct"] > baseline_stress["net_return_pct"]
        and selected_stress["max_drawdown_pct"] <= MAX_HOLDOUT_DRAWDOWN_PCT
    )


def replay_short_window_hypotheses(path: str | Path,
                                   source_config: dict[str, Any]) -> dict[str, Any]:
    all_bars = [bar for bar in load_bars(path) if bar.market == "KRW"]
    if not all_bars:
        raise ValueError("CSV contains no KRW crypto candles")
    sessions = _complete_sessions(all_bars)
    windows = _split_sessions(sessions)
    data = [bar for day in sessions for bar in day]
    base_config = _to_simulation_config(source_config)
    stressed_source = _stress_config(source_config)
    stressed_config = _to_simulation_config(stressed_source)
    strategy_results: dict[str, Any] = {}

    for strategy_id in ("A", "B"):
        candidate_config = copy.deepcopy(source_config)
        candidate_config["strategies"][strategy_id].update(VARIANTS[strategy_id]["settings"])
        candidate_simulation_config = _to_simulation_config(candidate_config)

        baseline_train = _summary(windows["train"], source_config, base_config, strategy_id)
        candidate_train = _summary(windows["train"], source_config,
                                   candidate_simulation_config, strategy_id)
        selected_name = "variant" if _score(candidate_train) > _score(baseline_train) else "baseline"
        selected_config = candidate_simulation_config if selected_name == "variant" else base_config

        baseline_validation = _summary(windows["validation"], source_config, base_config, strategy_id)
        selected_validation = _summary(windows["validation"], source_config, selected_config, strategy_id)
        baseline_holdout = _summary(windows["holdout"], source_config, base_config, strategy_id)
        selected_holdout = _summary(windows["holdout"], source_config, selected_config, strategy_id)

        baseline_stress_config = _to_simulation_config(stressed_source)
        if selected_name == "variant":
            selected_stress_source = copy.deepcopy(stressed_source)
            selected_stress_source["strategies"][strategy_id].update(VARIANTS[strategy_id]["settings"])
            selected_stress_config = _to_simulation_config(selected_stress_source)
        else:
            selected_stress_config = baseline_stress_config
        baseline_stress = _summary(windows["holdout"], stressed_source,
                                   baseline_stress_config, strategy_id)
        selected_stress = _summary(windows["holdout"], stressed_source,
                                    selected_stress_config, strategy_id)

        eligible = _eligible(
            selected_name=selected_name,
            selected_validation=selected_validation,
            baseline_validation=baseline_validation,
            selected_holdout=selected_holdout,
            baseline_holdout=baseline_holdout,
            selected_stress=selected_stress,
            baseline_stress=baseline_stress,
        )
        strategy_results[strategy_id] = {
            "hypothesis": VARIANTS[strategy_id]["hypothesis"],
            "variant_name": VARIANTS[strategy_id]["name"],
            "variant_settings": VARIANTS[strategy_id]["settings"],
            "train": {"baseline": baseline_train, "variant": candidate_train,
                      "score_baseline": _score(baseline_train),
                      "score_variant": _score(candidate_train)},
            "selected_on_train": selected_name,
            "validation": {"baseline": baseline_validation, "selected": selected_validation},
            "holdout": {"baseline": baseline_holdout, "selected": selected_holdout},
            "holdout_double_cost_stress": {"baseline": baseline_stress, "selected": selected_stress},
            "promotion_eligible": eligible,
            "promotion_reason": "Passed all chronological, trade-count, return, and double-cost gates"
            if eligible else "Insufficient evidence or failed at least one untouched out-of-sample gate",
        }

    universe_path = Path(path).with_suffix(".universe.json")
    universe_metadata = json.loads(universe_path.read_text(encoding="utf-8")) if universe_path.exists() else None
    symbols_used = sorted({bar.symbol for bar in data})
    symbol_coverage = []
    for symbol in symbols_used:
        rows = [bar for bar in data if bar.symbol == symbol]
        complete_symbol_days = 0
        day_groups: dict[Any, list[Bar]] = {}
        for bar in rows:
            day_groups.setdefault(bar.session, []).append(bar)
        for daily in day_groups.values():
            if (len(daily) >= 1_368
                    and daily[0].timestamp.timetz().replace(tzinfo=None) <= time(0, 1)
                    and daily[-1].timestamp.timetz().replace(tzinfo=None) >= time(23, 58)):
                complete_symbol_days += 1
        symbol_coverage.append({
            "symbol": symbol,
            "bars": len(rows),
            "coverage_pct_of_used_sessions": round(len(rows) / (len(sessions) * 1_440) * 100, 2),
            "complete_sessions": complete_symbol_days,
        })
    return {
        "classification": ("MULTI-COIN COSTED PAPER RESEARCH SCREEN — NOT A LIVE RESULT"
                           if len(symbols_used) > 1 else
                           "SINGLE-COIN COSTED PAPER RESEARCH SCREEN — NOT A LIVE RESULT"),
        "data": {
            "source": str(path),
            "downloaded_bars": len(all_bars),
            "downloaded_start": all_bars[0].timestamp.isoformat(),
            "downloaded_end": all_bars[-1].timestamp.isoformat(),
            "complete_sessions_used": len(sessions),
            "used_bars": len(data),
            "used_start": data[0].timestamp.isoformat(),
            "used_end": data[-1].timestamp.isoformat(),
            "symbols": symbols_used,
            "symbol_coverage": symbol_coverage,
            "partial_sessions_excluded": len({bar.session for bar in all_bars}) - len(sessions),
        },
        "universe_selection": universe_metadata,
        "method": {
            "split": "chronological 50% train / at least 1 session validation / remaining holdout; full KST sessions only",
            "selection": "one predeclared variant per strategy; select on train score = net return - 1.5 * max drawdown",
            "baseline_execution": "5bp fee/side + 5bp slippage/side + 10bp total fallback spread",
            "stress_execution": "double configured fee, slippage, and spread",
            "price_tick_policy": "Upbit KRW price-tier tick sizes, including the 0.5 KRW USDT/USDC tier, applied to simulated fills",
            "data_gap_policy": "clear that symbol's indicator history and pending signal; close an open position at the next observed candle open after gaps over 90 seconds",
            "buy_hold_reference": "invest 10% of initial capital equally across symbols, enter at each symbol's first observed open, exit at its last observed close; reference only",
            "promotion_gates": {
                "minimum_validation_trades": MIN_VALIDATION_TRADES,
                "minimum_holdout_trades": MIN_HOLDOUT_TRADES,
                "require_positive_validation_holdout_and_stress": True,
                "holdout_max_drawdown_pct": MAX_HOLDOUT_DRAWDOWN_PCT,
            },
            "not_promotable_note": "Short data window; repeat only with new future data before any adoption.",
        },
        "windows": {
            name: {"start": bars[0].timestamp.isoformat(), "end": bars[-1].timestamp.isoformat(),
                   "bars": len(bars), "sessions": len({bar.session for bar in bars})}
            for name, bars in windows.items()
        },
        "strategies": strategy_results,
        "any_candidate_eligible": any(row["promotion_eligible"] for row in strategy_results.values()),
    }


def write_short_window_report(report: dict[str, Any], destination: str | Path) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
