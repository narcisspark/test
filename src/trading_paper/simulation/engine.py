from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from trading_paper.simulation.models import Bar, Signal
from trading_paper.simulation.strategies import get_signal

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
MONEY = Decimal("1")
STRATEGY_IDS = ("A", "B", "C")


@dataclass(slots=True)
class Position:
    symbol: str
    market: str
    quantity: Decimal
    entry_time: datetime
    signal_time: datetime
    signal_reason: str
    entry_reference_price: Decimal
    entry_price: Decimal
    entry_notional: Decimal
    entry_commission: Decimal
    entry_execution_cost: Decimal
    stop_price: Decimal
    take_price: Decimal


@dataclass(slots=True)
class Account:
    strategy_id: str
    initial_cash: Decimal
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, Signal] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    session: date | None = None
    session_start_equity: Decimal = ZERO
    entries_today: int = 0
    consecutive_stops: int = 0

    def equity(self) -> Decimal:
        return self.cash + sum((position.quantity * self.marks.get(symbol, position.entry_price) for symbol, position in self.positions.items()), ZERO)

    def exposure(self) -> Decimal:
        return sum((position.quantity * self.marks.get(symbol, position.entry_price) for symbol, position in self.positions.items()), ZERO)


def simulate(
    bars: list[Bar],
    config: dict[str, Any],
    *,
    synthetic: bool = False,
    continuous: bool = False,
    strategy_ids: tuple[str, ...] = STRATEGY_IDS,
    liquidate_at_end: bool = False,
) -> dict[str, Any]:
    if not bars:
        raise ValueError("No bars to simulate")
    if not strategy_ids or any(strategy not in STRATEGY_IDS for strategy in strategy_ids):
        raise ValueError(f"strategy_ids must be a non-empty subset of {STRATEGY_IDS}")
    initial_cash = Decimal(str(config["initial_cash_krw"]))
    accounts = {strategy: Account(strategy, initial_cash, initial_cash) for strategy in strategy_ids}
    histories: dict[str, list[Bar]] = {}
    previous_timestamps: dict[str, datetime] = {}
    per_symbol_indexes: dict[str, list[int]] = {}
    for index, bar in enumerate(bars):
        per_symbol_indexes.setdefault(bar.symbol, []).append(index)
    last_in_session: set[int] = set()
    for indexes in per_symbol_indexes.values():
        for offset, index in enumerate(indexes):
            if offset + 1 == len(indexes) or bars[indexes[offset + 1]].session != bars[index].session:
                last_in_session.add(index)

    execution = config["execution"]
    risk = config["risk"]
    for index, bar in enumerate(bars):
        history = histories.setdefault(bar.symbol, [])
        previous_timestamp = previous_timestamps.get(bar.symbol)
        data_gap = (
            previous_timestamp is not None
            and (bar.timestamp - previous_timestamp).total_seconds() > 90
        )
        if data_gap:
            history.clear()
        if not history or history[-1].session != bar.session:
            history.clear()
        is_last = index in last_in_session

        for account in accounts.values():
            if account.session != bar.session:
                account.session = bar.session
                account.session_start_equity = account.equity()
                account.entries_today = 0
                account.consecutive_stops = 0

            position = account.positions.get(bar.symbol)
            if data_gap:
                account.pending.pop(bar.symbol, None)
                if position is not None:
                    _close(account, position, bar, bar.open, "DATA_GAP", execution)
                position = None
            elif position is not None:
                _manage_position(account, position, bar, execution, risk, is_last, continuous)

            pending = account.pending.pop(bar.symbol, None)
            max_pending_seconds = float(config.get("feed", {}).get("pending_signal_max_seconds", 120))
            if pending is not None and _pending_signal_is_fresh(pending, bar, max_pending_seconds):
                if bar.symbol not in account.positions and _entries_allowed(account, risk):
                    _try_entry(account, pending, bar, execution, risk)
                    position = account.positions.get(bar.symbol)
                    if position is not None:
                        _manage_position(account, position, bar, execution, risk, is_last, continuous)

            account.marks[bar.symbol] = _d(bar.close)

        history.append(bar)
        for account in accounts.values():
            if bar.symbol in account.positions or bar.symbol in account.pending or not _entries_allowed(account, risk):
                continue
            signal = get_signal(account.strategy_id, bar, history, config)
            if signal is not None:
                account.pending[bar.symbol] = signal

        for account in accounts.values():
            account.equity_curve.append((bar.timestamp, account.equity()))
        previous_timestamps[bar.symbol] = bar.timestamp

    if liquidate_at_end:
        final_bars = {bar.symbol: bar for bar in bars}
        for account in accounts.values():
            for position in list(account.positions.values()):
                final_bar = final_bars[position.symbol]
                _close(account, position, final_bar, final_bar.close, "PERIOD_END", execution)
            account.equity_curve.append((bars[-1].timestamp, account.equity()))

    summaries: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    for strategy_id, account in accounts.items():
        summaries[strategy_id] = _summarize(account, synthetic)
        trades.extend({**trade, "synthetic_demo": synthetic} for trade in account.trades)
    return {
        "classification": "SYNTHETIC/DEMO — NOT PERFORMANCE EVIDENCE" if synthetic else "HISTORICAL REPLAY — SIMULATION, NOT A LIVE RESULT",
        "synthetic_demo": synthetic,
        "continuous_crypto": continuous,
        "input_rows_complete": len(bars),
        "first_timestamp": _iso(bars[0].timestamp),
        "last_timestamp": _iso(bars[-1].timestamp),
        "symbols": sorted({bar.symbol for bar in bars}),
        "sessions": sorted({bar.session.isoformat() for bar in bars}),
        "config": config,
        "strategies": summaries,
        "trades": trades,
    }


