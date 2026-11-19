"""Owner receipt required before unrestricted (multi-entry) LIVE execution.

This module CHECKS an approval; it never issues one. Environment flags,
scheduler restarts or a stale file cannot enable unattended trading by
themselves: the receipt must bind the exact pinned account, expire within
a short window, and cap every configured risk ceiling.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_APPROVAL_WINDOW = timedelta(days=7)
RECEIPT_NAME = "unattended-live-approval.json"
_FIELDS = frozenset({
    "version", "account_sha256", "issued_at", "expires_at",
    "max_contracts_per_signal", "max_premium_per_signal_usd",
    "max_daily_loss_usd",
})


class UnattendedApprovalError(PermissionError):
    """Unrestricted LIVE execution is not approved for this configuration."""


@dataclass(frozen=True, slots=True)
class UnattendedApproval:
    account_sha256: str
    issued_at: datetime
    expires_at: datetime
    max_contracts_per_signal: int
    max_premium_per_signal_usd: Decimal
    max_daily_loss_usd: Decimal

    def active(self, now: datetime) -> bool:
        return now.utcoffset() is not None and self.issued_at <= now < self.expires_at


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise UnattendedApprovalError("Duplicate approval field")
        out[key] = value
    return out


def _when(raw: object) -> datetime:
    if type(raw) is not str:
        raise UnattendedApprovalError("Approval timestamps must be strings")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnattendedApprovalError("Invalid approval timestamp") from exc
    if value.utcoffset() is None:
        raise UnattendedApprovalError("Approval timestamps must carry a UTC offset")
    return value


def _money(raw: object) -> Decimal:
    if type(raw) is not str:
        raise UnattendedApprovalError("Approval dollar caps must be strings")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise UnattendedApprovalError("Invalid approval dollar cap") from exc
    if not value.is_finite() or value <= 0:
        raise UnattendedApprovalError("Approval dollar caps must be positive")
    return value


def load_unattended_approval(
    path: Path, *, account: str, settings, now: datetime,
) -> UnattendedApproval:
    """Validate the receipt against the pinned account and configured ceilings."""
    if now.utcoffset() is None:
        raise UnattendedApprovalError("An offset-aware clock is required")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(4097)
    except OSError as exc:
        raise UnattendedApprovalError("Unattended live approval receipt not found") from exc
    if len(raw) > 4096:
        raise UnattendedApprovalError("Approval receipt is too large")
    try:
        obj = json.loads(raw, object_pairs_hook=_unique)
    except ValueError as exc:
        raise UnattendedApprovalError("Approval receipt is not valid JSON") from exc
    if not isinstance(obj, dict) or set(obj) != _FIELDS:
        raise UnattendedApprovalError("Approval receipt fields are invalid")
    # v2: no aggregate-exposure ceiling exists any more (buying power alone bounds it).
    if type(obj["version"]) is not int or obj["version"] != 2:
        raise UnattendedApprovalError("Unsupported approval receipt version")
    if type(obj["account_sha256"]) is not str or not hmac.compare_digest(
        obj["account_sha256"], hashlib.sha256(account.encode("utf-8")).hexdigest()
    ):
        raise UnattendedApprovalError("Approval is bound to a different account")
    contracts = obj["max_contracts_per_signal"]
    if type(contracts) is not int or not 1 <= contracts <= 100:
        raise UnattendedApprovalError("Invalid approved contract ceiling")
    approval = UnattendedApproval(
        obj["account_sha256"], _when(obj["issued_at"]), _when(obj["expires_at"]),
        contracts, _money(obj["max_premium_per_signal_usd"]),
        _money(obj["max_daily_loss_usd"]),
    )
    if approval.expires_at - approval.issued_at > MAX_APPROVAL_WINDOW:
        raise UnattendedApprovalError("Approval window exceeds 7 days; renew it")
    if not approval.active(now):
        raise UnattendedApprovalError("Unattended live approval is expired or not yet valid")
    exceeded = [
        name for name, configured, approved in (
            ("MAX_CONTRACTS_PER_SIGNAL", settings.max_contracts_per_signal,
             approval.max_contracts_per_signal),
            ("MAX_PREMIUM_PER_SIGNAL_USD", settings.max_premium_per_signal_usd,
             approval.max_premium_per_signal_usd),
            ("MAX_DAILY_LOSS_USD", settings.max_daily_loss_usd,
             approval.max_daily_loss_usd),
        ) if configured > approved
    ]
    if exceeded:
        raise UnattendedApprovalError(
            "Configured ceilings exceed the owner approval: " + ", ".join(exceeded)
        )
    return approval
