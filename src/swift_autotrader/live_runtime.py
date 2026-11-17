"""Live-mode assembly and the startup self-check.

Nothing here is reached unless ``Settings.live_mode`` is true, i.e. every live setting
is present (PAPER_ONLY=false, LIVE_ENABLED, ALLOW_BROKER_WRITES, BROKER_BACKEND=
robinhood_live, LIVE_ACCOUNT_NUMBER, LIVE_JOURNAL_PATH). Anything less runs as paper.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from swift_autotrader.broker.base import Broker
from swift_autotrader.broker.live_broker import (
    LiveBroker,
    _LoopThread,
    engine_kill_switch,
    entry_signal_gate,
    read_kill_state,
)
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.config import Settings


class LiveStartupError(RuntimeError):
    """A live start precondition failed; the process must not run live."""


class LiveMCPSession:
    """Keeps one authenticated MCP client alive on its own loop thread.

    The context manager must be entered and left by the same task, so a single long-lived
    coroutine owns it until ``close``. The LiveBroker is handed this loop.
    """

    def __init__(self, enrollment: Any) -> None:
        self.loop = _LoopThread()
        self._enrollment = enrollment
        self._ready: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._stop: asyncio.Event | None = None
        self._task: concurrent.futures.Future[None] | None = None

    def start(self, timeout: float = 60.0) -> Any:
        from swift_autotrader.broker.mcp_transport import authenticated_mcp_client

        async def host() -> None:
            self._stop = asyncio.Event()
            try:
                async with authenticated_mcp_client(self._enrollment) as client:
                    self._ready.set_result(client)
                    await self._stop.wait()
            except BaseException as exc:
                if not self._ready.done():
                    self._ready.set_exception(exc)
                raise

        self._task = asyncio.run_coroutine_threadsafe(host(), self.loop.loop)
        return self._ready.result(timeout)

    def close(self) -> None:
        if self._stop is not None:
            self.loop.loop.call_soon_threadsafe(self._stop.set)
        if self._task is not None:
            with suppress(Exception):
                self._task.result(15)
        self.loop.close()


def build_broker(settings: Settings, db_path: Path, *, clock: Callable[[], datetime],
                 client: Any = None, loop: _LoopThread | None = None) -> Broker:
    """Paper broker unless live mode is complete; live then needs an MCP client."""
    if not settings.live_mode:
        return PaperBroker(db_path, clock=clock)
    if client is None or settings.live_journal_path is None:
        raise LiveStartupError('live mode requires an MCP client and a journal path')
    return LiveBroker(
        settings.live_journal_path, client, account_number=settings.live_account_number,
        writes_enabled=True, kill_switch=engine_kill_switch(db_path),
        signal_gate=entry_signal_gate(db_path), clock=clock, loop=loop)


def startup_self_check(settings: Settings, control_db: Path, broker: Broker | None = None,
                       *, out: Callable[[str], object] = print) -> dict[str, str]:
    """Print the active mode and account ending; refuse a live start on any failed check.

    Live needs: the journal file present, the kill switch readable, and the account
    verified as the pinned one (read-only get_accounts through the broker).
    """
    if not settings.live_mode:
        note = (f" (live requested but incomplete, missing: {', '.join(settings.live_fallback)})"
                if settings.live_fallback else "")
        out(f"MODE=PAPER{note}")
        return {"mode": "PAPER", "account": "", "kill_switch": ""}
    out(f"MODE=LIVE account={settings.live_account_ending}")
    journal = settings.live_journal_path
    if journal is None or not Path(journal).is_file():
        raise LiveStartupError("refusing live start: journal is missing")
    try:
        engaged = read_kill_state(control_db)
    except Exception as exc:
        raise LiveStartupError("refusing live start: kill switch cannot be read") from exc
    if not isinstance(broker, LiveBroker) or broker.account_number != settings.live_account_number:
        raise LiveStartupError("refusing live start: no broker bound to the pinned account")
    try:
        broker.verify_account()
    except Exception as exc:
        raise LiveStartupError("refusing live start: account is not the pinned account") from exc
    state = "ENGAGED" if engaged else "clear"
    out(f"self-check ok: journal=present kill_switch={state} "
        f"account={settings.live_account_ending}")
    return {"mode": "LIVE", "account": str(settings.live_account_ending), "kill_switch": state}