def _manage_position(account: Account, position: Position, bar: Bar, execution: dict, risk: dict, is_last: bool, continuous: bool) -> None:
    if position.symbol not in account.positions:
        return
    if not continuous and position.entry_time.date() != bar.session:
        _close(account, position, bar, bar.open, "SESSION_BOUNDARY", execution)
        return

    hold_minutes = (bar.timestamp - position.entry_time).total_seconds() / 60
    if hold_minutes >= float(risk["max_holding_minutes"]):
        _close(account, position, bar, bar.open, "MAX_HOLDING_TIME", execution)
        return

    low, high, opening = _d(bar.low), _d(bar.high), _d(bar.open)
    stop_hit = low <= position.stop_price
    take_hit = high >= position.take_price
    # OHLC does not reveal the intrabar path. If both levels are touched, apply stop first.
    if stop_hit:
        reference = min(opening, position.stop_price)
        _close(account, position, bar, float(reference), "STOP_LOSS", execution)
        return
    if take_hit:
        reference = max(opening, position.take_price)
        _close(account, position, bar, float(reference), "TAKE_PROFIT", execution)
        return
    if is_last and not continuous:
        _close(account, position, bar, bar.close, "SESSION_END", execution)


def _try_entry(account: Account, signal: Signal, bar: Bar, execution: dict, risk: dict) -> None:
    if len(account.positions) >= int(risk["max_positions"]):
        return
    entry_reference = _d(bar.open)
    max_round_trip_cost_pct = risk.get("max_round_trip_cost_pct")
    if (max_round_trip_cost_pct is not None
            and _estimated_round_trip_cost_pct(entry_reference, bar.symbol, bar.market, execution)
            > _d(max_round_trip_cost_pct)):
        return
    raw_entry = _execution_price(entry_reference, Side.BUY, execution)
    equity = account.equity()
    if equity <= ZERO:
        return
    position_cap = equity * _d(risk["max_position_equity_pct"])
    exposure_room = equity * _d(risk["max_total_exposure_pct"]) - account.exposure()
    cash_room = account.cash
    stop_distance = raw_entry * _d(risk["stop_loss_pct"])
    round_trip_cost_rate = (
        _d(execution["spread_bps_total"]) / BPS
        + 2 * _d(execution["slippage_bps"]) / BPS
        + 2 * _d(execution["commission_bps_per_side"]) / BPS
        + _d(execution["sell_tax_bps"][bar.market]) / BPS
    )
    risk_per_share = stop_distance + raw_entry * round_trip_cost_rate
    risk_budget = equity * _d(risk["risk_per_trade_pct"])
    per_share_cash = raw_entry * (ONE + _d(execution["commission_bps_per_side"]) / BPS)
    quantity = min(
        _floor_quantity(position_cap / raw_entry, bar.market),
        _floor_quantity(exposure_room / raw_entry, bar.market),
        _floor_quantity(cash_room / per_share_cash, bar.market),
        _floor_quantity(risk_budget / risk_per_share, bar.market) if risk_per_share > ZERO else ZERO,
    )
    if quantity <= 0:
        return
    entry_price = _price_tick(raw_entry, ROUND_CEILING, bar.symbol)
    notional = entry_price * quantity
    commission = _money(notional * _d(execution["commission_bps_per_side"]) / BPS)
    execution_cost = max(ZERO, entry_price-entry_reference) * quantity
    total_cash = notional + commission
    if total_cash > account.cash:
        return
    account.cash -= total_cash
    position = Position(
        symbol=bar.symbol,
        market=bar.market,
        quantity=quantity,
        entry_time=bar.timestamp,
        signal_time=signal.timestamp,
        signal_reason=signal.reason,
        entry_reference_price=entry_reference,
        entry_price=entry_price,
        entry_notional=notional,
        entry_commission=commission,
        entry_execution_cost=execution_cost,
        stop_price=_price_tick(entry_price * (ONE - _d(risk["stop_loss_pct"])), symbol=bar.symbol),
        take_price=_price_tick(entry_price * (ONE + _d(risk["take_profit_pct"])), symbol=bar.symbol),
    )
    account.positions[bar.symbol] = position
    account.entries_today += 1


