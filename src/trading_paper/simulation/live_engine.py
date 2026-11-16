from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from trading_paper.simulation.models import Bar, Signal
from trading_paper.simulation.strategies import get_signal

D = Decimal
BPS = D("10000")
KST = ZoneInfo("Asia/Seoul")


@dataclass(slots=True)
class LivePosition:
    market: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    signal_time: datetime
    entry_fee: Decimal
    stop_price: Decimal
    take_price: Decimal
    signal_reason: str
    last_mark: Decimal
    last_quote_at: datetime
    strategy_id: str = "A"
    highest_price: Decimal = D("0")
    trailing_activation_price: Decimal | None = None
    trailing_stop_fraction: Decimal | None = None
    max_holding_minutes: Decimal | None = None


@dataclass(slots=True)
class LiveAccount:
    strategy_id: str
    starting_cash: Decimal
    cash: Decimal
    positions: dict[str, LivePosition] = field(default_factory=dict)
    pending: dict[str, Signal] = field(default_factory=dict)
    histories: dict[str, list[Bar]] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    session: date | None = None
    session_start_equity: Decimal = D("0")
    entries_today: int = 0
    consecutive_stops: int = 0

    def equity(self) -> Decimal:
        return self.cash + sum((p.quantity * p.last_mark for p in self.positions.values()), D("0"))

    def exposure(self) -> Decimal:
        return sum((p.quantity * p.last_mark for p in self.positions.values()), D("0"))


