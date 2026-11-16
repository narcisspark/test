"""Historical paper replay of the proposed trend-pullback/order-flow strategy.

The probability gate is deliberately disabled until enough chronological
candidate labels exist. Reports identify this as rule-only research.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_paper.simulation.csvio import load_bars
from trading_paper.simulation.orderflow_features import FEE, SLIP, Features, buy_fill, sell_fill

CAPITAL = Decimal("10000000")
POSITION_CAP = Decimal("0.10")
RISK_BUDGET = Decimal("0.001")
DAILY_STOP = Decimal("0.005")
MAX_DRAWDOWN = Decimal("0.02")
DELAY_SECONDS = 0.5
MAX_BOOK_WAIT_SECONDS = 2.5
MAX_HOLD_SECONDS = 20 * 60
COOLDOWN_SECONDS = 5 * 60
KST = ZoneInfo("Asia/Seoul")


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(output[-1] + alpha * (value - output[-1]))
    return output


def _indicators(bars):
    epochs = [bar.timestamp.timestamp() for bar in bars]
    closes = [bar.close for bar in bars]
    ema20, ema60 = _ema(closes, 20), _ema(closes, 60)
    true_ranges = []
    for i, bar in enumerate(bars):
        previous = bars[i - 1].close if i else bar.close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    atr = [0.0] * len(bars)
    if len(bars) >= 14:
        atr[13] = sum(true_ranges[:14]) / 14
        for i in range(14, len(bars)):
            atr[i] = (atr[i - 1] * 13 + true_ranges[i]) / 14
    return epochs, ema20, ema60, atr


def _flow_confirmation(profile: str, features: list[float], spread_bps: float,
                       ask_depth_krw: float) -> bool:
    """Apply a fixed flow rule; strict thresholds mirror the earlier OFI study."""
    if len(features) < 7:
        return False
    if profile == "v1":
        return features[1] > 0 and features[2] > 0
    if profile == "strict":
        return (
            features[0] >= 0
            and features[1] > 0
            and features[2] >= 0.20
            and features[3] > 0
            and spread_bps <= 5
            and ask_depth_krw >= 2_500_000
        )
    raise ValueError(f"Unknown flow profile: {profile}")


@dataclass
class Setup:
    high: float
    high_closed_at: float
    atr: float
    low: float
    confirmations: int = 0


class PullbackAccount:
    def __init__(self, fee_rate: Decimal, slip_rate: Decimal):
        self.fee_rate, self.slip_rate = fee_rate, slip_rate
        self.cash = CAPITAL
        self.position = None
        self.pending = None
        self.trades = []
        self.signals = 0
        self.rejected = 0
        self.rejections_by_reason = {}
        self.order_diagnostics = []
        self.fees = Decimal(0)
        self.slippage = Decimal(0)
        self.cooldown_until = 0.0
        self.day = None
        self.day_start_equity = CAPITAL
        self.daily_stop_hit = False
        self.run_stop_hit = False
        self.peak_equity = CAPITAL
        self.max_drawdown = Decimal(0)
        self.consecutive_stops = 0

    def reject(self, reason):
        self.rejected += 1
        self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1

    def equity(self, book):
        if self.position is None:
            return self.cash
        liquidation = sell_fill(book, self.position["qty"], self.fee_rate, self.slip_rate) if book else None
        return self.cash + liquidation["total"] if liquidation else None

    def observe(self, book, at: float):
        equity = self.equity(book)
        if equity is None:
            return
        session = datetime.fromtimestamp(at, KST).date()
        if session != self.day:
            self.day = session
            self.day_start_equity = equity
            self.daily_stop_hit = False
        self.peak_equity = max(self.peak_equity, equity)
        self.max_drawdown = max(self.max_drawdown, (self.peak_equity - equity) / self.peak_equity)
        if equity <= self.day_start_equity * (Decimal(1) - DAILY_STOP):
            self.daily_stop_hit = True
        if equity <= CAPITAL * (Decimal(1) - MAX_DRAWDOWN):
            self.run_stop_hit = True

    def on_book(self, book, at: float):
        self.observe(book, at)
        order = self.pending
        if order and at > order["requested_at"]:
            order["book_updates_after_request"] = order.get("book_updates_after_request", 0) + 1
            order["last_book_at"] = at
        if order and at >= order["due"]:
            if at - order["requested_at"] > MAX_BOOK_WAIT_SECONDS:
                self.reject("ORDER_EXPIRED")
                self.order_diagnostics.append({
                    "result": "expired", "signal_at": _stamp(order["signal_at"]),
                    "first_book_after_request_at": _stamp(at),
                    "wait_seconds": round(at - order["requested_at"], 3),
                    "book_updates_after_request": order.get("book_updates_after_request", 0),
                })
                self.pending = None
            elif order["side"] == "BUY":
                self._buy(book, at, order)
            elif self.position:
                self._sell(book, at, order["reason"])
        if self.position and self.pending is None:
            bid = float(book[0]["bid_price"])
            pos = self.position
            reason = None
            if bid <= pos["stop"]:
                reason = "STOP_LOSS"
            elif bid >= pos["target"]:
                reason = "TAKE_PROFIT"
            elif at - pos["entry_at"] >= MAX_HOLD_SECONDS:
                reason = f"MAX_HOLD_{int(MAX_HOLD_SECONDS // 60)}M"
            if reason:
                self.pending = {"side": "SELL", "due": at + DELAY_SECONDS,
                                "requested_at": at, "reason": reason}
        self.observe(book, at)

    def _buy(self, book, at, order):
        equity = self.equity(book)
        if equity is None or self.daily_stop_hit or self.run_stop_hit:
            self.reject("RISK_STOP_ACTIVE")
            self.pending = None
            return
        mid = (float(book[0]["ask_price"]) + float(book[0]["bid_price"])) / 2
        if mid > order["signal_mid"] + 0.25 * order["atr"]:
            self.reject("PRICE_CHASE_LIMIT")
            self.pending = None
            return
        distance = mid - order["stop"]
        if distance <= 0:
            self.reject("INVALID_STOP_DISTANCE")
            self.pending = None
            return
        spread_rate = (float(book[0]["ask_price"]) - float(book[0]["bid_price"])) / mid
        risk_rate = distance / mid + float(2 * self.fee_rate + 2 * self.slip_rate) + spread_rate
        risk_cap = min(equity, CAPITAL) * RISK_BUDGET
        risk_budget = float(risk_cap) / risk_rate if risk_rate > 0 else 0
        budget = min(self.cash, equity * POSITION_CAP, Decimal(str(risk_budget)))
        depth_krw = sum(float(level["ask_price"]) * float(level["ask_size"]) for level in book[:5])
        depth_budget = Decimal(str(depth_krw * float(1 + self.fee_rate) * float(1 + self.slip_rate) * 0.99))
        budget = min(budget, depth_budget)
        fill = buy_fill(book, budget, self.fee_rate, self.slip_rate)
        if fill is None or fill["notional"] < Decimal("5000"):
            self.reject("INSUFFICIENT_VISIBLE_ASK_DEPTH")
            self.pending = None
            return
        entry_price = fill["notional"] / fill["qty"]
        estimated_exit = order["stop"] * (1 - float(self.slip_rate) - spread_rate) * (1 - float(self.fee_rate))
        estimated_loss = fill["total"] - fill["qty"] * Decimal(str(max(0, estimated_exit)))
        if estimated_loss > risk_cap * Decimal("1.05"):
            self.reject("ESTIMATED_STOP_LOSS_OVER_BUDGET")
            self.pending = None
            return
        target = float(entry_price) + 2 * (float(entry_price) - order["stop"])
        self.cash -= fill["total"]
        self.fees += fill["fee"]
        self.slippage += fill["slippage"]
        self.position = {
            "qty": fill["qty"], "entry_total": fill["total"], "entry_price": entry_price,
            "entry_at": at, "stop": order["stop"], "target": target,
            "entry_fee": fill["fee"], "entry_slippage": fill["slippage"],
            "signal_at": order["signal_at"],
        }
        self.pending = None

    def _sell(self, book, at, reason):
        fill = sell_fill(book, self.position["qty"], self.fee_rate, self.slip_rate)
        if fill is None:
            self.reject("INSUFFICIENT_VISIBLE_BID_DEPTH")
            self.pending = None
            return
        pos = self.position
        pnl = fill["total"] - pos["entry_total"]
        self.cash += fill["total"]
        self.fees += fill["fee"]
        self.slippage += fill["slippage"]
        self.trades.append({
            "signal_at": _stamp(pos["signal_at"]), "entry_at": _stamp(pos["entry_at"]),
            "exit_at": _stamp(at), "quantity": str(pos["qty"]),
            "entry_price": str(pos["entry_price"]), "exit_price": str(fill["notional"] / pos["qty"]),
            "net_pnl_krw": str(pnl), "net_return_on_trade_pct": str(pnl / pos["entry_total"] * 100),
            "reason": reason, "entry_fee_krw": str(pos["entry_fee"]), "exit_fee_krw": str(fill["fee"]),
            "entry_slippage_krw": str(pos["entry_slippage"]), "exit_slippage_krw": str(fill["slippage"]),
        })
        if reason == "STOP_LOSS":
            self.consecutive_stops += 1
            if self.consecutive_stops >= 3:
                self.cooldown_until = at + 3600
        elif pnl > 0:
            self.consecutive_stops = 0
        self.position = None
        self.pending = None
        self.cooldown_until = max(self.cooldown_until, at + COOLDOWN_SECONDS)
        self.observe(book, at)

    def report(self, book):
        equity = self.equity(book)
        realized = sum((Decimal(row["net_pnl_krw"]) for row in self.trades), Decimal(0))
        return {
            "initial_krw": str(CAPITAL), "cash_krw": str(self.cash),
            "final_equity_after_estimated_exit_costs_krw": str(equity) if equity is not None else None,
            "net_pnl_krw": str(equity - CAPITAL) if equity is not None else None,
            "net_return_pct": str((equity / CAPITAL - 1) * 100) if equity is not None else None,
            "realized_net_pnl_krw": str(realized), "fees_paid_krw": str(self.fees),
            "additional_slippage_paid_krw": str(self.slippage),
            "closed_trades": len(self.trades),
            "wins": sum(Decimal(row["net_pnl_krw"]) > 0 for row in self.trades),
            "losses": sum(Decimal(row["net_pnl_krw"]) < 0 for row in self.trades),
            "signals": self.signals, "rejected_or_unfilled": self.rejected,
            "rejections_by_reason": self.rejections_by_reason,
            "order_diagnostics": self.order_diagnostics,
            "open_position": self.position is not None, "daily_loss_stop_hit": self.daily_stop_hit,
            "run_loss_stop_hit": self.run_stop_hit, "max_drawdown_pct": str(self.max_drawdown * 100),
            "trades": self.trades,
        }


def replay_pullback(events: list[str | Path], candle_csv: str | Path, *,
                    fee_rate: Decimal = FEE, slip_rate: Decimal = SLIP,
                    flow_profile: str = "v1") -> dict:
    if flow_profile not in {"v1", "strict", "strict2"}:
        raise ValueError("flow_profile must be 'v1', 'strict', or 'strict2'")
    bars = [bar for bar in load_bars(candle_csv) if bar.market == "KRW" and bar.symbol == "KRW-BTC"]
    if len(bars) < 70:
        raise ValueError("At least 70 complete KRW-BTC minute candles are required")
    if not events:
        raise ValueError("At least one public Upbit event archive is required")
    epochs, ema20, ema60, atrs = _indicators(bars)
    account, features, setup = PullbackAccount(fee_rate, slip_rate), Features(), None
    quote_window = []
    connected = True
    last_event_at = last_event_iso = last_eval_second = None
    first_event_iso = None
    first_book = last_book = None
    event_count = book_count = trade_count = valid_snapshots = 0
    counts = {"trend_qualified_snapshots": 0, "pullback_candidates": 0,
              "flow_confirmed": 0, "cost_gate_passed": 0, "probability_gate_deferred": 0}
    flow_quality = {
        "micro_breakout_observations": 0, "positive_1s_flow": 0, "five_second_flow_ge_0_20": 0,
        "nonnegative_book_imbalance": 0, "positive_microprice": 0,
        "spread_at_most_5_bps": 0, "ask_depth_at_least_2500000_krw": 0,
        "all_strict_checks": 0,
    }

    def reset(at):
        nonlocal features, setup, connected
        if account.pending and account.pending["side"] == "BUY":
            account.pending = None
            account.reject("SIGNAL_CANCELLED_DATA_GAP")
        elif account.position and account.pending is None:
            account.pending = {"side": "SELL", "due": at + DELAY_SECONDS,
                               "requested_at": at, "reason": "DATA_GAP"}
        features, setup, connected = Features(), None, False

    paths = sorted(Path(path) for path in events)
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    iso = record["received_at"]
                    at = datetime.fromisoformat(iso).timestamp()
                    if first_event_iso is None:
                        first_event_iso = iso
                    if last_event_at is not None and at - last_event_at > 2:
                        reset(at)
                    last_event_at, last_event_iso = at, iso
                    if record.get("kind") == "connection":
                        if not record.get("connected"):
                            reset(at)
                        else:
                            connected = True
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") not in ("orderbook", "trade"):
                        continue
                    connected = True
                    features.on_event(payload, at)
                    event_count += 1
                    if payload["type"] == "trade":
                        trade_count += 1
                        continue
                    if features.book_at != at:
                        continue
                    book = features.book
                    book_count += 1
                    if first_book is None:
                        first_book = [dict(level) for level in book]
                    last_book = [dict(level) for level in book]
                    account.on_book(book, at)
                    mid = (float(book[0]["bid_price"]) + float(book[0]["ask_price"])) / 2
                    quote_window.append((at, mid))
                    while quote_window and quote_window[0][0] < at - 12:
                        quote_window.pop(0)
                    if int(at) == last_eval_second:
                        continue
                    last_eval_second = int(at)
                    snapshot = features.snapshot(at)
                    if snapshot is None:
                        continue
                    valid_snapshots += 1
                    minute_start = int(at // 60) * 60
                    idx = bisect_right(epochs, minute_start - 60) - 1
                    if idx < 64 or atrs[idx] <= 0:
                        continue
                    if account.position or account.pending or account.daily_stop_hit or account.run_stop_hit:
                        continue
                    if at < account.cooldown_until:
                        continue
                    if ema20[idx] <= ema60[idx] or ema20[idx] <= ema20[idx - 5]:
                        setup = None
                        continue
                    counts["trend_qualified_snapshots"] += 1
                    start = max(0, idx - 9)
                    high_idx = max(range(start, idx + 1), key=lambda j: bars[j].high)
                    high = bars[high_idx].high
                    high_closed_at = epochs[high_idx] + 60
                    atr = atrs[idx]
                    age = at - high_closed_at
                    if setup is not None and (
                        bars[idx].close < ema60[idx] or age > 300
                        or snapshot["mid"] < setup.high - 1.5 * setup.atr
                    ):
                        setup = None
                    if setup is not None:
                        setup.low = min(setup.low, snapshot["mid"])
                    drawdown = high - snapshot["mid"]
                    if setup is None and 0 <= age <= 300 and 0.5 * atr <= drawdown <= 1.5 * atr:
                        setup = Setup(high, high_closed_at, atr, snapshot["mid"])
                        counts["pullback_candidates"] += 1
                    if setup is None:
                        continue
                    prior_highs = [price for time_at, price in quote_window if at - 10 <= time_at < at]
                    if not prior_highs or snapshot["mid"] <= max(prior_highs):
                        setup.confirmations = 0
                        continue
                    flow_quality["micro_breakout_observations"] += 1
                    vector = snapshot["features"]
                    flow_quality["positive_1s_flow"] += int(vector[1] > 0)
                    flow_quality["five_second_flow_ge_0_20"] += int(vector[2] >= 0.20)
                    flow_quality["nonnegative_book_imbalance"] += int(vector[0] >= 0)
                    flow_quality["positive_microprice"] += int(vector[3] > 0)
                    flow_quality["spread_at_most_5_bps"] += int(snapshot["spread_bps"] <= 5)
                    flow_quality["ask_depth_at_least_2500000_krw"] += int(snapshot["ask_depth_krw"] >= 2_500_000)
                    flow_quality["all_strict_checks"] += int(
                        _flow_confirmation("strict", vector, snapshot["spread_bps"], snapshot["ask_depth_krw"])
                    )
                    gate_profile = "strict" if flow_profile == "strict2" else flow_profile
                    flow_ok = _flow_confirmation(
                        gate_profile, snapshot["features"], snapshot["spread_bps"], snapshot["ask_depth_krw"]
                    )
                    setup.confirmations = setup.confirmations + 1 if flow_ok else 0
                    required_confirmations = 2 if flow_profile == "strict2" else 3
                    if setup.confirmations < required_confirmations:
                        continue
                    counts["flow_confirmed"] += 1
                    stop = setup.low - 0.2 * setup.atr
                    distance_rate = (snapshot["mid"] - stop) / snapshot["mid"]
                    if not 0.0025 <= distance_rate <= 0.012:
                        setup = None
                        continue
                    spread_rate = snapshot["spread_bps"] / 10000
                    estimated_cost_rate = float(2 * fee_rate + 2 * slip_rate) + spread_rate
                    if 2 * distance_rate < 3 * estimated_cost_rate:
                        setup = None
                        continue
                    counts["cost_gate_passed"] += 1
                    counts["probability_gate_deferred"] += 1
                    account.signals += 1
                    account.pending = {
                        "side": "BUY", "due": at + DELAY_SECONDS, "requested_at": at,
                        "signal_at": at, "signal_mid": snapshot["mid"], "stop": stop, "atr": setup.atr,
                    }
                    account.order_diagnostics.append({
                        "result": "queued", "signal_at": _stamp(at),
                        "signal_mid": snapshot["mid"], "stop": stop, "atr": setup.atr,
                    })
                    setup = None
                except (ValueError, KeyError, TypeError, json.JSONDecodeError, OverflowError) as exc:
                    raise RuntimeError(f"Invalid replay event or engine state in {path}: {exc}") from exc

    if account.position and last_book is not None:
        account._sell(last_book, last_event_at or 0, "DATA_END_FORCE")
    benchmark = None
    if first_book and last_book:
        buy = buy_fill(first_book, CAPITAL * Decimal("0.10"), fee_rate, slip_rate)
        sell = sell_fill(last_book, buy["qty"], fee_rate, slip_rate) if buy else None
        if buy and sell:
            pnl = sell["total"] - buy["total"]
            benchmark = {"name": "10% initial-capital buy-and-hold; 90% remains cash",
                         "net_pnl_krw": str(pnl), "net_return_pct_of_account": str(pnl / CAPITAL * 100)}
    return {
        "classification": "RULE-ONLY HISTORICAL ORDERFLOW PAPER REPLAY — PROBABILITY GATE NOT TRAINED; NOT PROMOTABLE",
        "strategy": "KST trend + ATR pullback + persistent taker flow + cost gate",
        "flow_profile": flow_profile,
        "flow_confirmation_count": 2 if flow_profile == "strict2" else 3,
        "mode": "PAPER_ONLY",
        "model_gate": {"enabled": False, "reason": "Insufficient chronological orderflow candidate labels; qualifying signals are diagnostic only."},
        "policy": {"initial_capital_krw": str(CAPITAL), "max_position_pct": 10, "risk_per_trade_pct": 0.1,
                   "daily_loss_stop_pct": 0.5, "max_drawdown_stop_pct": 2.0,
                   "ema_periods_minutes": [20, 60], "pullback_atr_range": [0.5, 1.5],
                   "stop_atr_below_pullback_low": 0.2, "take_profit_risk_multiple": 2,
                   "target_to_cost_minimum": 3,
                   "flow_confirmation_seconds": 2 if flow_profile == "strict2" else 3,
                   "max_holding_seconds": MAX_HOLD_SECONDS, "cooldown_seconds": COOLDOWN_SECONDS,
                   "fee_pct_per_side": float(fee_rate * 100),
                   "additional_slippage_pct_per_side": float(slip_rate * 100),
                   "entry_delay_seconds": DELAY_SECONDS, "exit_delay_seconds": DELAY_SECONDS,
                   "maximum_wait_for_next_saved_book_seconds": MAX_BOOK_WAIT_SECONDS},
        "data": {"event_files": [str(path) for path in paths], "candle_file": str(candle_csv),
                 "event_start": first_event_iso, "event_end": last_event_iso,
                 "websocket_events": event_count, "orderbooks": book_count, "trades": trade_count,
                 "valid_snapshots": valid_snapshots},
        "signal_counts": counts, "flow_quality": flow_quality,
        "benchmark": benchmark, "account": account.report(last_book),
    }


def write_pullback_report(report: dict, destination: str | Path) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
