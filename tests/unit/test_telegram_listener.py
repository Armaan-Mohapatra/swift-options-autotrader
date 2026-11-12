"""Async fake transports: no actual Telegram login or order interaction."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.logging_config import SafeJsonFormatter
from swift_autotrader.telegram.client import normalize_telethon_event
from swift_autotrader.telegram.events import EventKind, TelegramMessage
from swift_autotrader.telegram.listener import QueueOverflow, TelegramListener
from swift_autotrader.telegram.reconnect import retry_delays, with_reconnect

T = datetime(2026, 11, 12, 20, tzinfo=timezone.utc)
CHANNEL = -1001234567890


def message(mid=100, channel=CHANNEL):
    return TelegramMessage(
        channel_id=channel, message_id=mid,
        body="synthetic only", sent_at_utc=T,
        received_at_utc=T + timedelta(seconds=1),
    )


def make_listener(tmp_path, *, capacity=512):
    db = tmp_path / "message.sqlite3"
    apply_migrations(db)
    store = TelegramStore(db, CHANNEL)
    return TelegramListener(store, channel_id=CHANNEL, capacity=capacity, started_at_utc=T)


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def listen(self, on_event):
        for item in self.messages:
            await on_event(item)


def test_mock_ingestion_filters_and_persists(tmp_path):
    listener = make_listener(tmp_path)
    asyncio.run(listener.run(FakeSource([message(100), message(100), message(101), message(1, -5)])))
    assert listener.stored == 2
    assert listener.duplicates == 1
    assert listener.filtered == 1
    assert listener.store.health()["messages"] == 2
    assert listener.store.health()["status"] == "STOPPED"


def test_bounded_queue_does_not_silently_drop(tmp_path):
    listener = make_listener(tmp_path, capacity=1)

    async def scenario():
        await listener.accept(message(100))
        with pytest.raises(QueueOverflow):
            await listener.accept(message(101))

    asyncio.run(scenario())
    assert listener.store.health()["status"] == "DEGRADED"
    assert listener.store.health()["overflow_count"] == 1


def test_resume_after_interruption(tmp_path):
    listener = make_listener(tmp_path)
    asyncio.run(listener.run(FakeSource([message(100)])))
    restarted = TelegramListener(
        listener.store, channel_id=CHANNEL, started_at_utc=T + timedelta(minutes=1)
    )
    asyncio.run(restarted.run(FakeSource([message(100), message(101)])))
    assert restarted.store.health()["messages"] == 2
    # A message first seen on later session but predating reconnection is metadata-only.
    assert restarted.store.health()["revisions"] == 2


def test_reconnect_policy_has_bounded_delays():
    assert retry_delays(4, initial_seconds=1, cap=30) == [1, 2, 4]
    with pytest.raises(ValueError):
        retry_delays(0)


def test_retry_of_only_transport_failure():
    attempts = 0
    delays = []

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("synthetic network down")
        return "connected"

    async def no_sleep(s):
        delays.append(s)

    assert asyncio.run(with_reconnect(operation, attempts=3, sleep=no_sleep)) == "connected"
    assert attempts == 3
    assert delays == [1, 2]


def test_no_retry_on_unauthorized_exception():
    attempts = 0

    async def bad():
        nonlocal attempts
        attempts += 1
        raise ValueError("auth required")

    with pytest.raises(ValueError):
        asyncio.run(with_reconnect(bad, attempts=5))
    assert attempts == 1


def test_normalize_telethon_event_uses_only_metadata():
    msg = SimpleNamespace(
        id=100, date=T, raw_text="synthetic", edit_date=T + timedelta(seconds=2),
        sender_id=12, reply_to=SimpleNamespace(reply_to_msg_id=99),
    )
    event = SimpleNamespace(chat_id=CHANNEL, message=msg)
    normalized = normalize_telethon_event(event, EventKind.EDIT)
    assert normalized.kind == EventKind.EDIT
    assert normalized.reply_to_message_id == 99
    assert normalized.author_id == 12
    assert normalized.edited_at_utc == msg.edit_date


def test_safe_logging_never_prints_message_or_credentials():
    formatter = SafeJsonFormatter()
    record = logging.LogRecord(
        "swift_autotrader.telegram", logging.WARNING, __file__, 12,
        "synthetic secret message text", (), None,
    )
    rendered = formatter.format(record)
    assert "synthetic secret" not in rendered
    assert json.loads(rendered)["event"] == "redacted_event"
