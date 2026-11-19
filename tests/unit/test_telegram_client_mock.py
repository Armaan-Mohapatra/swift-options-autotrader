"""Synthetic contract tests for the optional Telethon adapter; zero network traffic."""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.telegram.client import LocalAuthorizationRequired, TelethonSource
from swift_autotrader.telegram.events import EventKind

CHANNEL = -1001234567890
OTHER_CHANNEL = -1007777777777


def install_fake_telethon(monkeypatch, *, authorized=True):
    calls = []

    class FakeClient:
        def __init__(self, path, api_id, api_hash, **kwargs):
            calls.append(("constructed", path, api_id, kwargs["auto_reconnect"]))
            self.handlers = []

        async def connect(self):
            calls.append(("connect",))

        async def is_user_authorized(self):
            calls.append(("authorized_check",))
            return authorized

        async def get_entity(self, peer_id):
            calls.append(("get_entity", peer_id))
            return SimpleNamespace(id=CHANNEL)

        def on(self, event_filter):
            calls.append(("subscribe", event_filter.chat_id))

            def decorator(handler):
                self.handlers.append(handler)
                return handler

            return decorator

        async def run_until_disconnected(self):
            def wrapped(chat_id, *, edit_date=None):
                payload = SimpleNamespace(
                    id=456, date=datetime.now(UTC), raw_text="synthetic only",
                    edit_date=edit_date, sender_id=42,
                    reply_to=SimpleNamespace(reply_to_msg_id=321),
                )
                return SimpleNamespace(chat_id=chat_id, message=payload)

            await self.handlers[0](wrapped(CHANNEL))
            await self.handlers[0](wrapped(OTHER_CHANNEL))
            await self.handlers[1](wrapped(CHANNEL, edit_date=datetime.now(UTC)))
            calls.append(("handlers_completed",))

        async def disconnect(self):
            calls.append(("disconnect",))

    class FakeFilter:
        def __init__(self, *, chats):
            self.chat_id = chats[0].id

    fake_module = ModuleType("telethon")
    fake_module.TelegramClient = FakeClient
    fake_module.events = SimpleNamespace(NewMessage=FakeFilter, MessageEdited=FakeFilter)
    fake_module.utils = SimpleNamespace(get_peer_id=lambda peer: peer.id)
    monkeypatch.setitem(sys.modules, "telethon", fake_module)
    return calls


def test_mocked_telethon_private_channel_new_edits_filtered_and_read_only(monkeypatch, tmp_path):
    calls = install_fake_telethon(monkeypatch)
    path = tmp_path / "synthetic.sqlite3"
    apply_migrations(path)
    store = TelegramStore(path, CHANNEL)
    cfg = Settings(
        telegram_enabled=True, telegram_channel_id=CHANNEL,
        telegram_api_id=123, telegram_api_hash="a" * 32,
        telegram_session_path=Path("/fake/protected.session"),
    )
    messages = []

    async def collector(event):
        messages.append(event)

    async def scenario():
        with pytest.raises(ConnectionError, match="connection ended"):
            await TelethonSource(cfg, store=store).listen(collector)

    asyncio.run(scenario())
    assert len(messages) == 2
    assert [event.kind for event in messages] == [EventKind.NEW, EventKind.EDIT]
    assert all(event.channel_id == CHANNEL for event in messages)
    assert all(event.reply_to_message_id == 321 for event in messages)
    assert ("get_entity", CHANNEL) in calls
    assert ("subscribe", CHANNEL) in calls
    assert store.health()["last_connected_at_utc"] is not None
    assert not any("send" in str(call) or "post" in str(call) for call in calls)


def test_mocked_telethon_denies_unauthorized_session_without_subscribing(monkeypatch):
    calls = install_fake_telethon(monkeypatch, authorized=False)
    cfg = Settings(
        telegram_enabled=True, telegram_channel_id=CHANNEL,
        telegram_api_id=123, telegram_api_hash="a" * 32,
        telegram_session_path=Path("/fake/protected.session"),
    )

    async def collector(event):
        raise AssertionError("Unauthorized client must not receive messages")

    async def scenario():
        with pytest.raises(LocalAuthorizationRequired):
            await TelethonSource(cfg).listen(collector)

    asyncio.run(scenario())
    assert ("authorized_check",) in calls
    assert not any(call[0] in {"get_entity", "subscribe"} for call in calls)
