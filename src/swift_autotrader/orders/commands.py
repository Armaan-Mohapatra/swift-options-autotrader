"""Orders must use exact Swift original limit (Decimal); no repricing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from swift_autotrader.signals.identity import OptionContract


class OrderSide(str, Enum):
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    logical_order_id: str
    idempotency_key: str
    contract: OptionContract
    side: OrderSide
    quantity: int
    limit_price: Decimal

    def __post_init__(self) -> None:
        if not self.logical_order_id or not self.idempotency_key:
            raise ValueError("An order requires durable ID and idempotency key")
        if self.quantity < 1:
            raise ValueError("Quantity must be positive")
        if not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("Limit price must be positive and finite")
