"""Longer-window OHLCV proxy for the trend-pullback hypothesis.

This is deliberately labeled as a proxy: candle direction and volume stand in
for archived order-flow, so it cannot validate the live order-book strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_paper.simulation.csvio import load_bars
from trading_paper.simulation.models import Bar
from trading_paper.simulation.pullback_orderflow_replay import _indicators

CAPITAL = Decimal("10000000")
FEE_PER_SIDE = Decimal("0.0005")
ADVERSE_PER_SIDE = Decimal("0.001")  # 5bp half-spread assumption + 5bp slippage
POSITION_LIMIT = Decimal("0.10")
RISK_LIMIT = Decimal("0.001")
DAILY_STOP = Decimal("0.005")
MAX_DRAWDOWN = Decimal("0.02")
MAX_HOLD_SECONDS = 20 * 60
COOLDOWN_SECONDS = 5 * 60
KST = ZoneInfo("Asia/Seoul")


@dataclass
class Setup:
    high: float
    high_index: int
    atr: float
    low: float


@dataclass
class Position:
    quantity: Decimal
    entry_time: datetime
    entry_reference: Decimal
    entry_price: Decimal
    entry_notional: Decimal
    entry_fee: Decimal
    entry_execution_cost: Decimal
    stop: Decimal
    target: Decimal
    signal_time: datetime


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _quantity(value: Decimal) -> Decimal:
    return max(Decimal(0), value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))


def _close_position(account: dict[str, Any], position: Position, bar: Bar,
                    reference_price: float, reason: str) -> None:
    reference = Decimal(str(reference_price))
    exit_price = _price(reference * (1 - ADVERSE_PER_SIDE))
    exit_notional = exit_price * position.quantity
    exit_fee = _money(exit_notional * FEE_PER_SIDE)
    exit_execution = max(Decimal(0), reference - exit_price) * position.quantity
    account["cash"] += exit_notional - exit_fee
    pnl = exit_notional - exit_fee - position.entry_notional - position.entry_fee
    total_cost = position.entry_fee + exit_fee + position.entry_execution_cost + exit_execution
    account["trades"].append({
        "signal_time": position.signal_time.isoformat(),
        "entry_time": position.entry_time.isoformat(),
        "exit_time": bar.timestamp.isoformat(),
        "quantity": float(position.quantity),
        "entry_price_krw": float(position.entry_price),
        "exit_price_krw": float(exit_price),
        "gross_market_move_pnl_krw": float((reference - position.entry_reference) * position.quantity),
        "commission_krw": float(position.entry_fee + exit_fee),
        "estimated_spread_slippage_krw": float(position.entry_execution_cost + exit_execution),
        "total_costs_krw": float(total_cost),
        "net_pnl_krw": float(pnl),
        "net_return_pct": float(pnl / position.entry_notional * 100) if position.entry_notional else 0,
        "exit_reason": reason,
    })
    if pnl < 0:
        account["consecutive_losses"] += 1
    elif pnl > 0:
        account["consecutive_losses"] = 0
    if account["consecutive_losses"] >= 3:
        account["cooldown_until"] = bar.timestamp.timestamp() + 3600
    account["cooldown_until"] = max(
        account["cooldown_until"], bar.timestamp.timestamp() + COOLDOWN_SECONDS
    )


def _equity(account: dict[str, Any], bar: Bar, position: Position | None) -> Decimal:
    if position is None:
        return account["cash"]
    reference = Decimal(str(bar.close))
    marked_exit = _price(reference * (1 - ADVERSE_PER_SIDE))
    notional = marked_exit * position.quantity
    return account["cash"] + notional - _money(notional * FEE_PER_SIDE)


def _simulate_window(bars: list[Bar], start: int, end: int, indicators) -> dict[str, Any]:
    epochs, ema20, ema60, atrs = indicators
    account = {
        "cash": CAPITAL,
        "trades": [],
        "equity_curve": [],
        "peak": CAPITAL,
        "max_drawdown": Decimal(0),
        "day": None,
        "day_start_equity": CAPITAL,
        "daily_stop": False,
        "entries_today": 0,
        "consecutive_losses": 0,
        "cooldown_until": 0.0,
        "entry_attempts_evaluated": 0,
        "cost_gate_passes": 0,
        "positions_opened": 0,
        "gap_resets": 0,
        "run_loss_stop": False,
    }
    position: Position | None = None
    setup: Setup | None = None
    pending: dict[str, Any] | None = None
    previous_epoch = epochs[start - 1] if start > 0 else None
    contiguous_bars = 64 if start > 0 else 0

    for i in range(start, end):
        bar = bars[i]
        epoch = epochs[i]
        session = bar.timestamp.astimezone(KST).date()
        current_equity = _equity(account, bar, position)
        if session != account["day"]:
            account["day"] = session
            account["day_start_equity"] = current_equity
            account["daily_stop"] = False
            account["entries_today"] = 0
            setup = None
            pending = None
        gap = previous_epoch is not None and epoch - previous_epoch > 90
        if gap:
            setup = None
            account["gap_resets"] += 1
            contiguous_bars = 1
            if pending is not None:
                pending = None
            if position is not None:
                _close_position(account, position, bar, bar.open, "DATA_GAP")
                position = None
        else:
            contiguous_bars += 1
        previous_epoch = epoch

        if pending is not None:
            candidate = pending
            pending = None
            reference = Decimal(str(bar.open))
            if reference <= Decimal(str(candidate["stop"])):
                pass
            elif float(reference) > candidate["signal_close"] + 0.25 * candidate["atr"]:
                pass
            else:
                entry_price = _price(reference * (1 + ADVERSE_PER_SIDE))
                stop = Decimal(str(candidate["stop"]))
                distance_rate = float((entry_price - stop) / entry_price)
                estimated_cost_rate = float(2 * (FEE_PER_SIDE + ADVERSE_PER_SIDE))
                if (0.0025 <= distance_rate <= 0.012
                        and 2 * distance_rate >= 3 * estimated_cost_rate):
                    account["cost_gate_passes"] += 1
                    risk_cap = min(current_equity, CAPITAL) * RISK_LIMIT
                    stop_exit = stop * (1 - ADVERSE_PER_SIDE) * (1 - FEE_PER_SIDE)
                    per_unit_risk = entry_price * (1 + FEE_PER_SIDE) - stop_exit
                    if per_unit_risk > 0:
                        risk_quantity = risk_cap / per_unit_risk
                        cap_quantity = current_equity * POSITION_LIMIT / (entry_price * (1 + FEE_PER_SIDE))
                        cash_quantity = account["cash"] / (entry_price * (1 + FEE_PER_SIDE))
                        qty = _quantity(min(risk_quantity, cap_quantity, cash_quantity))
                        notional = entry_price * qty
                        entry_fee = _money(notional * FEE_PER_SIDE)
                        if qty > 0 and notional + entry_fee <= account["cash"]:
                            stop_distance = float(entry_price) - candidate["stop"]
                            target = Decimal(str(float(entry_price) + 2 * stop_distance))
                            account["cash"] -= notional + entry_fee
                            account["positions_opened"] += 1
                            position = Position(
                                quantity=qty, entry_time=bar.timestamp, entry_reference=reference,
                                entry_price=entry_price, entry_notional=notional, entry_fee=entry_fee,
                                entry_execution_cost=(entry_price-reference)*qty, stop=stop, target=target,
                                signal_time=candidate["signal_time"],
                            )
                            account["entries_today"] += 1
                account["entry_attempts_evaluated"] += 1

        if position is not None:
            low, high, opening = Decimal(str(bar.low)), Decimal(str(bar.high)), Decimal(str(bar.open))
            held = (bar.timestamp - position.entry_time).total_seconds()
            reason, reference = None, None
            if held >= MAX_HOLD_SECONDS:
                if opening <= position.stop:
                    reason, reference = "STOP_LOSS", opening
                elif opening >= position.target:
                    reason, reference = "TAKE_PROFIT", opening
                else:
                    reason, reference = "MAX_HOLD_20M", opening
            elif low <= position.stop:
                reason, reference = "STOP_LOSS", min(opening, position.stop)
            elif high >= position.target:
                reason, reference = "TAKE_PROFIT", max(opening, position.target)
            if reason:
                _close_position(account, position, bar, float(reference), reason)
                position = None

        equity = _equity(account, bar, position)
        account["peak"] = max(account["peak"], equity)
        if account["peak"] > 0:
            account["max_drawdown"] = max(
                account["max_drawdown"], (account["peak"] - equity) / account["peak"]
            )
        if equity <= account["day_start_equity"] * (1 - DAILY_STOP):
            account["daily_stop"] = True
        if equity <= CAPITAL * (1 - MAX_DRAWDOWN):
            account["run_loss_stop"] = True
        account["equity_curve"].append((epoch, equity))

        if (position is not None or pending is not None or account["daily_stop"]
                or account["run_loss_stop"]):
            continue
        if account["entries_today"] >= 10 or account["consecutive_losses"] >= 3:
            continue
        if (epoch < account["cooldown_until"] or i < 64 or contiguous_bars < 64
                or i + 1 >= end or atrs[i] <= 0):
            continue
        if ema20[i] <= ema60[i] or ema20[i] <= ema20[i - 5] or bar.close < ema60[i]:
            setup = None
            continue

        lookback = range(max(0, i - 10), i)
        if not lookback:
            continue
        high_index = max(lookback, key=lambda j: bars[j].high)
        swing_high = bars[high_index].high
        age = i - high_index
        atr = atrs[i]
        if setup is not None and (
            age > 5 or bar.low < setup.high - 1.5 * setup.atr
        ):
            setup = None
        if setup is not None:
            setup.low = min(setup.low, bar.low)
        pullback = swing_high - bar.close
        if setup is None and age <= 5 and 0.5 * atr <= pullback <= 1.5 * atr:
            setup = Setup(swing_high, high_index, atr, bar.low)
        if setup is None:
            continue
        prior_high = max(item.high for item in bars[max(0, i - 3):i])
        prior_volumes = [item.volume for item in bars[max(0, i - 5):i]]
        average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
        recovery = (
            bar.close > prior_high
            and bar.close > bar.open
            and average_volume > 0
            and bar.volume >= average_volume * 1.2
        )
        if not recovery:
            continue
        stop = setup.low - 0.2 * setup.atr
        distance_rate = (bar.close - stop) / bar.close
        estimated_cost_rate = float(2 * (FEE_PER_SIDE + ADVERSE_PER_SIDE))
        if not (0.0025 <= distance_rate <= 0.012
                and 2 * distance_rate >= 3 * estimated_cost_rate):
            setup = None
            continue
        pending = {
            "signal_time": bar.timestamp, "signal_close": bar.close,
            "stop": stop, "atr": setup.atr,
        }
        setup = None

    if position is not None:
        last = bars[end - 1]
        _close_position(account, position, last, last.close, "PERIOD_END")
        position = None
    final_equity = account["cash"]
    account["peak"] = max(account["peak"], final_equity)
    if account["peak"] > 0:
        account["max_drawdown"] = max(
            account["max_drawdown"], (account["peak"] - final_equity) / account["peak"]
        )
    if final_equity <= CAPITAL * (1 - MAX_DRAWDOWN):
        account["run_loss_stop"] = True
    if account["equity_curve"]:
        account["equity_curve"][-1] = (account["equity_curve"][-1][0], final_equity)
    pnl_values = [Decimal(str(trade["net_pnl_krw"])) for trade in account["trades"]]
    winners = [value for value in pnl_values if value > 0]
    losers = [value for value in pnl_values if value < 0]
    return {
        "start": bars[start].timestamp.isoformat(),
        "end": bars[end - 1].timestamp.isoformat(),
        "bars": end - start,
        "sessions": len({bar.session for bar in bars[start:end]}),
        "initial_cash_krw": float(CAPITAL),
        "final_equity_krw": float(final_equity),
        "net_pnl_krw": float(final_equity - CAPITAL),
        "net_return_pct": float((final_equity / CAPITAL - 1) * 100),
        "max_drawdown_pct": float(account["max_drawdown"] * 100),
        "entry_attempts_evaluated": account["entry_attempts_evaluated"],
        "cost_gate_passes": account["cost_gate_passes"],
        "positions_opened": account["positions_opened"],
        "closed_trades": len(pnl_values),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_pct": 100 * len(winners) / len(pnl_values) if pnl_values else 0,
        "profit_factor": float(sum(winners, Decimal(0)) / -sum(losers, Decimal(0))) if losers else None,
        "total_costs_krw": sum(trade["total_costs_krw"] for trade in account["trades"]),
        "gap_resets": account["gap_resets"],
        "run_loss_stop_hit": account["run_loss_stop"],
        "trades": account["trades"],
    }


def _buy_hold(bars: list[Bar]) -> dict[str, float]:
    first, last = bars[0], bars[-1]
    buy_price = Decimal(str(first.open)) * (1 + ADVERSE_PER_SIDE)
    sell_price = Decimal(str(last.close)) * (1 - ADVERSE_PER_SIDE)
    budget = CAPITAL * Decimal("0.10")
    qty = _quantity(budget / (buy_price * (1 + FEE_PER_SIDE)))
    entry_notional, exit_notional = qty * buy_price, qty * sell_price
    pnl = exit_notional - _money(exit_notional * FEE_PER_SIDE) - entry_notional - _money(entry_notional * FEE_PER_SIDE)
    return {"net_pnl_krw": float(pnl), "net_return_pct": float(pnl / CAPITAL * 100)}


def replay_ohlcv_pullback(path: str | Path) -> dict[str, Any]:
    bars = [bar for bar in load_bars(path) if bar.symbol == "KRW-BTC" and bar.market == "KRW"]
    sessions: dict[Any, list[int]] = {}
    for index, bar in enumerate(bars):
        sessions.setdefault(bar.session, []).append(index)
    days = [sessions[key] for key in sorted(sessions)]
    if len(days) < 30:
        raise ValueError(f"Need at least 30 KST sessions; found {len(days)}")
    train_end = int(len(days) * 0.60)
    validation_end = int(len(days) * 0.80)
    if train_end < 1 or validation_end <= train_end or validation_end >= len(days):
        raise ValueError("Could not form chronological train/validation/holdout windows")
    indicators = _indicators(bars)

    def evaluate(day_slice):
        start, end = day_slice[0][0], day_slice[-1][-1] + 1
        result = _simulate_window(bars, start, end, indicators)
        result["buy_hold_10pct"] = _buy_hold(bars[start:end])
        return result

    windows = {
        "train": evaluate(days[:train_end]),
        "validation": evaluate(days[train_end:validation_end]),
        "holdout": evaluate(days[validation_end:]),
    }
    return {
        "classification": "OHLCV PROXY HISTORICAL REPLAY — NO ARCHIVED ORDERFLOW; NOT PROMOTABLE",
        "strategy": "EMA20/60 trend + 0.5-1.5 ATR pullback + bullish volume-confirmed 3-bar recovery",
        "data": {"source": str(path), "bars": len(bars), "sessions": len(days),
                 "start": bars[0].timestamp.isoformat(), "end": bars[-1].timestamp.isoformat()},
        "policy": {
            "initial_cash_krw_per_window": str(CAPITAL),
            "ema_periods": [20, 60], "pullback_atr_range": [0.5, 1.5],
            "pullback_expiry_bars": 5, "entry_confirmation": "close above prior 3-bar high, bullish candle, volume >= 1.2x prior 5-bar average",
            "stop_atr_below_pullback_low": 0.2, "take_profit_risk_multiple": 2,
            "max_holding_minutes": MAX_HOLD_SECONDS // 60, "risk_per_trade_pct": float(RISK_LIMIT * 100),
            "max_position_pct": float(POSITION_LIMIT * 100), "daily_loss_stop_pct": float(DAILY_STOP * 100),
            "maximum_run_drawdown_stop_pct": float(MAX_DRAWDOWN * 100),
            "fee_pct_per_side": float(FEE_PER_SIDE * 100),
            "spread_half_bps_per_side": 5, "slippage_bps_per_side": 5,
            "ohlcv_execution": "next 1m open; if stop and target both touched in a bar, stop fills first",
        },
        "windows": windows,
        "promotion_eligible": False,
        "promotion_reason": "This OHLCV proxy has no true orderflow inputs and is an exploratory branch only.",
    }


def write_ohlcv_pullback_report(report: dict[str, Any], destination: str | Path) -> Path:
    import json

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