class LivePaperEngine:
    """Independent strategy portfolios fed by the same public market stream."""

    def __init__(self, config: dict[str, Any], markets: list[str], *, run_id: str):
        if config.get("mode") != "PAPER":
            raise ValueError("Only PAPER mode is supported")
        self.config = config
        self.markets = markets
        cash = D(str(config["initial_cash_krw"]))
        self.accounts = {key: LiveAccount(key, cash, cash) for key in config["strategies"]}
        self.forming: dict[str, Bar] = {}
        self.last_complete_at: dict[str, datetime] = {}
        self.last_quote_received: dict[str, datetime] = {}
        self.last_exchange_time: dict[str, datetime] = {}
        self.last_bid_ask: dict[str, tuple[Decimal | None, Decimal | None, Decimal | None]] = {}
        self.latest_quote_details: dict[str, dict[str, Any]] = {}
        self.event_count = 0
        self.websocket_message_count = 0
        self.websocket_type_counts: dict[str, int] = {}
        self.completed_candle_count = 0
        self.stale_count = 0
        self.disconnect_count = 0
        self.skipped: list[dict[str, Any]] = []
        self.run_id = run_id
        self.connected = False
        self.connection_changes: list[dict[str, Any]] = []

    def seed_history(self, market: str, candle_rows: list[dict[str, Any]]) -> None:
        rows = sorted(candle_rows, key=lambda row: row["candle_date_time_utc"])
        # The newest REST candle can still be forming; keep it out of indicator history.
        for row in rows[:-1]:
            bar = _bar_from_rest(row)
            for account in self.accounts.values():
                history = account.histories.setdefault(market, [])
                if history and history[-1].session != bar.session:
                    history.clear()
                history.append(bar)
            self.last_complete_at[market] = bar.timestamp + timedelta(minutes=1)

    def set_connection(self, connected: bool, at: datetime, detail: str | None = None) -> None:
        self.connected = connected
        if not connected and detail not in {"run stopped; virtual positions are marked, not force-filled", "offline replay complete"}:
            self.disconnect_count += 1
        self.connection_changes.append({"received_at": at.isoformat(), "connected": connected, "detail": detail})

    def on_candle(self, payload: dict[str, Any], received_at: datetime) -> None:
        market = payload.get("code")
        if market not in self.markets:
            return
        self._count_message("candle.1m")
        bar = _bar_from_ws(payload)
        previous = self.forming.get(market)
        if previous is None:
            self.forming[market] = bar
            return
        if bar.timestamp < previous.timestamp:
            self.skipped.append({"at": received_at.isoformat(), "market": market, "reason": "out_of_order_candle"})
            return
        if bar.timestamp == previous.timestamp:
            self.forming[market] = bar
            return
        self._complete_bar(previous)
        self.forming[market] = bar

    def _complete_bar(self, bar: Bar) -> None:
        close_at = bar.timestamp + timedelta(minutes=1)
        previous_close_at = self.last_complete_at.get(bar.symbol)
        if previous_close_at and close_at <= previous_close_at:
            return
        for account in self.accounts.values():
            history = account.histories.setdefault(bar.symbol, [])
            if history and history[-1].session != bar.session:
                history.clear()
            history.append(bar)
            if len(history) > 2000:
                del history[:-2000]
            if account.session != bar.session:
                account.session = bar.session
                account.session_start_equity = account.equity()
                account.entries_today = 0
                account.consecutive_stops = 0
            if bar.symbol in account.positions or bar.symbol in account.pending or not self._entries_allowed(account):
                continue
            signal = get_signal(account.strategy_id, bar, history, self.config)
            if signal is not None:
                # A one-minute candle is actionable only after its interval has ended.
                signal = Signal(signal.strategy, signal.symbol, close_at, signal.reason)
                account.pending[bar.symbol] = signal
                account.events.append({"event": "SIGNAL", "market": bar.symbol, "time": close_at.isoformat(), "reason": signal.reason})
        self.last_complete_at[bar.symbol] = close_at
        self.completed_candle_count += 1

    def on_quote(self, payload: dict[str, Any], received_at: datetime) -> None:
        market = payload.get("code") or payload.get("market")
        if market not in self.markets:
            return
        kind = payload.get("type")
        self._count_message(str(kind))
        bid: Decimal | None = None
        ask: Decimal | None = None
        last: Decimal | None = None
        exchange_time = _exchange_time(payload, kind)
        if kind == "orderbook":
            units = payload.get("orderbook_units") or []
            if units:
                bid = _positive(units[0].get("bid_price")) if _positive(units[0].get("bid_size")) else None
                ask = _positive(units[0].get("ask_price")) if _positive(units[0].get("ask_size")) else None
        elif kind == "ticker":
            last = _positive(payload.get("trade_price"))
        elif kind == "trade":
            last = _positive(payload.get("trade_price"))
        if bid and ask and bid > ask:
            self.stale_count += 1
            self.skipped.append({"at": received_at.isoformat(), "market": market, "reason": "invalid_crossed_orderbook"})
            return
        if not bid and not ask and not last:
            return
        age = max(0.0, (received_at - exchange_time).total_seconds())
        if age > float(self.config["feed"]["max_quote_age_seconds"]):
            self.stale_count += 1
            self.skipped.append({"at": received_at.isoformat(), "market": market, "reason": "stale_quote", "age_seconds": round(age, 3)})
            return
        self.event_count += 1
        self.last_quote_received[market] = received_at
        self.last_exchange_time[market] = exchange_time
        self.last_bid_ask[market] = (bid, ask, last)
        self.latest_quote_details[market] = {
            "type": kind,
            "bid_krw": str(bid) if bid is not None else None,
            "ask_krw": str(ask) if ask is not None else None,
            "last_krw": str(last) if last is not None else None,
            "exchange_time": exchange_time.isoformat(),
            "received_at": received_at.isoformat(),
            "age_seconds_at_receive": round(age, 3),
        }
        for account in self.accounts.values():
            position = account.positions.get(market)
            if position:
                mark = bid or last or position.last_mark
                position.last_mark = mark
                position.last_quote_at = received_at
                self._manage_position(account, position, market, bid, last, exchange_time, received_at)
            signal = account.pending.get(market)
            if signal:
                age_signal = (exchange_time - signal.timestamp).total_seconds()
                if age_signal < 0:
                    continue
                if age_signal > float(self.config["feed"]["pending_signal_max_seconds"]):
                    account.pending.pop(market, None)
                    self._skip(account, market, "signal_expired", received_at)
                elif self.connected and self._entries_allowed(account):
                    entry_quote = ask or last
                    if entry_quote is None:
                        continue
                    self._enter(account, signal, market, entry_quote, bid is not None and ask is not None, exchange_time, received_at)

    def _count_message(self, kind: str) -> None:
        self.websocket_message_count += 1
        self.websocket_type_counts[kind] = self.websocket_type_counts.get(kind, 0) + 1

    def _enter(self, account: LiveAccount, signal: Signal, market: str, quote: Decimal, has_bid_ask: bool, exchange_time: datetime, received_at: datetime) -> None:
        account.pending.pop(market, None)
        risk, execution = self.config["risk"], self.config["execution"]
        strategy_config = self.config["strategies"][account.strategy_id]
        if len(account.positions) >= int(risk["max_positions"]):
            self._skip(account, market, "max_positions", received_at)
            return
        fee_bps = D(str(execution["fee_bps_per_side"]))
        slippage = D(str(execution["slippage_bps"])) / BPS
        fallback_spread = D("0") if has_bid_ask else D(str(execution["fallback_spread_bps_total"])) / 2 / BPS
        entry_price = _tick(quote * (D("1") + slippage + fallback_spread))
        equity = account.equity()
        if equity <= 0:
            self._skip(account, market, "non_positive_equity", received_at)
            return
        position_cap = equity * D(str(risk["max_position_equity_pct"]))
        exposure_room = equity * D(str(risk["max_total_exposure_pct"])) - account.exposure()
        cash_room = account.cash
        stop_fraction = D(str(strategy_config.get("stop_loss_pct", risk["stop_loss_pct"])))
        take_fraction = D(str(strategy_config.get("take_profit_pct", risk["take_profit_pct"])))
        stop_distance = entry_price * stop_fraction
        cost_rate = 2 * fee_bps / BPS + 2 * D(str(execution["slippage_bps"])) / BPS + (D("0") if has_bid_ask else D(str(execution["fallback_spread_bps_total"])) / BPS)
        risk_share = stop_distance + entry_price * cost_rate
        risk_budget = equity * D(str(risk["risk_per_trade_pct"]))
        cash_per_unit = entry_price * (D("1") + fee_bps / BPS)
        quantity = min(
            _floor_units(position_cap / entry_price),
            _floor_units(exposure_room / entry_price),
            _floor_units(cash_room / cash_per_unit),
            _floor_units(risk_budget / risk_share) if risk_share else 0,
        )
        if quantity <= 0 or entry_price * D(quantity) / D("100000000") < D(str(execution["minimum_order_krw"])):
            self._skip(account, market, "below_minimum_or_risk_size_zero", received_at)
            return
        qty = D(quantity) / D("100000000")
        notional = entry_price * qty
        fee = _won(notional * fee_bps / BPS)
        if notional + fee > account.cash:
            self._skip(account, market, "insufficient_virtual_cash", received_at)
            return
        account.cash -= notional + fee
        trailing_activation_fraction = strategy_config.get("trailing_activation_pct")
        trailing_stop_fraction = strategy_config.get("trailing_stop_pct")
        holding_minutes = strategy_config.get("max_holding_minutes", risk["max_holding_minutes"])
        account.positions[market] = LivePosition(
            market=market,
            quantity=qty,
            entry_price=entry_price,
            entry_time=exchange_time,
            signal_time=signal.timestamp,
            entry_fee=fee,
            stop_price=_tick(entry_price * (D("1") - stop_fraction)),
            take_price=_tick(entry_price * (D("1") + take_fraction)),
            signal_reason=signal.reason,
            last_mark=quote,
            last_quote_at=received_at,
            strategy_id=account.strategy_id,
            highest_price=entry_price,
            trailing_activation_price=(
                _tick(entry_price * (D("1") + D(str(trailing_activation_fraction))))
                if trailing_activation_fraction is not None else None
            ),
            trailing_stop_fraction=(
                D(str(trailing_stop_fraction)) if trailing_stop_fraction is not None else None
            ),
            max_holding_minutes=(D(str(holding_minutes)) if holding_minutes is not None else None),
        )
        account.entries_today += 1
        account.events.append({"event": "PAPER_BUY_FILL", "market": market, "exchange_time": exchange_time.isoformat(), "received_at": received_at.isoformat(), "qty": str(qty), "price": str(entry_price), "fee_krw": str(fee), "quote_source": "ask" if has_bid_ask else "last_with_fallback_spread", "signal_time": signal.timestamp.isoformat()})

    def _manage_position(self, account: LiveAccount, position: LivePosition, market: str, bid: Decimal | None, last: Decimal | None, exchange_time: datetime, received_at: datetime) -> None:
        if market not in account.positions or not self.connected:
            return
        max_age = float(self.config["feed"]["max_quote_age_seconds"])
        if (received_at - exchange_time).total_seconds() > max_age:
            return
        mark = bid or last
        if mark is None:
            return
        reason = None
        if mark > position.highest_price:
            position.highest_price = mark
        if mark <= position.stop_price:
            reason = "STOP_LOSS"
        elif (
            position.trailing_activation_price is not None
            and position.trailing_stop_fraction is not None
            and position.highest_price >= position.trailing_activation_price
            and mark <= position.highest_price * (D("1") - position.trailing_stop_fraction)
        ):
            reason = "TRAILING_STOP"
        elif mark >= position.take_price:
            reason = "TAKE_PROFIT"
        elif (
            position.max_holding_minutes is not None
            and (exchange_time - position.entry_time).total_seconds() >= float(position.max_holding_minutes) * 60
        ):
            reason = "MAX_HOLDING_TIME"
        if reason:
            self._exit(account, position, mark, reason, exchange_time, received_at, bid is not None)

    def _exit(self, account: LiveAccount, position: LivePosition, quote: Decimal, reason: str, exchange_time: datetime, received_at: datetime, has_bid: bool) -> None:
        execution = self.config["execution"]
        slippage = D(str(execution["slippage_bps"])) / BPS
        fallback_spread = D("0") if has_bid else D(str(execution["fallback_spread_bps_total"])) / 2 / BPS
        exit_price = _tick(quote * (D("1") - slippage - fallback_spread))
        gross = exit_price * position.quantity
        fee = _won(gross * D(str(execution["fee_bps_per_side"])) / BPS)
        net_pnl = gross - fee - position.entry_price * position.quantity - position.entry_fee
        account.cash += gross - fee
        account.trades.append({
            "strategy": account.strategy_id,
            "market": position.market,
            "signal_time": position.signal_time.isoformat(),
            "entry_time": position.entry_time.isoformat(),
            "exit_time": exchange_time.isoformat(),
            "quantity": str(position.quantity),
            "entry_price_krw": str(position.entry_price),
            "exit_price_krw": str(exit_price),
            "entry_fee_krw": str(position.entry_fee),
            "exit_fee_krw": str(fee),
            "net_pnl_krw": str(_won(net_pnl)),
            "exit_reason": reason,
            "signal_reason": position.signal_reason,
            "quote_source": "bid" if has_bid else "last_with_fallback_spread",
        })
        if reason == "STOP_LOSS":
            account.consecutive_stops += 1
        elif net_pnl > 0:
            account.consecutive_stops = 0
        account.events.append({"event": "PAPER_SELL_FILL", "market": position.market, "exchange_time": exchange_time.isoformat(), "received_at": received_at.isoformat(), "qty": str(position.quantity), "price": str(exit_price), "fee_krw": str(fee), "reason": reason, "quote_source": "bid" if has_bid else "last_with_fallback_spread"})
        account.positions.pop(position.market, None)

    def _entries_allowed(self, account: LiveAccount) -> bool:
        risk = self.config["risk"]
        if account.session_start_equity <= 0:
            return False
        if account.equity() <= account.session_start_equity * (D("1") - D(str(risk["daily_loss_stop_pct"]))):
            return False
        return account.entries_today < int(risk["max_entries_per_day"]) and account.consecutive_stops < int(risk["max_consecutive_stops"])

    def _skip(self, account: LiveAccount, market: str, reason: str, at: datetime) -> None:
        item = {"strategy": account.strategy_id, "market": market, "at": at.isoformat(), "reason": reason}
        self.skipped.append(item)
        account.events.append({"event": "ENTRY_SKIPPED", **item})

    def report(self, stopped_at: datetime) -> dict[str, Any]:
        portfolios = {}
        for key, account in self.accounts.items():
            realized = sum((D(trade["net_pnl_krw"]) for trade in account.trades), D("0"))
            positions = []
            unrealized = D("0")
            for market, position in account.positions.items():
                mark = position.last_mark
                value = mark * position.quantity
                pnl = value - position.entry_price * position.quantity - position.entry_fee
                unrealized += pnl
                positions.append({"market": market, "quantity": str(position.quantity), "average_entry_krw": str(position.entry_price), "last_mark_krw": str(mark), "position_value_krw": str(_won(value)), "unrealized_pnl_krw": str(_won(pnl)), "entry_fee_paid_krw": str(position.entry_fee), "last_quote_received_at": position.last_quote_at.isoformat()})
            fees_paid = sum((D(trade["entry_fee_krw"]) + D(trade["exit_fee_krw"]) for trade in account.trades), D("0"))
            fees_paid += sum((position.entry_fee for position in account.positions.values()), D("0"))
            portfolios[key] = {
                "strategy": key,
                "initial_cash_krw": str(account.starting_cash),
                "cash_krw": str(_won(account.cash)),
                "realized_pnl_krw": str(_won(realized)),
                "unrealized_pnl_krw": str(_won(unrealized)),
                "net_return_pct": str((_won(account.equity()) - account.starting_cash) / account.starting_cash * 100) if account.starting_cash else "0",
                "equity_krw": str(_won(account.cash + sum((D(item["position_value_krw"]) for item in positions), D("0")))),
                "fees_paid_total_krw": str(_won(fees_paid)),
                "fills_count": sum(1 for event in account.events if event["event"].endswith("_FILL")),
                "skipped_entry_count": sum(1 for item in self.skipped if item.get("strategy") == key),
                "open_positions": positions,
                "closed_trades": account.trades,
                "event_ledger": account.events,
                "pending_entries": [{"market": market, "signal_time": signal.timestamp.isoformat(), "reason": signal.reason} for market, signal in account.pending.items()],
            }
        quote_ages = {market: max(0.0, (stopped_at - received).total_seconds()) for market, received in self.last_quote_received.items()}
        return {
            "run_id": self.run_id,
            "mode": "PAPER",
            "classification": "LIVE PUBLIC MARKET DATA — INTERNAL PAPER FILLS ONLY",
            "started_markets": self.markets,
            "stopped_at": stopped_at.isoformat(),
            "connected": self.connected,
            "websocket_message_count": self.websocket_message_count,
            "valid_quote_message_count": self.event_count,
            "websocket_type_counts": self.websocket_type_counts,
            "completed_live_candle_count": self.completed_candle_count,
            "stale_or_invalid_quote_count": self.stale_count,
            "disconnect_count": self.disconnect_count,
            "connection_changes": self.connection_changes,
            "last_quote_age_seconds": quote_ages,
            "latest_quotes": self.latest_quote_details,
            "skipped_entries": self.skipped,
            "portfolios": portfolios,
            "limits": {"public_data_only": True, "private_api": False, "real_orders": False},
        }


