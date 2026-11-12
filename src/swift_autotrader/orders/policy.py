"""Immutable entry deadline rule; order scheduling and cancellation are Phase 3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

ENTRY_TIMEOUT_SECONDS = 3600


def deadline_from_acceptance(accepted_at_utc: datetime) -> datetime:
    if accepted_at_utc.tzinfo is None:
        raise ValueError("accepted_at_utc must be timezone-aware")
    return accepted_at_utc.astimezone(UTC) + timedelta(seconds=ENTRY_TIMEOUT_SECONDS)


def deadline_reached(accepted_at_utc: datetime, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC) >= deadline_from_acceptance(accepted_at_utc)
