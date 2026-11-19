"""Adversarial regression and supervised lifecycle test suite for Swift AutoTrader.

Covers:
1. Multiple simultaneous eligible alerts filtered by authorized scope (symbol / message_id).
2. Previously recorded eligible signals present in DB with no new qualifying decisions (fail-closed).
3. No qualifying parser decisions fail-closed (allowed_signal_ids is frozenset(), 0 orders).
4. Unmatched or invalid authorization scope (fail-closed).
5. Durable acceptance-state handling (existing buy intents in DB lock out new entries across restarts).
6. Failure to persist acceptance stop marker raises RuntimeError and fails closed.
7. Brokerage response uncertainty and recovery reconciliation without duplicate orders.
8. Provider-required approval (customer_approval_required) blocks placement.
9. Complete supervised order lifecycle (ACCEPTED -> resting -> fill -> exit signal -> STC fill -> CLOSED).
10. Watchdog cancellation of resting unfilled orders.
11. Paper vs Live mutual exclusion lock collisions.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodMCPBroker,
)
from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.live_service import (
    _has_active_orders_or_positions,
    run_live_service,
    write_stop_marker_durable,
)
from swift_autotrader.orders.acceptance_scope import AcceptanceScope
from swift_autotrader.orders.engine import ReconciliationError
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.supervisor import run_supervisor
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from swift_autotrader.windows_runtime import PaperSupervisor, single_instance
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP, live_journal


class OpenMarketCalendar:
    """Session calendar double that always reports regular market hours."""

    def is_regular_session_open(self, at_utc: datetime) -> bool:
        return True


@pytest.fixture
def managed_live_env(tmp_path: Path):
    """Set up an isolated managed production installation directory."""
    root = tmp_path / "SwiftAutoTrader-Supervised-Install"
    for part in ("data", "logs", "backups", "secrets", "state", "releases", "config", "scripts"):
        (root / part).mkdir(parents=True, exist_ok=True)

    (root / "deployment.json").write_text(
        json.dumps({
            "product": "swift-options-autotrader",
            "mode": "LIVE",
            "live_enabled": True,
            "broker_writes_enabled": True,
            "created_utc": "2026-11-16T05:00:00Z",
            "installation_guid": "rehearsal-supervised-0000-0000-000000000000",
        }),
        encoding="utf-8",
    )

    paper_db = root / "data" / "swift.sqlite3"
    live_db = root / "data" / "live.sqlite3"
    apply_migrations(paper_db)
    apply_migrations(live_db)

    session_file = root / "secrets" / "telegram.session"
    session_file.write_bytes(b"synthetic_telegram_session")
    (root / "secrets" / "oauth.dpapi").write_bytes(b"synthetic_oauth")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="RH_LIVE_ACC_SUPERVISED",
        db_path=live_db,
        telegram_enabled=True,
        telegram_channel_id=-1001234567890,
        telegram_api_id=123456,
        telegram_api_hash="a" * 32,
        telegram_session_path=session_file,
        managed_install_root=root,
        live_journal_path=live_journal(root / "data" / "live_journal.sqlite3"),
        max_daily_loss_usd=Decimal("750"),
    )
    return root, settings, paper_db, live_db


def test_multiple_simultaneous_alerts_scoped_to_authorized_symbol(managed_live_env) -> None:
    """Verify multiple simultaneous alerts are filtered strictly to the authorized symbol."""
    root, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    # Ingest 3 simultaneous alerts
    for mid, body in [
        (401, "BTO SPY 680C 11/20 @ 1.20"),
        (402, "BTO QQQ 500C 11/20 @ 2.00"),
        (403, "BTO AAPL 250C 11/20 @ 1.50"),
    ]:
        msg = TelegramMessage(
            channel_id=settings.telegram_channel_id,
            message_id=mid,
            sent_at_utc=now,
            received_at_utc=now,
            body=body,
            kind=EventKind.NEW,
            origin=EventOrigin.LIVE,
        )
        store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    # Execute with single_entry=True and authorized_symbol='QQQ'
    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            single_entry=True,
            authorized_symbol="QQQ",
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )

    # Verify EXACTLY 1 order submitted, strictly for QQQ
    assert len(fake_mcp.orders) == 1
    order = next(iter(fake_mcp.orders.values()))
    assert order["direction"] == "buy"
    assert Decimal(order["quantity"]) == 1
    # Verify underlying is QQQ
    with sqlite3.connect(live_db) as conn:
        row = conn.execute(
            "SELECT s.underlying FROM order_intents o JOIN option_signals s ON s.signal_id=o.signal_id"
        ).fetchone()
        assert row is not None
        assert row[0] == "QQQ"


def test_multiple_simultaneous_alerts_scoped_to_target_message_id(managed_live_env) -> None:
    """Verify multiple simultaneous alerts are filtered strictly to the target message ID."""
    root, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    # Ingest 2 alerts
    for mid, body in [
        (501, "BTO SPY 680C 11/20 @ 1.20"),
        (502, "BTO AAPL 250C 11/20 @ 1.50"),
    ]:
        msg = TelegramMessage(
            channel_id=settings.telegram_channel_id,
            message_id=mid,
            sent_at_utc=now,
            received_at_utc=now,
            body=body,
            kind=EventKind.NEW,
            origin=EventOrigin.LIVE,
        )
        store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    # Execute with single_entry=True and target_message_id=502 (AAPL)
    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            single_entry=True,
            target_message_id=502,
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )

    # Verify EXACTLY 1 order submitted, strictly for AAPL (message 502)
    assert len(fake_mcp.orders) == 1
    with sqlite3.connect(live_db) as conn:
        row = conn.execute(
            "SELECT s.underlying FROM order_intents o JOIN option_signals s ON s.signal_id=o.signal_id"
        ).fetchone()
        assert row is not None
        assert row[0] == "AAPL"


def test_previously_recorded_signals_with_no_qualifying_parser_decisions(managed_live_env) -> None:
    """Verify that previously recorded signals in DB do NOT trigger orders when no new qualifying decision occurs."""
    root, settings, _, live_db = managed_live_env
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    # Seed an earlier candidate signal in option_signals WITHOUT order intent
    with sqlite3.connect(live_db) as conn:
        conn.execute(
            "INSERT INTO raw_events(channel_id, telegram_message_id, edit_version, received_at_utc, sent_at_utc, body) "
            "VALUES(?, '99', 0, ?, ?, 'BTO NVDA 140C 11/20 @ 3.00')",
            (str(settings.telegram_channel_id), now.isoformat(), now.isoformat()),
        )
        raw_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO option_signals(signal_id, source_event_id, underlying, option_right, strike, expiration_date, original_limit, state, created_at_utc) "
            "VALUES('sig-prior-nvda', ?, 'NVDA', 'CALL', '140.0', '2026-11-20', '3.00', 'SEEN', ?)",
            (raw_id, now.isoformat()),
        )
        conn.commit()

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    # Dispatcher in single_entry mode with authorized_symbol='SPY' (no SPY alerts in DB)
    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
        authorized_symbol="SPY",
    )

    result = asyncio.run(dispatcher.run_once())

    # FAIL-CLOSED ASSERTIONS:
    # 0 parser events, 0 entries submitted
    assert result.entry_orders_submitted == 0
    assert len(fake_mcp.orders) == 0

    # Verify order_intents table in DB is still completely empty
    with sqlite3.connect(live_db) as conn:
        assert conn.execute("SELECT count(*) FROM order_intents").fetchone()[0] == 0


def test_no_new_qualifying_parser_decisions_fails_closed(managed_live_env) -> None:
    """Verify that a cycle with no messages produces allowed_signal_ids=frozenset() and zero orders."""
    _, settings, _, live_db = managed_live_env
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
    )

    result = asyncio.run(dispatcher.run_once())
    assert result.entry_orders_submitted == 0
    assert len(fake_mcp.orders) == 0


def test_unmatched_authorization_scope_fails_closed(managed_live_env) -> None:
    """Verify that an alert for an unauthorized symbol is rejected fail-closed."""
    _, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    # Ingest SPY alert
    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=601,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    # Dispatcher authorized ONLY for TSLA
    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
        authorized_symbol="TSLA",
    )

    result = asyncio.run(dispatcher.run_once())
    assert result.entry_orders_submitted == 0
    assert len(fake_mcp.orders) == 0


def test_durable_acceptance_state_prevents_duplicate_on_restart(managed_live_env) -> None:
    """Verify that an existing BUY_TO_OPEN intent in the database permanently bars new entries across restarts."""
    root, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    msg1 = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=701,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg1, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    # Cycle 1: places first entry order
    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            single_entry=True,
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )
    assert len(fake_mcp.orders) == 1

    # Ingest a SECOND eligible entry alert (QQQ)
    msg2 = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=702,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO QQQ 500C 11/20 @ 2.00",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg2, session_started_at_utc=now - timedelta(minutes=5))

    # Remove stop marker to simulate a service restart or operator re-invocation
    (root / "state" / "stop.request").unlink(missing_ok=True)

    # Cycle 2: restart live service in single_entry mode
    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            single_entry=True,
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )

    # CRITICAL: Verify NO second order was submitted
    assert len(fake_mcp.orders) == 1
    with sqlite3.connect(live_db) as conn:
        assert conn.execute("SELECT count(*) FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0] == 1


def test_failed_marker_writes_raise_and_fail_closed(managed_live_env) -> None:
    """Verify write_stop_marker_durable fails closed by raising RuntimeError on any OSError."""
    root, _, _, _ = managed_live_env
    state_dir = root / "state"

    # Make state directory read-only or patch os.replace to raise OSError
    with patch("os.replace", side_effect=OSError("Disk full or permission denied")):
        with pytest.raises(RuntimeError, match="Failed to durably persist stop marker"):
            write_stop_marker_durable(root, "STOP")


def test_provider_required_approval_blocks_placement(managed_live_env) -> None:
    """Verify that customer_approval_required=True in broker review blocks unattended execution."""
    root, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=801,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    # Patch call_tool for review_option_order to return approval_required: True
    orig_call_tool = fake_mcp.call_tool

    async def patched_call_tool(name: str, arguments: dict[str, Any]):
        if name == "review_option_order":
            return {"structuredContent": {"data": {
                "order_checks": [],
                "approval_required": True,
                "customer_approval_required": True,
            }}}
        return await orig_call_tool(name, arguments)

    fake_mcp.call_tool = patched_call_tool

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            single_entry=True,
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )

    # Provider review required approval -> 0 orders placed!
    assert len(fake_mcp.orders) == 0
    with sqlite3.connect(live_db) as conn:
        assert conn.execute("SELECT count(*) FROM order_intents").fetchone()[0] == 0


@pytest.mark.parametrize("exit_origin", [EventOrigin.LIVE, EventOrigin.RECOVERED])
def test_complete_supervised_order_lifecycle(managed_live_env, exit_origin) -> None:
    """Demonstrate complete supervised lifecycle: ACCEPTED -> resting -> fill -> exit signal -> STC fill -> CLOSED."""
    root, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    # 1. Ingest entry alert
    entry_msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=901,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(entry_msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
        authorized_symbol="SPY",
    )

    # Step A: Run cycle 1 -> BUY order submitted and ACCEPTED (resting)
    res1 = asyncio.run(dispatcher.run_once())
    assert res1.entry_orders_submitted == 1
    assert len(fake_mcp.orders) == 1
    buy_ord_id = next(iter(fake_mcp.orders.keys()))
    assert fake_mcp.orders[buy_ord_id]["state"] == "confirmed"

    # Verify state in DB is ACCEPTED
    with sqlite3.connect(live_db) as conn:
        state = conn.execute("SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0]
        assert state == "ACCEPTED"
    assert _has_active_orders_or_positions(live_db) is True

    # Step B: Broker fills buy order
    fake_mcp.fill_order(buy_ord_id, filled_quantity=1, fill_price="1.20")
    res2 = asyncio.run(dispatcher.run_once())
    # Cycle 2 reconciles fill -> DB state is FILLED, open position count is 1
    with sqlite3.connect(live_db) as conn:
        state = conn.execute("SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0]
        assert state == "FILLED"
        sig_state = conn.execute("SELECT state FROM option_signals WHERE underlying='SPY'").fetchone()[0]
        assert sig_state == "OPEN"
    assert _has_active_orders_or_positions(live_db) is True

    # Step C: Ingest exit alert SPY ALL OUT
    now = clock.advance(60)
    fake_mcp.set_quote("opt-spy-680c", "1.80", "1.85", updated_at=clock.now())
    exit_msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=902,
        reply_to_message_id=901,
        sent_at_utc=now,
        received_at_utc=now,
        body="SPY ALL OUT",
        kind=EventKind.NEW,
        origin=exit_origin,
    )
    exit_ingest = store.ingest(exit_msg, session_started_at_utc=now - timedelta(minutes=5))
    assert exit_ingest.eligible_for_future_processing is (exit_origin is EventOrigin.LIVE)

    # Step D: Run cycle 3 -> exit policy submits SELL_TO_CLOSE order
    res3 = asyncio.run(dispatcher.run_once())
    assert res3.close_orders_submitted == 1
    assert len(fake_mcp.orders) == 2
    sell_ord_id = [oid for oid in fake_mcp.orders if oid != buy_ord_id][0]
    assert fake_mcp.orders[sell_ord_id]["direction"] == "sell"

    # Step E: Broker fills sell order
    fake_mcp.fill_order(sell_ord_id, filled_quantity=1, fill_price="1.80")
    res4 = asyncio.run(dispatcher.run_once())

    # Step F: Terminal settlement verified: position is CLOSED, no active orders
    with sqlite3.connect(live_db) as conn:
        sell_state = conn.execute("SELECT state FROM order_intents WHERE side='SELL_TO_CLOSE'").fetchone()[0]
        assert sell_state == "FILLED"
        sig_state = conn.execute("SELECT state FROM option_signals WHERE underlying='SPY'").fetchone()[0]
        assert sig_state == "CLOSED"

    assert _has_active_orders_or_positions(live_db) is False


def test_cancellation_and_watchdog_unfilled_order(managed_live_env) -> None:
    """Verify watchdog cancels resting unfilled order after timeout and reconciles to terminal CANCELED."""
    _, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=950,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
    )

    # Cycle 1 submits BUY
    res1 = asyncio.run(dispatcher.run_once())
    assert res1.entry_orders_submitted == 1
    buy_ord_id = next(iter(fake_mcp.orders.keys()))

    # Advance clock past order expiration (ENTRY_TIMEOUT_SECONDS is 3600s)
    clock.advance(3610)
    fake_mcp.set_quote("opt-spy-680c", "1.20", "1.22", updated_at=clock.now())

    # Cycle 2 watchdog runs and requests cancel
    res2 = asyncio.run(dispatcher.run_once())
    assert res2.cancellations >= 1
    assert fake_mcp.orders[buy_ord_id]["state"] == "cancelled"

    # Terminal state in DB is CANCELED
    with sqlite3.connect(live_db) as conn:
        state = conn.execute("SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0]
        assert state == "CANCELED"

    assert _has_active_orders_or_positions(live_db) is False


def test_competing_service_mutual_exclusion(managed_live_env) -> None:
    """Verify mutual exclusion prevents concurrent execution of Paper and Live services."""
    root, settings, _, _ = managed_live_env

    # 1. When service.lock is held (e.g. by Paper), run_supervisor fails
    with single_instance(root / "state" / "service.lock"):
        with pytest.raises(RuntimeError, match="already running"):
            run_supervisor(root, config=settings, once=True)

    # 2. When supervisor.lock is held (e.g. by Live), PaperSupervisor fails
    (root / "deployment.json").write_text(
        json.dumps({
            "product": "swift-options-autotrader",
            "mode": "PAPER_ONLY",
            "live_enabled": False,
            "broker_writes_enabled": False,
        }),
        encoding="utf-8",
    )
    paper_env = {
        "PAPER_ONLY": "true",
        "LIVE_ENABLED": "false",
        "DB_PATH": str(root / "data" / "swift.sqlite3"),
        "SWIFT_INSTALL_ROOT": str(root),
    }
    with single_instance(root / "state" / "supervisor.lock"):
        supervisor = PaperSupervisor(root, env=paper_env)
        with pytest.raises(RuntimeError, match="already running"):
            supervisor.run(max_cycles=1)


@pytest.mark.parametrize("appears_in_history", [True, False])
def test_brokerage_response_uncertainty_and_restart_reconciliation(managed_live_env, appears_in_history) -> None:
    """An uncertain placement is never resent; after restart it reconciles from real-shape history.

    Real get_option_orders responses do not echo ref_id, so LiveBroker matches by account,
    contract, side, quantity, exact limit and agent-created time. Absence never proves
    "not placed": the order stays unresolved and blocks, it is never placed again.
    """
    _, settings, _, live_db = managed_live_env
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)

    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=980,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    provider = MCPReadOnlyOptionData(mcp_broker, calendar=OpenMarketCalendar(), clock=clock)

    dispatcher = LiveDispatcher(
        live_db,
        settings.telegram_channel_id,
        mcp_broker,
        account,
        provider,
        settings,
        clock=clock,
        single_entry=True,
    )

    # 1. The placement call times out: its disposition is unknown.
    fake_mcp.fail_tool("place_option_order", TimeoutError("Broker gateway timeout"))
    with pytest.raises(ReconciliationError, match="Unresolved live order"):
        asyncio.run(dispatcher.run_once())

    with sqlite3.connect(live_db) as conn:
        rows = conn.execute("SELECT logical_order_id, state FROM order_intents").fetchall()
        assert len(rows) == 1
        order_id, state = rows[0]
        assert state == "INTENT_RECORDED"
    assert sum(name == "place_option_order" for name, _ in fake_mcp.calls) == 1

    # 2. The broker did receive it: it appears in history in the real response shape.
    fake_mcp.clear_faults()
    placed = next(args for name, args in fake_mcp.calls if name == "place_option_order")
    if appears_in_history:
        fake_mcp.orders["rh_ord_recovered_980"] = {
            "id": "rh_ord_recovered_980",
            "account_number": settings.robinhood_mcp_account,
            "state": "confirmed", "type": "limit",
            "quantity": "1.0000", "price": "1.2000", "processed_quantity": "0.0000",
            "agent_initiated": True, "placed_agent": "agent",
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "legs": [dict(leg, executions=[]) for leg in placed["legs"]],
        }

    # Reconstruct ALL application-side objects from the durable databases.
    del dispatcher, provider, mcp_broker
    restarted_mcp = RobinhoodMCPBroker(
        fake_mcp, approved_account_number=settings.robinhood_mcp_account,
    )
    restarted = LiveDispatcher(
        live_db, settings.telegram_channel_id, restarted_mcp, account,
        MCPReadOnlyOptionData(restarted_mcp, calendar=OpenMarketCalendar(), clock=clock),
        settings, clock=clock, single_entry=True,
    )
    if appears_in_history:
        assert asyncio.run(restarted.run_once()).entry_orders_submitted == 0
    else:
        with pytest.raises(ReconciliationError, match="Unresolved live order"):
            asyncio.run(restarted.run_once())

    assert sum(name == "place_option_order" for name, _args in fake_mcp.calls) == 1
    with sqlite3.connect(live_db) as conn:
        all_intents = conn.execute(
            "SELECT logical_order_id,state FROM order_intents"
        ).fetchall()
    assert all_intents == [(order_id, "ACCEPTED" if appears_in_history else "INTENT_RECORDED")]


@pytest.mark.parametrize("mismatch", ["none", "strike", "expiry", "right", "limit", "expired"])
def test_exact_supervised_receipt_enforced_before_broker_submission(managed_live_env, mismatch):
    """Exercise the real engine authorization callback through a fake provider only."""
    from dataclasses import replace
    from datetime import date
    _, settings, _, live_db = managed_live_env
    now = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    store = TelegramStore(live_db, settings.telegram_channel_id)
    store.ingest(
        TelegramMessage(
            channel_id=settings.telegram_channel_id, message_id=1001,
            sent_at_utc=now, received_at_utc=now, body="BTO SPY 680C 11/20 @ 1.20",
            kind=EventKind.NEW, origin=EventOrigin.LIVE,
        ),
        session_started_at_utc=now - timedelta(minutes=5),
    )
    with sqlite3.connect(live_db) as conn:
        revision_id = conn.execute(
            "SELECT revision_id FROM telegram_message_revisions WHERE message_id=1001"
        ).fetchone()[0]
    contract = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
    scope = AcceptanceScope(
        settings.robinhood_mcp_account, settings.telegram_channel_id, 1001,
        revision_id, contract, 1, Decimal("1.25"), now + timedelta(seconds=90),
    )
    if mismatch == "strike":
        scope = replace(scope, contract=replace(contract, strike=Decimal("681")))
    elif mismatch == "expiry":
        scope = replace(scope, contract=replace(contract, expiration=date(2026, 11, 27)))
    elif mismatch == "right":
        scope = replace(scope, contract=replace(contract, right=OptionRight.PUT))
    elif mismatch == "limit":
        scope = replace(scope, maximum_limit=Decimal("1.00"))
    elif mismatch == "expired":
        scope = replace(scope, expires_at=now)

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account, buying_power=Decimal("10000"), clock=clock,
    )
    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True, options_level="option_level_2")
    mcp = RobinhoodMCPBroker(fake_mcp, approved_account_number=settings.robinhood_mcp_account)
    dispatcher = LiveDispatcher(
        live_db, settings.telegram_channel_id, mcp, account,
        MCPReadOnlyOptionData(mcp, calendar=OpenMarketCalendar(), clock=clock), settings,
        clock=clock, single_entry=True, acceptance_scope=scope,
    )
    if mismatch == "none":
        assert asyncio.run(dispatcher.run_once()).entry_orders_submitted == 1
        assert len(fake_mcp.orders) == 1
    else:
        with pytest.raises(PermissionError, match="outside supervised authorization"):
            asyncio.run(dispatcher.run_once())
        assert not any(name == "place_option_order" for name, _args in fake_mcp.calls)
        with sqlite3.connect(live_db) as conn:
            assert conn.execute("SELECT count(*) FROM order_intents").fetchone()[0] == 0
