"""Replay safety: never promote older Telegram history into fresh signals."""
from __future__ import annotations

from datetime import datetime

from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage, utc


def fresh_live_entry_candidate(
    event: TelegramMessage,
    *,
    session_started_at_utc: datetime,
    max_observed_message_id: int,
    max_age_seconds: int,
    already_seen: bool = False,
) -> bool:
    """Phase 1 stores this flag only; it NEVER places or dispatches orders."""
    now = utc(event.received_at_utc)
    sent = utc(event.sent_at_utc)
    return (
        event.kind is EventKind.NEW
        and event.origin is EventOrigin.LIVE
        and not already_seen
        and event.message_id > max_observed_message_id
        and sent >= utc(session_started_at_utc)
        and now >= sent
        and (now - sent).total_seconds() <= max_age_seconds
    )
