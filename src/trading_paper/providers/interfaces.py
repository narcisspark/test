"""Read-only provider contracts; order operations are intentionally absent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    market: str
    event_time: datetime
    received_at: datetime
    last_price: Decimal
    cumulative_volume: int | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    quality: tuple[str, ...] = ()


class ReadOnlyMarketData(Protocol):
    """Normalized market-data surface for a later read-only adapter."""

    def get_quotes(self, symbols: Sequence[str]) -> Sequence[Quote]: ...
