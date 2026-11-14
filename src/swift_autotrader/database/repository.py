"""Transactional, restart-safe storage for *authorized local* Telegram data.

Raw paid message content is stored only in the selected local SQLite database.
No database operation triggers a parser, order, broker request or Telegram send.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from swift_autotrader.telegram.events import (
    EventKind,
    EventOrigin,
    TelegramMessage,
    utc,
)


def _iso(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class IngestResult:
    stored: bool
    revision: int | None
    eligible_for_future_processing: bool
    ignored_reason: str | None


class TelegramStore:
    """Each event is committed atomically before another event is acknowledged."""

    def __init__(self, db_path: Path, channel_id: int) -> None:
        self.db_path = Path(db_path)
        self.channel_id = str(channel_id)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.db_path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=10,
            )
        else:
            if not self.db_path.is_file():
                raise RuntimeError("Run 'migrate' before starting Telegram ingestion")
            connection = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='telegram_message_revisions'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Telegram schema missing; run 'migrate' first")

    def ingest(
        self,
        message: TelegramMessage,
        *,
        session_started_at_utc: datetime,
        max_live_age_seconds: int = 120,
    ) -> IngestResult:
        if str(message.channel_id) != self.channel_id:
            return IngestResult(False, None, False, "WRONG_CHANNEL")
        if max_live_age_seconds < 0:
            raise ValueError("max age cannot be negative")
        now = utc(message.received_at_utc)
        sent = utc(message.sent_at_utc)
        started = utc(session_started_at_utc)
        effective = message.effective_at
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "body": message.body,
                    "kind": message.kind.value,
                    "edit_at": _iso(message.edited_at_utc)
                    if message.edited_at_utc is not None else None,
                    "reply": message.reply_to_message_id,
                    "author": message.author_id,
                    "sent_at": _iso(message.sent_at_utc),
                },
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        with closing(self._connect()) as conn, conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            seen = conn.execute(
                "SELECT latest_revision_id, latest_effective_at_utc FROM telegram_messages "
                "WHERE channel_id=? AND message_id=?",
                (self.channel_id, message.message_id),
            ).fetchone()
            duplicate = conn.execute(
                "SELECT revision FROM telegram_message_revisions "
                "WHERE channel_id=? AND message_id=? AND content_sha256=?",
                (self.channel_id, message.message_id, fingerprint),
            ).fetchone()
            if duplicate is not None:
                return IngestResult(False, int(duplicate[0]), False, "DUPLICATE")
            state = conn.execute(
                "SELECT max_observed_message_id FROM telegram_runtime_state "
                "WHERE channel_id=?",
                (self.channel_id,),
            ).fetchone()
            watermark = int(state[0]) if state else 0
            reason = None
            if message.kind != EventKind.NEW:
                reason = "EDIT_METADATA_ONLY"
            elif seen is not None:
                reason = "EXISTING_MESSAGE"
            elif message.origin != EventOrigin.LIVE:
                reason = "RECOVERED_HISTORY"
            elif message.message_id <= watermark:
                reason = "BEHIND_CHECKPOINT"
            elif sent < started:
                reason = "PREDATES_SESSION"
            elif now < sent:
                reason = "FUTURE_TIMESTAMP"
            elif (now - sent).total_seconds() > max_live_age_seconds:
                reason = "STALE_MESSAGE"
            eligible = reason is None
            rev_row = conn.execute(
                "SELECT MAX(revision) FROM telegram_message_revisions "
                "WHERE channel_id=? AND message_id=?",
                (self.channel_id, message.message_id),
            ).fetchone()
            revision = int(rev_row[0]) + 1 if rev_row[0] is not None else 0
            conn.execute(
                "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
                "received_at_utc,sent_at_utc,body) VALUES(?,?,?,?,?,?)",
                (
                    self.channel_id, str(message.message_id), revision,
                    _iso(now), _iso(sent), message.body,
                ),
            )
            raw_event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO telegram_message_revisions("
                "raw_event_id,channel_id,message_id,revision,event_kind,origin,"
                "telegram_sent_at_utc,telegram_edited_at_utc,received_at_utc,"
                "author_id,reply_to_message_id,content_sha256,"
                "eligible_for_future_processing,ignored_reason)"
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    raw_event_id, self.channel_id, message.message_id, revision,
                    message.kind.value, message.origin.value, _iso(sent),
                    _iso(message.edited_at_utc)
                    if message.edited_at_utc is not None else None,
                    _iso(now), str(message.author_id)
                    if message.author_id is not None else None,
                    message.reply_to_message_id, fingerprint, int(eligible), reason,
                ),
            )
            rev_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if seen is None:
                conn.execute(
                    "INSERT INTO telegram_messages(channel_id,message_id,"
                    "latest_revision_id,latest_effective_at_utc,first_seen_at_utc)"
                    "VALUES(?,?,?,?,?)",
                    (self.channel_id, message.message_id, rev_id, _iso(effective), _iso(now)),
                )
            elif effective >= _parse(seen[1]):
                conn.execute(
                    "UPDATE telegram_messages SET latest_revision_id=?,"
                    "latest_effective_at_utc=? WHERE channel_id=? AND message_id=?",
                    (rev_id, _iso(effective), self.channel_id, message.message_id),
                )
            else:
                # Keep old out-of-order edits in the audit trail, but do not roll back
                # the latest materialized snapshot or make them actionable.
                conn.execute(
                    "UPDATE telegram_message_revisions SET ignored_reason=? "
                    "WHERE revision_id=?", ("OUT_OF_ORDER_REVISION", rev_id),
                )
                reason = "OUT_OF_ORDER_REVISION"
            conn.execute(
                "INSERT INTO telegram_runtime_state("
                "channel_id,max_observed_message_id,last_received_at_utc,updated_at_utc)"
                "VALUES(?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
                "max_observed_message_id=MAX("
                "telegram_runtime_state.max_observed_message_id,excluded.max_observed_message_id),"
                "last_received_at_utc=excluded.last_received_at_utc,"
                "updated_at_utc=excluded.updated_at_utc",
                (self.channel_id, message.message_id, _iso(now), _iso(now)),
            )
        return IngestResult(True, revision, eligible, reason)

    def mark_health(
        self, state: str, *, at: datetime, error_code: str | None = None,
        overflow: bool = False,
    ) -> None:
        if state not in {"CONNECTED", "DISCONNECTED", "DEGRADED", "STOPPED"}:
            raise ValueError("unsupported health state")
        now = _iso(at)
        with closing(self._connect()) as conn, conn:
            self._ensure_schema(conn)
            conn.execute(
                "INSERT INTO telegram_runtime_state("
                "channel_id,max_observed_message_id,last_connected_at_utc,last_error_code,"
                "overflow_count,state,updated_at_utc) VALUES(?,0,?,?,?, ?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "last_connected_at_utc=CASE WHEN excluded.state='CONNECTED' "
                "THEN excluded.last_connected_at_utc ELSE "
                "telegram_runtime_state.last_connected_at_utc END,"
                "last_error_code=excluded.last_error_code,"
                "overflow_count=telegram_runtime_state.overflow_count+excluded.overflow_count,"
                "state=excluded.state,updated_at_utc=excluded.updated_at_utc",
                (
                    self.channel_id, now if state == "CONNECTED" else None,
                    error_code, int(overflow), state, now,
                ),
            )

    def health(self) -> dict[str, object]:
        if not self.db_path.is_file():
            return {"status": "NOT_INITIALIZED", "messages": 0, "revisions": 0}
        with closing(self._connect(read_only=True)) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT state, max_observed_message_id, last_received_at_utc,"
                "last_connected_at_utc,last_error_code,overflow_count "
                "FROM telegram_runtime_state WHERE channel_id=?",
                (self.channel_id,),
            ).fetchone()
            message_count = conn.execute(
                "SELECT COUNT(*) FROM telegram_messages WHERE channel_id=?",
                (self.channel_id,),
            ).fetchone()[0]
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM telegram_message_revisions WHERE channel_id=?",
                (self.channel_id,),
            ).fetchone()[0]
        return {
            "status": row[0] if row else "NOT_STARTED",
            "max_observed_message_id": int(row[1]) if row else 0,
            "last_received_at_utc": row[2] if row else None,
            "last_connected_at_utc": row[3] if row else None,
            "last_error_code": row[4] if row else None,
            "overflow_count": int(row[5]) if row else 0,
            "messages": message_count,
            "revisions": revision_count,
        }
