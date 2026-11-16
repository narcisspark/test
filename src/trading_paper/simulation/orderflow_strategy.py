"""Predefined, paper-only channel-breakout confirmation using public order flow."""
from __future__ import annotations

from collections import deque


class ChannelBreakoutConfirmation:
    """Confirm a five-minute range breakout with persistent aggressive buying.

    Quotes are sampled on every valid order-book update. The breakout needs a
    minimum four-minute observed range so a partial warmup cannot masquerade as
    a complete five-minute channel. A candidate remains active for ten seconds
    and fires once after two consecutive one-second flow evaluations.
    """

    def __init__(self, *, window_seconds: float = 300, min_history_seconds: float = 240,
                 candidate_seconds: float = 10, min_quotes: int = 30,
                 confirmations: int = 2) -> None:
        if not 0 < min_history_seconds <= window_seconds:
            raise ValueError("min_history_seconds must be within the channel window")
        if candidate_seconds <= 0 or min_quotes < 2 or confirmations < 1:
            raise ValueError("invalid breakout confirmation settings")
        self.window_seconds = window_seconds
        self.min_history_seconds = min_history_seconds
        self.candidate_seconds = candidate_seconds
        self.min_quotes = min_quotes
        self.confirmations = confirmations
        self.quotes: deque[tuple[float, float]] = deque()
        self.breakout_level: float | None = None
        self.breakout_until = 0.0
        self.confirmation_count = 0
        self.fired = False

    def observe_quote(self, at: float, mid: float) -> float | None:
        """Update the rolling channel and return the active breakout level."""
        if mid <= 0:
            return self.breakout_level
        while self.quotes and self.quotes[0][0] < at - self.window_seconds:
            self.quotes.popleft()

        if self.breakout_level is not None:
            if at > self.breakout_until or mid < self.breakout_level:
                self._clear_candidate()

        enough_history = (
            len(self.quotes) >= self.min_quotes
            and self.quotes
            and at - self.quotes[0][0] >= self.min_history_seconds
        )
        prior_high = max((price for _, price in self.quotes), default=None) if enough_history else None
        if self.breakout_level is None and prior_high is not None and mid > prior_high:
            self.breakout_level = prior_high
            self.breakout_until = at + self.candidate_seconds
            self.confirmation_count = 0
            self.fired = False

        self.quotes.append((at, mid))
        return self.breakout_level

    def confirm(self, at: float, mid: float, features: list[float], spread_bps: float,
                ask_depth_krw: float) -> float | None:
        """Return the breakout level once flow, book, and spread gates agree."""
        level = self.breakout_level
        if level is None or self.fired:
            return None
        if at > self.breakout_until or mid < level or len(features) < 7:
            self._clear_candidate()
            return None

        book_imbalance, trade_1s, trade_5s, microprice_bps = features[:4]
        valid = (
            book_imbalance >= 0
            and trade_1s > 0
            and trade_5s >= 0.20
            and microprice_bps > 0
            and spread_bps <= 5
            and ask_depth_krw >= 2_500_000
        )
        self.confirmation_count = self.confirmation_count + 1 if valid else 0
        if self.confirmation_count >= self.confirmations:
            self.fired = True
            return level
        return None

    def reset(self) -> None:
        self.quotes.clear()
        self._clear_candidate()

    def _clear_candidate(self) -> None:
        self.breakout_level = None
        self.breakout_until = 0.0
        self.confirmation_count = 0
        self.fired = False
