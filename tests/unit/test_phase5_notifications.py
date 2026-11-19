"""Persistent metadata-only local alerts: disconnects and unfilled exits."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.notifications.alerts import NotificationCenter

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def test_notifications_are_persisted_and_deduplicated(tmp_path):
    path = tmp_path / "notifications.sqlite3"
    apply_migrations(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO paper_engine_alerts(code,recorded_at_utc) VALUES(" 
                   "'UNFILLED_EXIT','2026-11-12T15:00:00Z')")
        db.execute("INSERT INTO telegram_runtime_state(channel_id,state,updated_at_utc)"
                   "VALUES('-1001234567000','DEGRADED','2026-11-12T15:00:00Z')")
    center = NotificationCenter(path)
    assert center.scan(at=NOW) == 2
    assert center.scan(at=NOW) == 0
    assert len(center.pending()) == 2
    assert {x.code for x in center.pending()} == {"UNFILLED_EXIT", "TELEGRAM_DEGRADED"}
    assert "-1001234567000" not in str(center.pending())
    assert NotificationCenter(path).scan(at=NOW) == 0  # durable across restart


def test_application_failure_is_metadata_only(tmp_path):
    path = tmp_path / "notifications.sqlite3"
    apply_migrations(path)
    center = NotificationCenter(path)
    assert center.record("APPLICATION", "SERVICE_FAILED", source_key="job-v1", at=NOW)
    assert not center.record("APPLICATION", "SERVICE_FAILED", source_key="job-v1", at=NOW)
    assert center.pending()[0].severity == "ERROR"
    with pytest.raises(ValueError):
        center.record("APPLICATION", "actual session secret 123", source_key="unsafe")
    with pytest.raises(ValueError):
        center.record("APPLICATION", "SERVICE_FAILED", source_key="")