def _bar_from_rest(row: dict[str, Any]) -> Bar:
    timestamp = datetime.fromisoformat(row["candle_date_time_kst"]).replace(tzinfo=KST)
    return Bar(timestamp, row["market"], "KRW", float(row["opening_price"]), float(row["high_price"]), float(row["low_price"]), float(row["trade_price"]), float(row["candle_acc_trade_volume"]), float(row["candle_acc_trade_price"]))


def _bar_from_ws(row: dict[str, Any]) -> Bar:
    timestamp = datetime.fromisoformat(row["candle_date_time_kst"]).replace(tzinfo=KST)
    return Bar(timestamp, row["code"], "KRW", float(row["opening_price"]), float(row["high_price"]), float(row["low_price"]), float(row["trade_price"]), float(row["candle_acc_trade_volume"]), float(row["candle_acc_trade_price"]))


def _exchange_time(payload: dict[str, Any], kind: str | None) -> datetime:
    raw = payload.get("timestamp") or payload.get("trade_timestamp")
    if raw is None:
        raise ValueError("Public quote did not include an exchange timestamp")
    return datetime.fromtimestamp(int(raw) / 1000, tz=KST)


def _positive(value: Any) -> Decimal | None:
    if value is None:
        return None
    amount = D(str(value))
    return amount if amount > 0 else None


def _tick(value: Decimal) -> Decimal:
    return value


def _won(value: Decimal) -> Decimal:
    return value.quantize(D("1"), rounding=ROUND_HALF_UP)


def _floor_units(value: Decimal) -> int:
    return max(0, int((value * D("100000000")).to_integral_value(rounding=ROUND_DOWN)))
