"""Storage interface reserved for Phase 1 onward; no message data read in Phase 0."""

from __future__ import annotations

from typing import Protocol


class CheckpointStore(Protocol):
    def has_processed(self, channel_id: str, message_id: str, edit_version: int) -> bool: ...
