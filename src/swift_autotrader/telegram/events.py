"""Only field names; no paid content or access keys in source control."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelegramEventKey:
    channel_id: str
    message_id: str
    edit_version: int
    received_at_utc: datetime
