"""Explicit paper brokerage contract; no hidden exchange transport."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract


class PrePlacementRefusal(RuntimeError):
    """A broker adapter proved the placement request was never sent."""


class BrokerOrderState(StrEnum):
    UNKNOWN = "UNKNOWN"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class BrokerFill:
    fill_id: str
    quantity: int
    price: Decimal
    filled_at_utc: datetime


@dataclass(frozen=True, slots=True)
class BrokerOrderStatus:
    logical_order_id: str
    state: BrokerOrderState
    filled_quantity: int
    accepted_at_utc: datetime | None
    fills: tuple[BrokerFill, ...] = ()
    rejected_reason: str | None = None
    contract: OptionContract | None = None
    side: OrderSide | None = None
    requested_quantity: int | None = None
    limit_price: Decimal | None = None
    idempotency_key: str | None = None
    broker_order_id: str | None = None


@runtime_checkable
class Broker(Protocol):
    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus: ...

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus: ...

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus: ...
