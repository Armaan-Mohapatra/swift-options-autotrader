"""Synthetic-only Phase 1 durability, replay and lineage tests."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage

UTC = UTC
T = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)
SOURCE = -1001234567890


def event(
    message_id=100,
    *,
    channel_id=SOURCE,
    body="synthetic alert not from any paid channel",
    sent_at=T,
    received_at=T + timedelta(seconds=1),
    kind=EventKind.NEW,
    origin=EventOrigin.LIVE,
    edited_at=None,
    author=999,
    reply_to=None,
):
    return TelegramMessage(
        channel_id, message_id, sent_at, received_at, body,
        kind=kind, origin=origin, edited_at_utc=edited_at,
        author_id=author, reply_to_message_id=reply_to,
    )


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "messages.sqlite3"
    assert apply_migrations(db) == [
        "0001_initial.sql", "0002_telegram_ingest.sql", "0003_signal_linkage.sql",
    ]
    return db


@pytest.fixture
def store(database):
    return TelegramStore(database, SOURCE)


def test_first_message_is_stored_with_provenance(store, database):
    result = store.ingest(event(reply_to=77), session_started_at_utc=T)
    assert (result.stored, result.revision, result.eligible_for_future_processing) == (
        True, 0, True,
    )
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT m.message_id, r.author_id, r.reply_to_message_id, r.event_kind, "
            "r.eligible_for_future_processing, e.body FROM telegram_messages m "
            "JOIN telegram_message_revisions r ON m.latest_revision_id=r.revision_id "
            "JOIN raw_events e ON r.raw_event_id=e.event_id"
        ).fetchone()
    assert row == (100, "999", 77, "new", 1, "synthetic alert not from any paid channel")


def test_redelivery_is_idempotent(store):
    first = store.ingest(event(), session_started_at_utc=T)
    duplicate = store.ingest(event(), session_started_at_utc=T)
    assert first.stored
    assert duplicate.ignored_reason == "DUPLICATE"
    assert duplicate.revision == 0
    assert store.health()["revisions"] == 1


def test_edits_are_versioned_without_new_entry_eligibility(store, database):
    store.ingest(event(), session_started_at_utc=T)
    edit = event(
        body="synthetic edited", kind=EventKind.EDIT,
        edited_at=T + timedelta(seconds=3), received_at=T + timedelta(seconds=4),
    )
    changed = store.ingest(edit, session_started_at_utc=T)
    assert changed.revision == 1
    assert changed.ignored_reason == "EDIT_METADATA_ONLY"
    assert not changed.eligible_for_future_processing
    assert store.ingest(edit, session_started_at_utc=T).ignored_reason == "DUPLICATE"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 2
        assert db.execute(
            "SELECT body FROM raw_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()[0] == "synthetic edited"


def test_late_out_of_order_edit_keeps_latest_snapshot(store, database):
    store.ingest(event(), session_started_at_utc=T)
    store.ingest(
        event(body="newest", kind=EventKind.EDIT, edited_at=T + timedelta(minutes=2)),
        session_started_at_utc=T,
    )
    old = store.ingest(
        event(body="older", kind=EventKind.EDIT, edited_at=T + timedelta(minutes=1)),
        session_started_at_utc=T,
    )
    assert old.stored and old.ignored_reason == "OUT_OF_ORDER_REVISION"
    with sqlite3.connect(database) as db:
        latest = db.execute(
            "SELECT e.body FROM telegram_messages m "
            "JOIN telegram_message_revisions v ON m.latest_revision_id=v.revision_id "
            "JOIN raw_events e ON v.raw_event_id=e.event_id"
        ).fetchone()[0]
    assert latest == "newest"


def test_wrong_channel_is_ignored(store):
    result = store.ingest(event(channel_id=-1009999999999), session_started_at_utc=T)
    assert result.ignored_reason == "WRONG_CHANNEL"
    assert store.health()["status"] == "NOT_STARTED"


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"origin": EventOrigin.RECOVERED}, "RECOVERED_HISTORY"),
        ({"sent_at": T - timedelta(minutes=5)}, "PREDATES_SESSION"),
        ({"sent_at": T + timedelta(hours=1)}, "FUTURE_TIMESTAMP"),
        ({"received_at": T + timedelta(minutes=5)}, "STALE_MESSAGE"),
    ],
)
def test_historical_and_clock_skew_never_actionable(store, changed, reason):
    outcome = store.ingest(event(**changed), session_started_at_utc=T)
    assert outcome.stored and not outcome.eligible_for_future_processing
    assert outcome.ignored_reason == reason


def test_checkpoint_persists_after_new_instance(database):
    a = TelegramStore(database, SOURCE)
    a.ingest(event(message_id=200), session_started_at_utc=T)
    b = TelegramStore(database, SOURCE)
    earlier = b.ingest(event(message_id=199), session_started_at_utc=T)
    assert earlier.ignored_reason == "BEHIND_CHECKPOINT"
    assert b.health()["max_observed_message_id"] == 200
    assert b.health()["messages"] == 2


def test_recovered_message_advances_checkpoint_without_signal(store):
    a = store.ingest(
        event(message_id=350, origin=EventOrigin.RECOVERED),
        session_started_at_utc=T,
    )
    assert not a.eligible_for_future_processing
    b = store.ingest(event(message_id=349), session_started_at_utc=T)
    assert b.ignored_reason == "BEHIND_CHECKPOINT"


def test_same_message_id_in_different_chat_is_never_saved(store):
    store.ingest(event(), session_started_at_utc=T)
    store.ingest(event(channel_id=-1007777777777), session_started_at_utc=T)
    assert store.health()["messages"] == 1


def test_restart_migrations_and_duplicate_behavior(database):
    apply_migrations(database)
    first = TelegramStore(database, SOURCE)
    first.ingest(event(), session_started_at_utc=T)
    restarted = TelegramStore(database, SOURCE)
    assert restarted.ingest(event(), session_started_at_utc=T).ignored_reason == "DUPLICATE"
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_no_schema_cannot_write(tmp_path):
    store = TelegramStore(tmp_path / "missing.sqlite3", SOURCE)
    with pytest.raises(RuntimeError, match="migrate"):
        store.ingest(event(), session_started_at_utc=T)


def test_edited_message_requires_edit_date():
    with pytest.raises(ValueError, match="edit time"):
        event(kind=EventKind.EDIT)


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        event(sent_at=datetime(2026, 11, 12, 20, 0))


def test_health_does_not_expose_paid_body_or_channel(store):
    store.ingest(event(body="synthetic private placeholder"), session_started_at_utc=T)
    summary = str(store.health())
    assert "private placeholder" not in summary
    assert str(SOURCE) not in summary
