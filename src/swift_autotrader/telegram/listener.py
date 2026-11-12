"""Bounded read-only event ingestion; no parser or broker dependency.

A full queue raises a fatal alarm and disconnects the source, rather than silently
losing a message or falsely acknowledging it. Recovery must be reviewed locally.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol

from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.telegram.events import TelegramMessage


class QueueOverflow(RuntimeError):
    """Listener could not durably accept an incoming update."""


class TelegramSource(Protocol):
    async def listen(
        self, on_event: Callable[[TelegramMessage], Awaitable[None]]
    ) -> None: ...


class TelegramListener:
    def __init__(
        self,
        store: TelegramStore,
        *,
        channel_id: int,
        capacity: int = 512,
        max_live_age_seconds: int = 120,
        started_at_utc: datetime | None = None,
    ) -> None:
        if not 1 <= capacity <= 4096:
            raise ValueError("queue capacity must be between 1 and 4096")
        self.store = store
        self.channel_id = channel_id
        self.queue: asyncio.Queue[TelegramMessage] = asyncio.Queue(maxsize=capacity)
        self.max_live_age_seconds = max_live_age_seconds
        self.started_at_utc = started_at_utc or datetime.now(UTC)
        self.filtered = 0
        self.stored = 0
        self.duplicates = 0
        self.failure: BaseException | None = None
        self.log = logging.getLogger("swift_autotrader.telegram")

    async def _worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                outcome = self.store.ingest(
                    event,
                    session_started_at_utc=self.started_at_utc,
                    max_live_age_seconds=self.max_live_age_seconds,
                )
                if outcome.stored:
                    self.stored += 1
                else:
                    self.duplicates += 1
            except Exception as exc:
                self.failure = exc
                self.log.error("store_unavailable")
                raise
            finally:
                self.queue.task_done()

    async def accept(self, event: TelegramMessage) -> None:
        if event.channel_id != self.channel_id:
            self.filtered += 1
            return
        if self.failure is not None:
            raise RuntimeError("Telegram persistence worker failed") from self.failure
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            self.failure = QueueOverflow("Telegram queue capacity exceeded")
            self.log.error("queue_overflow")
            self.store.mark_health(
                "DEGRADED", at=datetime.now(UTC),
                error_code="QUEUE_OVERFLOW", overflow=True,
            )
            raise self.failure from exc

    async def run(self, source: TelegramSource) -> None:
        # Starting an event loop does NOT imply Telegram authorization succeeded.
        self.store.mark_health("DISCONNECTED", at=datetime.now(UTC))
        self.log.info("listener_started")
        worker = asyncio.create_task(self._worker())
        source_task = asyncio.create_task(source.listen(self.accept))
        source_error: BaseException | None = None
        try:
            done, _ = await asyncio.wait(
                {worker, source_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                # If persistence dies while Telegram is quiet, disconnect immediately.
                if not worker.cancelled():
                    with suppress(Exception):
                        await worker
                source_task.cancel()
                with suppress(asyncio.CancelledError):
                    await source_task
            else:
                try:
                    await source_task
                except BaseException as exc:
                    source_error = exc
                if not worker.done():
                    try:
                        await asyncio.wait_for(self.queue.join(), timeout=20)
                    except TimeoutError as exc:
                        self.failure = exc
                if worker.done() and not worker.cancelled():
                    with suppress(Exception):
                        await worker
        finally:
            source_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await source_task
            worker.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await worker
            self.store.mark_health(
                "DEGRADED" if self.failure or source_error else "STOPPED",
                at=datetime.now(UTC),
                error_code=(
                    "QUEUE_OVERFLOW" if isinstance(self.failure, QueueOverflow)
                    else "INGEST_FAILURE" if self.failure is not None
                    else "SOURCE_FAILURE" if source_error is not None else None
                ),
            )
            self.log.info("listener_stopped")
        if self.failure is not None:
            raise RuntimeError("Telegram persistence failed") from self.failure
        if source_error is not None:
            raise source_error