def _close(account: Account, position: Position, bar: Bar, reference_price: float, reason: str, execution: dict) -> None:
    if position.symbol not in account.positions:
        return
    exit_price = _price_tick(_execution_price(reference_price, Side.SELL, execution), ROUND_FLOOR,
                             position.symbol)
    gross = exit_price * position.quantity
    exit_execution_cost = max(ZERO, _d(reference_price)-exit_price) * position.quantity
    commission = _money(gross * _d(execution["commission_bps_per_side"]) / BPS)
    tax = _money(gross * _d(execution["sell_tax_bps"][position.market]) / BPS)
    exit_costs = commission + tax
    account.cash += gross - exit_costs
    net_pnl = gross - exit_costs - position.entry_notional - position.entry_commission
    entry_costs = position.entry_commission
    estimated_execution_costs = position.entry_execution_cost + exit_execution_cost
    total_costs = entry_costs + exit_costs + estimated_execution_costs
    account.trades.append({
        "strategy": account.strategy_id,
        "symbol": position.symbol,
        "market": position.market,
        "signal_time": _iso(position.signal_time),
        "entry_time": _iso(position.entry_time),
        "exit_time": _iso(bar.timestamp),
        "quantity": _number(position.quantity),
        "entry_price_krw": _number(position.entry_price),
        "exit_price_krw": _number(exit_price),
        "gross_market_move_pnl_krw": _number((_d(reference_price)-position.entry_reference_price)*position.quantity),
        "gross_price_pnl_krw": _number(gross - position.entry_notional),
        "commission_krw": _number(position.entry_commission + commission),
        "sell_tax_krw": _number(tax),
        "estimated_spread_slippage_krw": _number(estimated_execution_costs),
        "total_costs_krw": _number(total_costs),
        "net_pnl_krw": _number(net_pnl),
        "net_return_pct": _number(net_pnl / position.entry_notional * 100) if position.entry_notional else 0,
        "exit_reason": reason,
        "signal_reason": position.signal_reason,
    })
    if reason == "STOP_LOSS":
        account.consecutive_stops += 1
    elif net_pnl > ZERO:
        account.consecutive_stops = 0
    account.positions.pop(position.symbol)


def _entries_allowed(account: Account, risk: dict) -> bool:
    if account.session_start_equity <= ZERO:
        return False
    if account.equity() <= account.session_start_equity * (ONE - _d(risk["daily_loss_stop_pct"])):
        return False
    if account.entries_today >= int(risk["max_entries_per_day"]):
        return False
    if account.consecutive_stops >= int(risk["max_consecutive_stops"]):
        return False
    return True


def _pending_signal_is_fresh(signal: Signal, bar: Bar, max_age_seconds: float) -> bool:
    """Only fill on a later bar while the signal remains within its configured TTL."""
    age = (bar.timestamp - signal.timestamp).total_seconds()
    return 0 < age <= max_age_seconds and signal.timestamp.date() == bar.session


def _execution_price(reference: float | Decimal, side: str, execution: dict) -> Decimal:
    ref = _d(reference)
    half_spread = _d(execution["spread_bps_total"]) / 2
    slippage = _d(execution["slippage_bps"])
    adverse_bps = (half_spread + slippage) / BPS
    return ref * (ONE + adverse_bps if side == "BUY" else ONE - adverse_bps)


