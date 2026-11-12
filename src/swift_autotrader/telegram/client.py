"""Optional Telethon 1.45 MTProto adapter, receive-only and channel-filtered.

Requires a user-authorized local *.session file. Never calls start(), login,
send_message(), download_media(), iter_dialogs() or iter_messages(). No history
backfill is enabled. Telethon's own connectivity may be restored automatically;
any disconnected epoch restarts with a new session freshness fence.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from swift_autotrader.config import Settings
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.telegram.events import EventKind, TelegramMessage
from swift_autotrader.telegram.listener import TelegramListener
from swift_autotrader.telegram.reconnect import with_reconnect


class LocalAuthorizationRequired(RuntimeError):
    """No enrolled local Telegram session or unavailable Swift channel."""


def normalize_telethon_event(event: Any, kind: EventKind) -> TelegramMessage:
    msg = event.message
    reply = getattr(msg, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None) if reply is not None else None
    return TelegramMessage(
        channel_id=int(event.chat_id),
        message_id=int(msg.id),
        sent_at_utc=msg.date,
        received_at_utc=datetime.now(UTC),
        body=msg.raw_text or "",
        kind=kind,
        edited_at_utc=msg.edit_date if kind is EventKind.EDIT else None,
        author_id=int(msg.sender_id) if msg.sender_id is not None else None,
        reply_to_message_id=int(reply_id) if reply_id is not None else None,
    )


class TelethonSource:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_enabled:
            raise ValueError("Enable authorized Telegram ingestion in local settings first")
        self.settings = settings

    async def listen(
        self, on_event: Callable[[TelegramMessage], Awaitable[None]]
    ) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError as exc:
            raise RuntimeError("Install the pinned telegram extra before listening") from exc

        cfg = self.settings
        assert cfg.telegram_api_id is not None
        assert cfg.telegram_session_path is not None
        assert cfg.telegram_channel_id is not None
        client = TelegramClient(
            str(cfg.telegram_session_path),
            cfg.telegram_api_id,
            cfg.telegram_api_hash,
            catch_up=False,
            auto_reconnect=False,
            connection_retries=3,
            retry_delay=2,
            request_retries=3,
            timeout=10,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=25)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
                raise LocalAuthorizationRequired("Authorized user session is missing")
            try:
                channel = await asyncio.wait_for(
                    client.get_entity(cfg.telegram_channel_id), timeout=15
                )
            except Exception as exc:
                raise LocalAuthorizationRequired("Configured channel unavailable") from exc

            from telethon import utils

            if int(utils.get_peer_id(channel)) != cfg.telegram_channel_id:
                raise LocalAuthorizationRequired("Configured channel identity mismatch")

            failure: BaseException | None = None

            async def dispatch(event: Any, kind: EventKind) -> None:
                nonlocal failure
                if int(event.chat_id or 0) != cfg.telegram_channel_id:
                    return
                try:
                    await on_event(normalize_telethon_event(event, kind))
                except Exception as exc:
                    failure = exc
                    await client.disconnect()

            @client.on(events.NewMessage(chats=[channel]))
            async def on_new(event: Any) -> None:
                await dispatch(event, EventKind.NEW)

            @client.on(events.MessageEdited(chats=[channel]))
            async def on_edited(event: Any) -> None:
                await dispatch(event, EventKind.EDIT)

            await client.run_until_disconnected()
            if failure is not None:
                raise RuntimeError("Telegram ingestion failed and source disconnected") from failure
            raise ConnectionError("Telegram connection ended")
        finally:
            await client.disconnect()


def start_listener(settings: Settings) -> None:
    """Only local, explicitly enabled users can initiate a read-only connection."""
    if not settings.telegram_enabled or settings.telegram_channel_id is None:
        raise LocalAuthorizationRequired("Telegram listener has not been locally enabled")
    store = TelegramStore(settings.db_path, settings.telegram_channel_id)
    store.health()  # Raises on missing migration, before any network connection.

    async def run_once() -> None:
        listener = TelegramListener(
            store,
            channel_id=settings.telegram_channel_id,
            capacity=settings.telegram_queue_capacity,
            max_live_age_seconds=settings.telegram_max_live_age_seconds,
            started_at_utc=datetime.now(UTC),
        )
        await listener.run(TelethonSource(settings))

    async def supervised() -> None:
        await with_reconnect(
            run_once,
            attempts=settings.telegram_reconnect_attempts,
        )

    asyncio.run(supervised())
