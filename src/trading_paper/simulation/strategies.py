from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta

from trading_paper.simulation.models import Bar, Signal


def get_signal(strategy_id: str, current: Bar, history: Sequence[Bar], config: dict) -> Signal | None:
    """Evaluate only completed bars through `current`; no future rows are visible."""
    settings = config["strategies"][strategy_id]
    session_bars = [bar for bar in history if bar.session == current.session]
    if not session_bars or session_bars[-1] is not current:
        return None
    vwap = session_vwap(session_bars)
    if vwap <= 0:
        return None
    if strategy_id == "A":
        return _trend_breakout(current, session_bars, settings, vwap)
    if strategy_id == "B":
        return _vwap_reversion(current, session_bars, settings, vwap)
    if strategy_id == "C":
        return _opening_range(current, session_bars, settings)
    if strategy_id in {"D", "E"}:
        return _alphascalp_rsi_bollinger(
            current, session_bars, settings,
            strategy_id=strategy_id,
            require_rebound=strategy_id == "D",
        )
    raise ValueError(f"Unknown strategy: {strategy_id}")


def session_vwap(bars: Sequence[Bar]) -> float:
    volume = sum(bar.volume for bar in bars)
    if volume <= 0:
        return 0.0
    turnover = sum(
        bar.turnover_krw if bar.turnover_krw is not None else ((bar.high + bar.low + bar.close) / 3.0) * bar.volume
        for bar in bars
    )
    return turnover / volume


def _trend_breakout(current: Bar, bars: Sequence[Bar], cfg: dict, vwap: float) -> Signal | None:
    fast, slow = int(cfg["ema_fast"]), int(cfg["ema_slow"])
    breakout, volume_n = int(cfg["breakout_bars"]), int(cfg["volume_lookback"])
    minimum = max(slow + 1, breakout + 1, volume_n + 1)
    if len(bars) < minimum:
        return None
    closes = [bar.close for bar in bars]
    ema_fast = _ema(closes, fast)
    ema_slow_values = _ema_series(closes, slow)
    previous_high = max(bar.high for bar in bars[-breakout - 1:-1])
    prior_volume = sum(bar.volume for bar in bars[-volume_n - 1:-1]) / volume_n
    checks = (
        ema_fast > ema_slow_values[-1],
        ema_slow_values[-1] > ema_slow_values[-2],
        current.close > vwap,
        current.close > previous_high,
        prior_volume > 0 and current.volume >= prior_volume * float(cfg["volume_multiple"]),
    )
    if all(checks):
        return Signal("A", current.symbol, current.timestamp, "EMA5>EMA20; EMA20 rising; close>session VWAP; 6-bar high breakout; volume expansion")
    return None


def _vwap_reversion(current: Bar, bars: Sequence[Bar], cfg: dict, vwap: float) -> Signal | None:
    if len(bars) < 3:
        return None
    threshold = float(cfg["deviation_below_vwap_pct"])
    confirmed = not cfg.get("require_close_above_previous", True) or current.close > bars[-2].close
    if current.close <= vwap * (1.0 - threshold) and confirmed:
        return Signal("B", current.symbol, current.timestamp, f"close {threshold:.2%} or more below session VWAP with rebound confirmation")
    return None


def _opening_range(current: Bar, bars: Sequence[Bar], cfg: dict) -> Signal | None:
    width = int(cfg["opening_range_bars"])
    if width < 1 or len(bars) < 2:
        return None
    session_start = datetime.combine(current.session, time(0, 0), tzinfo=current.timestamp.tzinfo)
    range_end = session_start + timedelta(minutes=width)
    if current.timestamp < range_end:
        return None
    opening_bars = [bar for bar in bars if session_start <= bar.timestamp < range_end]
    if not opening_bars:
        return None
    opening_high = max(bar.high for bar in opening_bars)
    previous_close = bars[-2].close
    if previous_close <= opening_high < current.close:
        return Signal("C", current.symbol, current.timestamp, f"close crossed above first {width} completed bars' high")
    return None


def _alphascalp_rsi_bollinger(
    current: Bar,
    bars: Sequence[Bar],
    cfg: dict,
    *,
    strategy_id: str,
    require_rebound: bool,
) -> Signal | None:
    """Reproduce AlphaScalp's actual UI entry rule on real completed candles.

    The archived UI uses (RSI < threshold OR near lower Bollinger band) AND
    an uptick versus the prior close. The archived daemon omits that rebound
    check. Neither archived implementation uses its displayed volume filter.
    """
    rsi_period = int(cfg["rsi_period"])
    band_period = int(cfg["bollinger_period"])
    if len(bars) < max(rsi_period + 1, band_period):
        return None
    closes = [bar.close for bar in bars]
    current_rsi = _wilder_rsi(closes, rsi_period)
    band_closes = closes[-band_period:]
    middle = sum(band_closes) / band_period
    variance = sum((value - middle) ** 2 for value in band_closes) / band_period
    lower_band = middle - float(cfg["bollinger_stddev"]) * variance ** 0.5
    oversold = current_rsi < float(cfg["rsi_below"])
    near_lower_band = current.close <= lower_band * (1 + float(cfg["lower_band_buffer_pct"]))
    momentum_up = current.close > bars[-2].close
    if (oversold or near_lower_band) and (momentum_up or not require_rebound):
        confirmation = " with rebound confirmation" if require_rebound else " without rebound confirmation"
        return Signal(
            strategy_id, current.symbol, current.timestamp,
            f"RSI({rsi_period})={current_rsi:.1f} or near lower Bollinger band; entry{confirmation}",
        )
    return None


def _wilder_rsi(closes: Sequence[float], period: int) -> float:
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    if len(changes) < period:
        return 50.0
    average_gain = sum(max(0.0, change) for change in changes[:period]) / period
    average_loss = sum(max(0.0, -change) for change in changes[:period]) / period
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(0.0, change)) / period
        average_loss = (average_loss * (period - 1) + max(0.0, -change)) / period
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def _ema(values: Sequence[float], period: int) -> float:
    return _ema_series(values, period)[-1]


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    multiplier = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result
