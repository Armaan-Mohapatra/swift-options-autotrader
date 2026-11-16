"""Pure transaction-scope validation for isolated supervised acceptance.

This module is an authorization CHECK, not an authorization issuer. It never
connects to a broker. The operator-approved scope must originate independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from swift_autotrader.signals.identity import OptionContract


@dataclass(frozen=True, slots=True)
class AcceptanceScope:
    account: str
    channel_id: int
    message_id: int
    revision_id: int
    contract: OptionContract
    quantity: int
    maximum_limit: Decimal
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.account, str) or not self.account.strip():
            raise ValueError("An exact account binding is required")
        if type(self.channel_id) is not int or self.channel_id == 0:
            raise ValueError("An exact source channel is required")
        if any(type(x) is not int or x <= 0 for x in (self.message_id, self.revision_id)):
            raise ValueError("Exact message and revision IDs are required")
        if not isinstance(self.contract, OptionContract):
            raise ValueError("An exact option contract is required")
        if type(self.quantity) is not int or self.quantity != 1:
            raise ValueError("Supervised acceptance permits one contract only")
        if not isinstance(self.maximum_limit, Decimal) or not (
            self.maximum_limit.is_finite() and self.maximum_limit > 0
        ):
            raise ValueError("A finite positive maximum unit premium is required")
        if not isinstance(self.expires_at, datetime) or self.expires_at.utcoffset() is None:
            raise ValueError("An offset-aware authorization expiry is required")

    def permits(
        self, *, account: str, channel_id: int, message_id: int,
        revision_id: int, contract: OptionContract, quantity: int,
        limit: Decimal, now: datetime,
    ) -> bool:
        if not isinstance(limit, Decimal) or not limit.is_finite() or limit <= 0:
            return False
        if now.utcoffset() is None:
            return False
        return (
            account == self.account
            and channel_id == self.channel_id
            and message_id == self.message_id
            and revision_id == self.revision_id
            and contract == self.contract
            and type(quantity) is int
            and quantity == self.quantity
            and limit <= self.maximum_limit
            and now < self.expires_at
        )
