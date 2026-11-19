"""Structured metadata-only logs: never include message bodies or secrets."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never interpolate untrusted record arguments or print exception text.
        return json.dumps(
            {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "component": record.name.split(".")[0],
                "event": record.msg if record.msg in {
                    "listener_started", "listener_stopped", "reconnect",
                    "store_unavailable", "queue_overflow", "health_check",
                    "source_verified",
                } else "redacted_event",
            },
            sort_keys=True,
        )


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger("swift_autotrader")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
