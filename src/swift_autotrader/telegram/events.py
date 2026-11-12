"""Stable, fully normalized read-only Telegram message envelope."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Telegram timestamps must be timezone-aware")
    return value.astimezone(UTC)


class EventKind(StrEnum):
    NEW = "new"
    EDIT = "edit"


class EventOrigin(StrEnum):
    LIVE = "live"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class TelegramEventKey:
    channel_id: str
    message_id: str
    edit_version: int
    received_at_utc: datetime


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    channel_id: int
    message_id: int
    sent_at_utc: datetime
    received_at_utc: datetime
    body: str
    kind: EventKind = EventKind.NEW
    origin: EventOrigin = EventOrigin.LIVE
    edited_at_utc: datetime | None = None
    author_id: int | None = None
    reply_to_message_id: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.channel_id, bool) or not isinstance(self.channel_id, int):
            raise ValueError("channel ID must be an integer")
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise ValueError("message ID must be an integer")
        if self.message_id <= 0 or self.channel_id == 0:
            raise ValueError("invalid Telegram message identity")
        if self.reply_to_message_id is not None and self.reply_to_message_id <= 0:
            raise ValueError("invalid reply-to message ID")
        if not isinstance(self.body, str):
            raise ValueError("message body must be text")
        utc(self.sent_at_utc)
        utc(self.received_at_utc)
        if self.edited_at_utc is not None:
            utc(self.edited_at_utc)
        if self.kind == EventKind.EDIT and self.edited_at_utc is None:
            raise ValueError("edits require Telegram edit time")

    @property
    def effective_at(self) -> datetime:
        return utc(self.edited_at_utc or self.sent_at_utc)

    @property
    def source_key(self) -> tuple[int, int]:
        return self.channel_id, self.message_id
