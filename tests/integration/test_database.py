import sqlite3

import pytest

from swift_autotrader.database.migrate import apply_migrations


def test_migrations_are_idempotent_and_enable_wal(tmp_path):
    db = tmp_path / "private" / "test.sqlite3"
    assert apply_migrations(db) == [
        "0001_initial.sql", "0002_telegram_ingest.sql", "0003_signal_linkage.sql",
    ]
    assert apply_migrations(db) == []
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0  # connection-local
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "raw_events", "option_signals", "order_intents", "order_fills", "schema_migrations"
        } <= tables
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_event_dedupe_and_order_idempotency_constraints(tmp_path):
    db = tmp_path / "test.sqlite3"
    apply_migrations(db)
    with sqlite3.connect(db) as conn:
        event = (
            "fake-channel", "synthetic-001", 0,
            "2026-11-13T00:00:00Z", "2026-11-13T00:00:00Z", "synthetic",
        )
        conn.execute(
            "INSERT INTO raw_events(channel_id, telegram_message_id, edit_version, "
            "received_at_utc, sent_at_utc, body) VALUES(?,?,?,?,?,?)",
            event,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO raw_events(channel_id, telegram_message_id, edit_version, "
                "received_at_utc, sent_at_utc, body) VALUES(?,?,?,?,?,?)",
                event,
            )
        conn.rollback()  # preserve no user content
