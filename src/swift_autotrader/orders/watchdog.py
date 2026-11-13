"""Explicit deadline evaluation, not an unsupervised background trader."""
from __future__ import annotations

from datetime import datetime

from swift_autotrader.broker.paper import as_utc
from swift_autotrader.orders.policy import deadline_from_acceptance


def cancellation_due(accepted_at_utc: datetime | None, now: datetime) -> bool:
    return accepted_at_utc is not None and (
        as_utc(now) >= deadline_from_acceptance(accepted_at_utc)
    )
