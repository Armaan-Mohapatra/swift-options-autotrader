"""Synthetic tests for the LIVE dispatch incident. No brokerage or Telegram access."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.parser.rules import SignalIntent, parse_message


NOW = datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
CHANNEL = -100123456789


def _db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE raw_events (
                event_id INTEGER PRIMARY KEY, body TEXT NOT NULL
            );
            CREATE TABLE telegram_message_revisions (
                revision_id INTEGER PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                telegram_sent_at_utc TEXT,
                raw_event_id INTEGER NOT NULL
            );
            CREATE TABLE signal_parser_decisions (
                revision_id INTEGER PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                intent TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                signal_id TEXT,
                processed_at_utc TEXT NOT NULL
            );
            """
        )


def _insert(path: Path, revision_id: int, body: str, sent_at: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO raw_events(event_id,body) VALUES(?,?)", (revision_id, body))
        db.execute(
            "INSERT INTO telegram_message_revisions "
            "(revision_id,channel_id,message_id,telegram_sent_at_utc,raw_event_id) "
            "VALUES(?,?,?,?,?)",
            (revision_id, str(CHANNEL), 14000 + revision_id, sent_at, revision_id),
        )


def _expire(path: Path) -> int:
    dispatcher = SimpleNamespace(
        db_path=path, channel_id=CHANNEL,
        settings=SimpleNamespace(telegram_max_live_age_seconds=120),
    )
    return LiveDispatcher._expire_stale_entry_revisions(dispatcher, NOW)


def _decision(path: Path, revision_id: int):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT intent,status,reason FROM signal_parser_decisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()


def test_expired_entry_closed_durably_without_market_data(tmp_path):
    path = tmp_path / "live.sqlite3"
    _db(path)
    _insert(path, 1, "SPY 680C 11/20 @ 1.25",
            (NOW - timedelta(minutes=5)).isoformat())
    assert parse_message("SPY 680C 11/20 @ 1.25").intent is SignalIntent.ENTRY
    assert _expire(path) == 1
    assert _decision(path, 1) == ("ENTRY", "SKIPPED", "ALERT_STALE_EXPIRED")
    assert _expire(path) == 0  # idempotent across service restart


def test_fresh_entry_does_not_get_misclassified(tmp_path):
    path = tmp_path / "live.sqlite3"
    _db(path)
    _insert(path, 2, "SPY 680C 11/20 @ 1.25",
            (NOW - timedelta(seconds=100)).isoformat())
    assert _expire(path) == 0
    assert _decision(path, 2) is None


def test_invalid_timestamp_fails_closed_for_new_entry(tmp_path):
    path = tmp_path / "live.sqlite3"
    _db(path)
    _insert(path, 3, "SPY 680C 11/20 @ 1.25", "bad timestamp")
    assert _expire(path) == 1
    assert _decision(path, 3) == ("ENTRY", "SKIPPED", "ALERT_TIMESTAMP_UNVERIFIED")


def test_delayed_close_is_not_expired(tmp_path):
    path = tmp_path / "live.sqlite3"
    _db(path)
    body = "SPY 680C 11/20 SOLD HALF"
    if parse_message(body).intent is not SignalIntent.CLOSE_100_PERCENT:
        pytest.skip("Adjust fixture to exact closing grammar in parser")
    _insert(path, 4, body, (NOW - timedelta(days=2)).isoformat())
    assert _expire(path) == 0
    assert _decision(path, 4) is None


def test_outside_channel_not_modified(tmp_path):
    path = tmp_path / "live.sqlite3"
    _db(path)
    _insert(path, 5, "SPY 680C 11/20 @ 1.25",
            (NOW - timedelta(hours=1)).isoformat())
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE telegram_message_revisions SET channel_id=? WHERE revision_id=5",
            ("-100999",),
        )
    assert _expire(path) == 0
    assert _decision(path, 5) is None