def _estimated_round_trip_cost_pct(reference: Decimal, symbol: str, market: str,
                                   execution: dict) -> Decimal:
    """Estimate flat-price round-trip fee and tick-rounded execution cost in percent."""
    reference = _d(reference)
    if reference <= ZERO:
        return Decimal("Infinity")
    buy = _price_tick(_execution_price(reference, Side.BUY, execution), ROUND_CEILING, symbol)
    sell = _price_tick(_execution_price(reference, Side.SELL, execution), ROUND_FLOOR, symbol)
    fee_rate = _d(execution["commission_bps_per_side"]) / BPS
    tax_rate = _d(execution["sell_tax_bps"].get(market, 0)) / BPS
    fees_and_tax = buy * fee_rate + sell * (fee_rate + tax_rate)
    return max(ZERO, (buy - sell + fees_and_tax) / reference * Decimal("100"))


class Side:
    BUY = "BUY"
    SELL = "SELL"


def _summarize(account: Account, synthetic: bool) -> dict[str, Any]:
    equity = account.equity()
    total_pnl = equity - account.initial_cash
    pnl_values = [_d(trade["net_pnl_krw"]) for trade in account.trades]
    winners = [pnl for pnl in pnl_values if pnl > ZERO]
    losers = [pnl for pnl in pnl_values if pnl < ZERO]
    gross_wins = sum(winners, ZERO)
    gross_losses = -sum(losers, ZERO)
    running_peak = account.initial_cash
    max_drawdown = ZERO
    for _, value in account.equity_curve:
        running_peak = max(running_peak, value)
        if running_peak > ZERO:
            max_drawdown = max(max_drawdown, (running_peak - value) / running_peak)
    return {
        "strategy": account.strategy_id,
        "initial_cash_krw": _number(account.initial_cash),
        "final_equity_krw": _number(equity),
        "open_positions_marked_at_last_close": len(account.positions),
        "net_pnl_krw": _number(total_pnl),
        "net_return_pct": _number(total_pnl / account.initial_cash * 100),
        "max_drawdown_pct": _number(max_drawdown * 100),
        "closed_trades": len(account.trades),
        "win_rate_pct": _number(Decimal(len(winners)) / len(pnl_values) * 100) if pnl_values else 0,
        "average_closed_trade_net_pnl_krw": _number(sum(pnl_values, ZERO) / len(pnl_values)) if pnl_values else 0,
        "profit_factor": _number(gross_wins / gross_losses) if gross_losses > ZERO else (None if gross_wins == ZERO else "infinite"),
        "total_costs_krw": sum(trade["total_costs_krw"] for trade in account.trades),
        "classification": "SYNTHETIC/DEMO — NOT PERFORMANCE EVIDENCE" if synthetic else "HISTORICAL REPLAY — SIMULATION ONLY",
    }


def _floor(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding="ROUND_FLOOR")))


def _floor_quantity(value: Decimal, market: str) -> Decimal:
    """Keep whole shares for stocks and Upbit's 8-decimal amount precision for crypto."""
    if market == "KRW":
        return max(ZERO, value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
    return Decimal(_floor(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _price_tick(value: Decimal, rounding: str = ROUND_HALF_UP,
                symbol: str = "") -> Decimal:
    """Round a KRW market price to Upbit's current price-tier tick size."""
    price = abs(value)
    if symbol in {"KRW-USDT", "KRW-USDC"} and Decimal("1000") <= price < Decimal("10000"):
        tick = Decimal("0.5")
    elif price >= Decimal("1000000"):
        tick = Decimal("1000")
    elif price >= Decimal("500000"):
        tick = Decimal("500")
    elif price >= Decimal("100000"):
        tick = Decimal("100")
    elif price >= Decimal("50000"):
        tick = Decimal("50")
    elif price >= Decimal("10000"):
        tick = Decimal("10")
    elif price >= Decimal("5000"):
        tick = Decimal("5")
    elif price >= Decimal("1000"):
        tick = Decimal("1")
    elif price >= Decimal("100"):
        tick = Decimal("1")
    elif price >= Decimal("10"):
        tick = Decimal("0.1")
    elif price >= Decimal("1"):
        tick = Decimal("0.01")
    elif price >= Decimal("0.1"):
        tick = Decimal("0.001")
    elif price >= Decimal("0.01"):
        tick = Decimal("0.0001")
    elif price >= Decimal("0.001"):
        tick = Decimal("0.00001")
    elif price >= Decimal("0.0001"):
        tick = Decimal("0.000001")
    elif price >= Decimal("0.00001"):
        tick = Decimal("0.0000001")
    else:
        tick = Decimal("0.00000001")
    rounded_units = (value / tick).to_integral_value(rounding=rounding)
    return rounded_units * tick


def _d(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _number(value: Decimal) -> float:
    return float(value)


def _iso(value: datetime) -> str:
    return value.isoformat()
