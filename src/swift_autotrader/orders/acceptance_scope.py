"""Pure transaction-scope validation for isolated supervised acceptance.

This module is an authorization CHECK, not an authorization issuer. It never
connects to a broker. The operator-approved scope must originate independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import hashlib
import hmac
import json

from swift_autotrader.signals.identity import OptionContract, OptionRight


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


def load_acceptance_scope(path: Path, *, account: str) -> AcceptanceScope:
    """Read an independently approved local receipt; never infer authorization."""
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("Supervised acceptance receipt is too large")
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate authorization field")
            result[key] = value
        return result
    obj = json.loads(raw, object_pairs_hook=unique_fields)
    fields = {
        "version", "account_sha256", "channel_id", "message_id", "revision_id",
        "underlying", "right", "strike", "expiration", "quantity",
        "maximum_limit", "expires_at",
    }
    if not isinstance(obj, dict) or set(obj) != fields or type(obj["version"]) is not int or obj["version"] != 1:
        raise ValueError("Invalid supervised acceptance receipt")
    for field in ("account_sha256", "underlying", "right", "strike", "expiration", "maximum_limit", "expires_at"):
        if type(obj[field]) is not str:
            raise ValueError("Invalid authorization field type")
    expected = hashlib.sha256(account.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(obj["account_sha256"], expected):
        raise ValueError("Supervised account binding mismatch")
    return AcceptanceScope(
        account, obj["channel_id"], obj["message_id"], obj["revision_id"],
        OptionContract(obj["underlying"], OptionRight(obj["right"]),
                       Decimal(obj["strike"]), date.fromisoformat(obj["expiration"])),
        obj["quantity"], Decimal(obj["maximum_limit"]),
        datetime.fromisoformat(obj["expires_at"].replace("Z", "+00:00")),
    )
