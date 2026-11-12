"""Broker interface contracts with no invented trading APIs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from swift_autotrader.orders.commands import OrderIntent


class BrokerOrderState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class BrokerOrderStatus:
    logical_order_id: str
    state: BrokerOrderState
    filled_quantity: int
    accepted_at_utc: datetime | None


class Broker(Protocol):
    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus: ...

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus: ...

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus: ...
