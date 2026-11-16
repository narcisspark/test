"""Deterministic KRX stock paper replay with rule-only A and LLM-gated B.

This module only simulates local integer-share fills from completed bars. It
contains no provider or broker calls and cannot place a real order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
STRATEGIES = ("A", "B")
ALLOWED_LLM_ACTIONS = {"ALLOW_ENTRY", "WAIT", "REJECT_ENTRY"}


@dataclass(frozen=True, slots=True)
class StockBar:
    timestamp: datetime
    symbol: str
    market: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover_krw: Decimal | None
    complete: bool

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Stock bar timestamp must include a timezone")
        if self.timestamp.utcoffset() != self.timestamp.astimezone(ZoneInfo("Asia/Seoul")).utcoffset():
            raise ValueError("Stock bar timestamp must be supplied in the Asia/Seoul timezone")
        if len(self.symbol) != 6 or not self.symbol.isascii() or not self.symbol.isdigit():
            raise ValueError("Stock bar symbol must be a six-digit Korean stock code")
        if self.market != "KRX":
            raise ValueError("Only KRX regular-session stock bars are supported")
        values = [self.open, self.high, self.low, self.close, self.volume]
        if self.turnover_krw is not None:
            values.append(self.turnover_krw)
        if not all(value.is_finite() for value in values):
            raise ValueError("Stock bar numeric fields must be finite")
        if min(self.open, self.high, self.low, self.close) <= ZERO or self.volume < ZERO:
            raise ValueError("Stock bar prices must be positive and volume non-negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid stock bar OHLC range")
        if self.turnover_krw is not None and self.turnover_krw < ZERO:
            raise ValueError("Stock bar turnover cannot be negative")

    @property
    def session(self) -> date:
        return self.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()


@dataclass(frozen=True, slots=True)
class EntrySignal:
    symbol: str
    timestamp: datetime
    snapshot_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class LLMDecision:
    snapshot_id: str
    symbol: str
    action: str
    received_at: datetime
    expires_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if self.action not in ALLOWED_LLM_ACTIONS:
            raise ValueError("LLM action must be ALLOW_ENTRY, WAIT, or REJECT_ENTRY")
        if (self.received_at.tzinfo is None or self.received_at.utcoffset() is None
                or self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None):
            raise ValueError("LLM decision timestamps must be timezone-aware")
        if self.expires_at <= self.received_at:
            raise ValueError("LLM decision must expire after it is received")

    @classmethod
    def from_payload(cls, payload: dict[str, Any], expected_snapshot_id: str,
                     expected_symbol: str) -> "LLMDecision":
        if payload.get("schema_version") != "1.0":
            raise ValueError("Unsupported LLM decision schema")
        if payload.get("snapshot_id") != expected_snapshot_id:
            raise ValueError("LLM decision snapshot does not match the candidate")
        if payload.get("symbol") != expected_symbol or payload.get("action") not in ALLOWED_LLM_ACTIONS:
            raise ValueError("LLM decision is missing an allowed symbol/action")
        return cls(
            snapshot_id=expected_snapshot_id,
            symbol=expected_symbol,
            action=payload["action"],
            received_at=datetime.fromisoformat(payload["received_at"]),
            expires_at=datetime.fromisoformat(payload["expires_at"]),
            reason=str(payload.get("reason", ""))[:240],
        )


@dataclass(slots=True)
class _Position:
    symbol: str
    quantity: int
    entry_time: datetime
    entry_price: Decimal
    entry_notional: Decimal
    entry_commission: Decimal
    entry_price_impact: Decimal
    stop_price: Decimal
    take_price: Decimal
    signal: EntrySignal


@dataclass(slots=True)
class _Account:
    strategy: str
    initial_cash: Decimal
    cash: Decimal
    positions: dict[str, _Position] = field(default_factory=dict)
    pending: dict[str, EntrySignal] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_points: list[Decimal] = field(default_factory=list)
    session: date | None = None
    session_opening_equity: Decimal = ZERO
    entries_today: int = 0
    stopped_today: int = 0
    cooldown_until: dict[str, datetime] = field(default_factory=dict)

    def equity(self) -> Decimal:
        marked = sum((p.quantity * self.marks.get(s, p.entry_price) for s, p in self.positions.items()), ZERO)
        return self.cash + marked

    def exposure(self) -> Decimal:
        return sum((p.quantity * self.marks.get(s, p.entry_price) for s, p in self.positions.items()), ZERO)


def simulate_stock(
    bars: list[StockBar], config: dict[str, Any], *,
    llm_decisions: dict[str, LLMDecision] | None = None,
) -> dict[str, Any]:
    """Replay completed KRX bars; B enters only with a fresh matching LLM allow."""
    if not bars:
        raise ValueError("No KRX bars supplied")
    if any(not bar.complete for bar in bars):
        raise ValueError("Incomplete or unverified bars cannot be used for entry signals")
    if bars != sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        raise ValueError("Stock bars must be sorted by timestamp then symbol")
    if not config.get("enabled", False):
        raise ValueError("N2 stock paper engine is disabled until its data gate is approved")
    if config.get("market") != "KRX":
        raise ValueError("Only KRX is supported")
    interval_minutes = int(config.get("bar_interval_minutes", 0))
    if interval_minutes != 5:
        raise ValueError("This stock replay supports five-minute bars only")
    seen_bars: set[tuple[str, datetime]] = set()
    for bar in bars:
        key = (bar.symbol, bar.timestamp.astimezone(timezone.utc))
        if key in seen_bars:
            raise ValueError("Duplicate stock bar for symbol and instant")
        seen_bars.add(key)
    gate = config.get("data_gate", {})
    required_gates = ("quote_freshness_verified", "market_status_codes_verified",
                      "five_minute_bar_completion_verified", "instrument_master_filter_verified")
    if not all(gate.get(name) is True for name in required_gates):
        raise ValueError("N2 stock data gate is incomplete; no simulation entries are allowed")

    initial = _d(config["initial_cash_krw"])
    accounts = {name: _Account(name, initial, initial) for name in STRATEGIES}
    decisions = llm_decisions or {}
    risk, execution, strategy = config["risk"], config["execution"], config["strategy"]
    history: dict[str, list[StockBar]] = {}
    last_in_session: set[int] = set()
    indexes: dict[tuple[str, date], list[int]] = {}
    for index, bar in enumerate(bars):
        indexes.setdefault((bar.symbol, bar.session), []).append(index)
    for positions in indexes.values():
        last_in_session.add(positions[-1])

    llm_counts = {"ALLOW_ENTRY": 0, "WAIT": 0, "REJECT_ENTRY": 0,
                  "MISSING_OR_EXPIRED": 0, "ALLOW_EXPIRED_BEFORE_FILL": 0}
    generated_signals = 0
    entry_signals: list[dict[str, str]] = []
    decision_ttl = timedelta(minutes=int(config.get("llm", {}).get("decision_ttl_minutes", 5)))
    offset = 0
    while offset < len(bars):
        end = offset + 1
        while end < len(bars) and bars[end].timestamp == bars[offset].timestamp:
            end += 1
        group = list(enumerate(bars[offset:end], start=offset))
        session = group[0][1].session
        if any(bar.session != session for _, bar in group):
            raise ValueError("Bars at the same instant resolve to different KRX sessions")

        for account in accounts.values():
            if account.session != session:
                account.session = session
                account.session_opening_equity = account.equity()
                account.entries_today = 0
                account.stopped_today = 0
            for index, bar in group:
                position = account.positions.get(bar.symbol)
                if position is not None:
                    _manage(account, position, bar, execution, risk, index in last_in_session)

            # Publish all close marks together before sizing fills to avoid a
            # same-timestamp, alphabetical symbol ordering bias.
            for _, bar in group:
                account.marks[bar.symbol] = bar.close

            # Simultaneous candidates use stable symbol ordering when capital is
            # limited. The coarse-bar replay fills at this completed bar's close.
            for index, bar in group:
                signal = account.pending.pop(bar.symbol, None)
                if signal and signal.timestamp < bar.timestamp and bar.symbol not in account.positions:
                    decision = decisions.get(signal.snapshot_id) if account.strategy == "B" else None
                    decision_ok = (account.strategy == "A" or _decision_allows_fill(
                        decision, signal, bar.timestamp, decision_ttl))
                    if account.strategy == "B" and decision is not None and decision.action == "ALLOW_ENTRY" and not decision_ok:
                        llm_counts["ALLOW_EXPIRED_BEFORE_FILL"] += 1
                    cooldown_ok = account.cooldown_until.get(
                        bar.symbol, datetime.min.replace(tzinfo=bar.timestamp.tzinfo)) <= bar.timestamp
                    next_bar_only = bar.timestamp <= signal.timestamp + timedelta(minutes=interval_minutes)
                    if (index not in last_in_session and next_bar_only and _entry_allowed(account, risk)
                            and decision_ok and cooldown_ok):
                        _enter(account, signal, bar, execution, risk)

        for _, bar in group:
            bars_for_symbol = history.setdefault(bar.symbol, [])
            if bars_for_symbol and bars_for_symbol[-1].session != bar.session:
                bars_for_symbol.clear()
            bars_for_symbol.append(bar)
            signal = _entry_signal(bars_for_symbol, strategy)
            if signal:
                generated_signals += 1
                entry_signals.append({
                    "symbol": signal.symbol,
                    "timestamp": signal.timestamp.isoformat(),
                    "snapshot_id": signal.snapshot_id,
                    "reason": signal.reason,
                })
                if bar.symbol not in accounts["A"].positions and _entry_allowed(accounts["A"], risk):
                    accounts["A"].pending[bar.symbol] = signal
                if bar.symbol not in accounts["B"].positions and _entry_allowed(accounts["B"], risk):
                    decision = decisions.get(signal.snapshot_id)
                    if not _decision_matches_signal(decision, signal, decision_ttl):
                        llm_counts["MISSING_OR_EXPIRED"] += 1
                    else:
                        llm_counts[decision.action] += 1
                        if decision.action == "ALLOW_ENTRY":
                            accounts["B"].pending[bar.symbol] = signal

        for account in accounts.values():
            account.equity_points.append(account.equity())
        offset = end

    reports = {key: _account_report(account) for key, account in accounts.items()}
    return {
        "classification": "HISTORICAL STOCK PAPER REPLAY — NOT A LIVE RESULT OR PERFORMANCE CLAIM",
        "mode": "PAPER",
        "market": "KRX",
        "bar_interval_minutes": interval_minutes,
        "timestamp_semantics": "completed-bar availability time; CSV timestamps must include KST offset",
        "fill_model": "next completed five-minute bar close; estimated spread/slippage; no same-bar exit",
        "llm_mode": "offline decision-file replay; no Bedrock invocation",
        "input_complete_bars": len(bars),
        "symbols": sorted({bar.symbol for bar in bars}),
        "first_timestamp": bars[0].timestamp.isoformat(),
        "last_timestamp": bars[-1].timestamp.isoformat(),
        "config": config,
        "rule_signal_count": generated_signals,
        "entry_signals": entry_signals,
        "llm_gate_counts": llm_counts,
        "strategies": reports,
        "trades": [trade for account in accounts.values() for trade in account.trades],
    }


def _entry_signal(bars: list[StockBar], cfg: dict[str, Any]) -> EntrySignal | None:
    minimum = max(int(cfg["ema_slow"]) + 2, int(cfg["breakout_bars"]) + 1, int(cfg["volume_lookback"]) + 1)
    if not bars:
        return None
    current = bars[-1]
    first_recent = len(bars) - 1
    while first_recent > 0:
        previous, following = bars[first_recent - 1], bars[first_recent]
        if (previous.session != current.session
                or following.timestamp - previous.timestamp != timedelta(minutes=5)):
            break
        first_recent -= 1
    recent = bars[first_recent:]
    if len(recent) < minimum:
        return None
    local_time = current.timestamp.astimezone(ZoneInfo("Asia/Seoul")).time().replace(second=0, microsecond=0)
    start = time.fromisoformat(cfg["entry_window_start_kst"])
    end = time.fromisoformat(cfg["entry_window_end_kst"])
    if not start <= local_time <= end:
        return None
    closes = [float(bar.close) for bar in recent]
    ema_fast = _ema(closes, int(cfg["ema_fast"]))
    slow_values = _ema_series(closes, int(cfg["ema_slow"]))
    vwap = _vwap(bars)
    previous_high = max(bar.high for bar in recent[-int(cfg["breakout_bars"]) - 1:-1])
    lookback = int(cfg["volume_lookback"])
    prior_volume = sum((bar.volume for bar in recent[-lookback - 1:-1]), ZERO) / lookback
    if not all((ema_fast > slow_values[-1], slow_values[-1] > slow_values[-2],
                current.close > vwap, current.close > previous_high,
                prior_volume > ZERO and current.volume >= prior_volume * _d(cfg["volume_multiple"]))):
        return None
    timestamp_key = current.timestamp.isoformat(timespec="seconds").replace(":", "").replace("+", "")
    snapshot_id = f"n2-{current.session:%Y%m%d}-{current.symbol}-{timestamp_key}"
    return EntrySignal(current.symbol, current.timestamp, snapshot_id,
                       "EMA5>EMA20; EMA20 rising; close>session VWAP; prior 6-bar breakout; volume expansion")


def _decision_matches_signal(decision: LLMDecision | None, signal: EntrySignal,
                             ttl: timedelta) -> bool:
    return bool(decision is not None and decision.snapshot_id == signal.snapshot_id
                and decision.symbol == signal.symbol and signal.timestamp <= decision.received_at
                and decision.expires_at > signal.timestamp
                and decision.expires_at <= decision.received_at + ttl)


def _decision_allows_fill(decision: LLMDecision | None, signal: EntrySignal,
                          fill_time: datetime, ttl: timedelta) -> bool:
    return bool(_decision_matches_signal(decision, signal, ttl)
                and decision is not None and decision.action == "ALLOW_ENTRY"
                and decision.received_at < fill_time and decision.expires_at > fill_time)


def _vwap(bars: list[StockBar]) -> Decimal:
    volume = sum((bar.volume for bar in bars), ZERO)
    if volume <= ZERO:
        return ZERO
    turnover = sum((bar.turnover_krw if bar.turnover_krw is not None else
                    (bar.high + bar.low + bar.close) / Decimal(3) * bar.volume for bar in bars), ZERO)
    return turnover / volume


def _enter(account: _Account, signal: EntrySignal, bar: StockBar, execution: dict, risk: dict) -> None:
    equity = account.equity()
    if equity <= ZERO or len(account.positions) >= int(risk["max_positions"]):
        return
    entry = _execution(bar.close, "BUY", execution)
    cap = equity * _d(risk["max_position_equity_pct"])
    exposure_room = equity * _d(risk["max_total_exposure_pct"]) - account.exposure()
    stop_distance = entry * _d(risk["stop_loss_pct"])
    rate = (_d(execution["spread_bps_total"]) + 2 * _d(execution["slippage_bps"])
            + 2 * _d(execution["commission_bps_per_side"])
            + _d(execution["sell_tax_bps"][bar.market])) / BPS
    per_share_risk = stop_distance + entry * rate
    risk_budget = equity * _d(risk["risk_per_trade_pct"])
    cash_per_share = entry * (ONE + _d(execution["commission_bps_per_side"]) / BPS)
    qty = min(_floor(cap / entry), _floor(exposure_room / entry),
              _floor(account.cash / cash_per_share),
              _floor(risk_budget / per_share_risk) if per_share_risk > ZERO else 0)
    if qty <= 0:
        return
    price = _price(entry)
    notional = price * qty
    fee = _money(notional * _d(execution["commission_bps_per_side"]) / BPS)
    if notional + fee > account.cash:
        return
    account.cash -= notional + fee
    account.positions[bar.symbol] = _Position(
        symbol=bar.symbol, quantity=qty, entry_time=bar.timestamp, entry_price=price,
        entry_notional=notional, entry_commission=fee,
        entry_price_impact=max(ZERO, price - bar.close) * qty,
        stop_price=_price(price * (ONE - _d(risk["stop_loss_pct"]))),
        take_price=_price(price * (ONE + _d(risk["take_profit_pct"]))), signal=signal)
    account.entries_today += 1


def _manage(account: _Account, position: _Position, bar: StockBar,
            execution: dict, risk: dict, last_bar: bool) -> None:
    if position.symbol not in account.positions:
        return
    held = (bar.timestamp - position.entry_time).total_seconds() / 60
    if bar.low <= position.stop_price:
        _close(account, position, bar, min(bar.open, position.stop_price), "STOP_LOSS", execution)
    elif bar.high >= position.take_price:
        _close(account, position, bar, max(bar.open, position.take_price), "TAKE_PROFIT", execution)
    elif held >= float(risk["max_holding_minutes"]):
        _close(account, position, bar, bar.close, "MAX_HOLDING_TIME", execution)
    elif last_bar:
        _close(account, position, bar, bar.close, "SESSION_END", execution)


def _close(account: _Account, position: _Position, bar: StockBar, reference: Decimal,
           reason: str, execution: dict) -> None:
    exit_price = _price(_execution(reference, "SELL", execution))
    notional = exit_price * position.quantity
    fee = _money(notional * _d(execution["commission_bps_per_side"]) / BPS)
    tax = _money(notional * _d(execution["sell_tax_bps"]["KRX"]) / BPS)
    exit_price_impact = max(ZERO, reference - exit_price) * position.quantity
    pnl = notional - fee - tax - position.entry_notional - position.entry_commission
    account.cash += notional - fee - tax
    account.trades.append({
        "strategy": account.strategy, "symbol": position.symbol,
        "signal_time": position.signal.timestamp.isoformat(),
        "entry_time": position.entry_time.isoformat(), "exit_time": bar.timestamp.isoformat(),
        "snapshot_id": position.signal.snapshot_id, "quantity_shares": position.quantity,
        "entry_price_krw": str(position.entry_price), "exit_price_krw": str(exit_price),
        "commission_krw": str(position.entry_commission + fee), "sell_tax_krw": str(tax),
        "spread_slippage_krw": str(position.entry_price_impact + exit_price_impact),
        "net_pnl_krw": str(pnl), "net_return_pct": str(pnl / position.entry_notional * 100),
        "exit_reason": reason, "signal_reason": position.signal.reason,
    })
    if reason == "STOP_LOSS":
        account.stopped_today += 1
    elif pnl > ZERO:
        account.stopped_today = 0
    account.positions.pop(position.symbol, None)
    cooldown = int(execution.get("cooldown_after_exit_minutes", 0))
    if cooldown:
        account.cooldown_until[position.symbol] = bar.timestamp + timedelta(minutes=cooldown)


def _entry_allowed(account: _Account, risk: dict) -> bool:
    if account.session_opening_equity <= ZERO:
        return False
    if account.equity() <= account.session_opening_equity * (ONE - _d(risk["daily_loss_stop_pct"])):
        return False
    return (account.entries_today < int(risk["max_entries_per_day"])
            and account.stopped_today < int(risk["max_consecutive_stops"]))


def _execution(price: Decimal, side: str, execution: dict) -> Decimal:
    adverse_bps = (_d(execution["spread_bps_total"]) / 2 + _d(execution["slippage_bps"])) / BPS
    return price * (ONE + adverse_bps if side == "BUY" else ONE - adverse_bps)


def _account_report(account: _Account) -> dict[str, Any]:
    equity = account.equity()
    peak, drawdown = account.initial_cash, ZERO
    for value in account.equity_points:
        peak = max(peak, value)
        if peak > ZERO:
            drawdown = max(drawdown, (peak - value) / peak)
    pnls = [_d(trade["net_pnl_krw"]) for trade in account.trades]
    winners, losers = [p for p in pnls if p > ZERO], [p for p in pnls if p < ZERO]
    gross_loss = -sum(losers, ZERO)
    gross_win = sum(winners, ZERO)
    total_costs = sum((_d(trade["commission_krw"]) + _d(trade["sell_tax_krw"])
                       + _d(trade["spread_slippage_krw"])
                       for trade in account.trades), ZERO)
    return {
        "strategy": account.strategy, "initial_cash_krw": str(account.initial_cash),
        "final_equity_krw": str(equity), "net_pnl_krw": str(equity - account.initial_cash),
        "net_return_pct": str((equity - account.initial_cash) / account.initial_cash * 100),
        "max_drawdown_pct": str(drawdown * 100), "closed_trades": len(account.trades),
        "open_positions": len(account.positions),
        "win_rate_pct": str(Decimal(len(winners)) / len(pnls) * 100) if pnls else "0",
        "average_trade_pnl_krw": str(sum(pnls, ZERO) / len(pnls)) if pnls else "0",
        "profit_factor": str(gross_win / gross_loss) if gross_loss else ("infinite" if gross_win else None),
        "total_costs_krw": str(total_costs),
    }


def _ema(values: list[float], period: int) -> float:
    return _ema_series(values, period)[-1]


def _ema_series(values: list[float], period: int) -> list[float]:
    multiplier = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def _floor(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding=ROUND_FLOOR)))


def _price(value: Decimal) -> Decimal:
    # Whole-won pricing only. KRX price-tier tick sizes remain an explicit model limitation.
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
