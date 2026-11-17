"""Live-mode assembly and the startup self-check.

Nothing here is reached unless ``Settings.live_mode`` is true, i.e. every live setting
is present (PAPER_ONLY=false, LIVE_ENABLED, ALLOW_BROKER_WRITES, BROKER_BACKEND=
robinhood_live, LIVE_ACCOUNT_NUMBER, LIVE_JOURNAL_PATH). Anything less runs as paper.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, date, datetime
from decimal import Decimal
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
from swift_autotrader.broker.robinhood_mcp import READ_TOOLS, BrokerWritesDisabledError
from swift_autotrader.config import Settings
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.risk_limits import MAX_CONTRACTS_PER_ALERT
from swift_autotrader.signals.identity import OptionContract, OptionRight


class LiveStartupError(RuntimeError):
    """A live start precondition failed; the process must not run live."""


class BorrowedMCPLoop:
    """Run synchronous LiveBroker calls on the existing authenticated MCP loop.

    The Telegram/market-data provider and live broker must share one OAuth
    session. Never create a second client that can race the token refresh.
    LiveBroker methods are invoked from a worker thread, not from this loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        if not loop.is_running():
            raise LiveStartupError('MCP event loop must already be running')
        self.loop = loop

    def run(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self.loop.is_running():
                return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
        # Never deadlock an event loop by synchronously waiting on itself.
        if hasattr(coro, 'close'):
            coro.close()
        raise LiveStartupError('live broker must run from a worker thread')



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
                 client: Any = None, loop: _LoopThread | BorrowedMCPLoop | None = None) -> Broker:
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


class ReadOnlyClient:
    """Wraps an MCP client so only known READ tools can ever be called (rehearsal).

    An allowlist, not a denylist: place_option_order, cancel_option_order and any tool
    this code does not know are refused before they reach the wire.
    """

    ALLOWED = READ_TOOLS | {'get_realized_pnl'}

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def list_tools(self) -> Any:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.ALLOWED:
            raise BrokerWritesDisabledError(f'rehearsal refuses tool {name!r}')
        return await self._inner.call_tool(name, arguments)


def recorded_entry_intents(control_db: Path, *, limit: int = 50) -> list[OrderIntent | str]:
    """Order intents for recorded ENTRY signals, newest first (a str is a bad row's id)."""
    with closing(sqlite3.connect(f'{Path(control_db).resolve().as_uri()}?mode=ro', uri=True,
                                 timeout=5)) as db:
        rows = db.execute(
            'SELECT s.signal_id, s.underlying, s.option_right, s.strike, s.expiration_date, '
            's.original_limit FROM option_signals s WHERE EXISTS (SELECT 1 FROM '
            "signal_parser_decisions d WHERE d.signal_id=s.signal_id AND d.intent='ENTRY' "
            "AND d.status='RECORDED') ORDER BY s.created_at_utc DESC LIMIT ?",
            (limit,)).fetchall()
    out: list[OrderIntent | str] = []
    for sid, underlying, right, strike, expiry, price in rows:
        try:
            contract = OptionContract(underlying, OptionRight(right), Decimal(strike),
                                      date.fromisoformat(expiry))
            out.append(OrderIntent(
                f'{sid}:entry', PaperOrderEngine._key(sid, OrderSide.BUY_TO_OPEN), contract,
                OrderSide.BUY_TO_OPEN, MAX_CONTRACTS_PER_ALERT, Decimal(price)))
        except (ValueError, ArithmeticError):
            out.append(str(sid))
    return out


def rehearse_entries(broker: LiveBroker, control_db: Path, *, limit: int = 50,
                     log_path: Path | None = None) -> list[dict[str, Any]]:
    """Run the full entry preflight for every recorded ENTRY signal; write nothing live.

    Each result is WOULD_SEND (with the exact place_option_order arguments, account masked)
    or DENIED (with the reason). One JSON line per result goes to log_path if given.
    """
    if not broker.rehearsal or broker.writes_enabled:
        raise LiveStartupError('rehearsal needs a broker built with rehearsal=True')
    results: list[dict[str, Any]] = []
    for item in recorded_entry_intents(control_db, limit=limit):
        if isinstance(item, str):
            result: dict[str, Any] = {'logical_order_id': f'{item}:entry', 'status': 'DENIED',
                                      'reason': 'signal row malformed'}
        else:
            result = broker.rehearse(item)
        result['at_utc'] = datetime.now(UTC).isoformat()
        results.append(result)
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_path).open('a', encoding='utf-8') as handle:
            for result in results:
                handle.write(json.dumps(result, sort_keys=True) + '\n')
    return results
