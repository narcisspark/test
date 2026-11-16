"""Small internal-only paper order model; never submits provider requests."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderState(StrEnum):
    CREATED = "CREATED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    symbol: str
    side: Side
    quantity: int
    created_at: str
    reference_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.quantity <= 0 or self.reference_price <= 0:
            raise ValueError("Paper order intent requires a symbol, positive quantity and price.")


@dataclass(frozen=True, slots=True)
class PaperOrder:
    intent: PaperOrderIntent
    state: PaperOrderState = PaperOrderState.CREATED
    execution_model: str = "unfilled_until_a_later_valid_observation"


class PaperBroker:
    """Creates local intents only. Filling requires a future market observation."""

    def create_order(self, intent: PaperOrderIntent) -> PaperOrder:
        if not isinstance(intent, PaperOrderIntent):
            raise TypeError("PaperBroker accepts PaperOrderIntent only.")
        return PaperOrder(intent=intent)
