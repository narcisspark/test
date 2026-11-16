from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp: datetime
    symbol: str
    market: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover_krw: float | None = None

    @property
    def session(self) -> date:
        return self.timestamp.date()


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: str
    symbol: str
    timestamp: datetime
    reason: str
