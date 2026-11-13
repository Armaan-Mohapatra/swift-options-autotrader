"""Durable local-only failure notifications, never Telegram/email/broker writes.

Only low-sensitivity event codes and timestamps enter notification storage. Paid
Swift text, authorization material, raw exceptions and account IDs are excluded.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from swift_autotrader.broker.paper import iso


@dataclass(frozen=True, slots=True)
class LocalNotification:
    category: str
    severity: str
    code: str
    fingerprint: str
    created_at_utc: str


class NotificationCenter:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError("notification DB unavailable")
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _fingerprint(category: str, source_key: str, code: str) -> str:
        return hashlib.sha256(f"phase5:{category}:{source_key}:{code}".encode()).hexdigest()

    def record(self, category: str, code: str, *, source_key: str,
               severity: str = "ERROR", at: datetime | None = None) -> bool:
        allowed = {"PAPER_ORDER", "TELEGRAM_SERVICE", "APPLICATION", "RESEARCH"}
        if category not in allowed or severity not in {
            "INFO", "WARNING", "ERROR", "CRITICAL"
        }:
            raise ValueError("unsupported notification category or severity")
        # Explicit symbolic error codes only; never accept paid text or exceptions.
        import re
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
            raise ValueError("error code must be metadata only")
        if not source_key or len(source_key) > 512:
            raise ValueError("invalid notification source identity")
        fingerprint = self._fingerprint(category, source_key, code)
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO local_notifications(" 
                "fingerprint,category,severity,source_code,created_at_utc) "
                "VALUES(?,?,?,?,?)",
                (fingerprint, category, severity, code, iso(at or datetime.now(UTC))),
            )
            return bool(db.execute("SELECT changes()").fetchone()[0])

    def scan(self, *, at: datetime | None = None) -> int:
        """Idempotently surface engine alerts and Telegram connection failures."""
        now = at or datetime.now(UTC)
        with self._connect() as db:
            paper = db.execute(
                "SELECT alert_id,code FROM paper_engine_alerts WHERE resolved=0"
            ).fetchall()
            offline = db.execute(
                "SELECT channel_id,state,last_error_code FROM telegram_runtime_state "
                "WHERE state IN ('DEGRADED','DISCONNECTED')"
            ).fetchall()
        count = 0
        for alert_id, code in paper:
            severity = "CRITICAL" if code in {
                "RECONCILIATION_REQUIRED", "UNKNOWN_BROKER_ORDER"
            } else "ERROR"
            count += int(self.record("PAPER_ORDER", code,
                                     source_key=str(alert_id), severity=severity, at=now))
        for channel, state, _ in offline:
            # We never store the channel ID; only its HMAC-like SHA fingerprint.
            channel_ref = hashlib.sha256(str(channel).encode()).hexdigest()[:24]
            count += int(self.record("TELEGRAM_SERVICE", "TELEGRAM_" + state,
                                     source_key=channel_ref + ":" + state,
                                     severity="ERROR", at=now))
        return count

    def pending(self, limit: int = 50) -> list[LocalNotification]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid notification limit")
        with self._connect() as db:
            rows = db.execute(
                "SELECT category,severity,source_code,fingerprint,created_at_utc "
                "FROM local_notifications WHERE acknowledged=0 "
                "ORDER BY notification_id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [LocalNotification(*r) for r in rows]
