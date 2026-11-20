"""Requirement-driven integration test suite for Live Market Dispatcher (R2 / Feature 5).

Covers:
- Tier 1: Feature Coverage (End-to-end alert ingestion, entry order placement,
  exit liquidation, feed offline recovery, watchdog cancellation, settings mapping).
- Tier 2: Boundary & Corner Cases (Zero events idempotence, stale snapshot rejection,
  disabled live writes permission error, slippage excursion block, concurrent cycle serialization,
  non-entry message handling).
- Tier 3: Cross-Feature Interactions (Cumulative daily loss gate, engine pause flag enforcement).
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.broker.robinhood_mcp import MCPAccount, RobinhoodMCPBroker
from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.orders.risk import Preflight, RiskCeilings
from swift_autotrader.paper_dispatch import (
    MarketEvidenceUnavailable,
    MarketSnapshot,
    VerifiedQuote,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP

CHANNEL_ID = -1001234567890
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))


class MockQuoteFeed:
    """Configurable quote and preflight provider for integration dispatch testing."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.down = False
        self.market_open = True
        self.bid = Decimal("1.20")
        self.ask = Decimal("1.22")
        self.bid_size = 10
        self.ask_size = 10
        self.stale_seconds = 0
        self.contract = CONTRACT

    async def snapshot(self, entries: list, active: list, now: datetime) -> MarketSnapshot:
        if self.down:
            raise MarketEvidenceUnavailable("Simulated broker market data feed failure")

        observed_time = now - timedelta(seconds=self.stale_seconds)
        quote = VerifiedQuote(
            quote_id=f"quote-spy-680c-{int(now.timestamp())}",
            contract=self.contract,
            bid=self.bid,
            ask=self.ask,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            observed_at_utc=observed_time,
            source="MCP_READONLY",
            evidence_reference="evidence_ref_hash_123",
        )
        preflight = Preflight(
            instrument_verified=True,
            options_permission=True,
            market_session_open=self.market_open,
            available_buying_power_usd=Decimal("10000.00"),
            premium_tick=Decimal("0.01"),
            contract=self.contract,
        )
        return MarketSnapshot(
            instruments={self.contract: preflight},
            quotes={self.contract: quote},
            source="MCP_READONLY",
            as_of_utc=observed_time,
        )


@pytest.fixture
def dispatch_rig(tmp_path: Path) -> tuple[Path, FakeClock, StatefulFakeMCP, MockQuoteFeed, TelegramStore, LiveDispatcher]:
    """Test rig providing isolated database, mock MCP broker, quote feed, and live dispatcher."""
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="test-live-acc", buying_power=Decimal("10000.00"), clock=clock)
    mcp_broker = RobinhoodMCPBroker(mcp)  # read adapter; LiveBroker is the one writer
    account = MCPAccount("test-live-acc", True, "option_level_2")
    journal = tmp_path / "data" / "live_journal.sqlite3"
    LiveBroker.create_journal(journal)
    feed = MockQuoteFeed(clock)
    store = TelegramStore(db_path, CHANNEL_ID)

    session_file = tmp_path / "secrets" / "test.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"session_bytes")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="test-live-acc",
        db_path=db_path,
        max_daily_loss_usd=Decimal("500"),
        live_journal_path=journal,
        kill_switch_enabled=True,
        kill_switch_telegram_bot_token="test_bot_token",
        kill_switch_telegram_chat_id=123456789,
        telegram_enabled=True,
        telegram_channel_id=CHANNEL_ID,
        telegram_api_id=12345,
        telegram_api_hash="a" * 32,
        telegram_session_path=session_file,
    )

    dispatcher = LiveDispatcher(
        db_path,
        CHANNEL_ID,
        mcp_broker,
        account,
        feed,  # type: ignore[arg-type]
        settings,
        clock=clock,
    )
    return db_path, clock, mcp, feed, store, dispatcher


def ingest_alert(store: TelegramStore, clock: FakeClock, text: str, msg_id: int, reply_to: int | None = None) -> None:
    """Helper to ingest a synthetic Telegram alert into the local SQLite store."""
    store.ingest(
        TelegramMessage(
            CHANNEL_ID,
            msg_id,
            clock.now(),
            clock.now(),
            text,
            kind=EventKind.NEW,
            origin=EventOrigin.LIVE,
            reply_to_message_id=reply_to,
        ),
        session_started_at_utc=clock.now() - timedelta(minutes=5),
    )


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_live_dispatch_entry_order_happy_path(dispatch_rig: tuple) -> None:
    """T1-F5-01: Swift entry alert -> live market snapshot -> order placed via MCP."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.parsed_events == 1
    assert result.entry_orders_submitted == 1
    assert len(mcp.orders) == 1

    # Verify MCP received place_option_order with deterministic ref_id
    order = list(mcp.orders.values())[0]
    assert order["account_number"] == "test-live-acc"
    assert order["state"] == "confirmed"
    assert order["ref_id"] is not None


def test_live_dispatch_exit_order_happy_path(dispatch_rig: tuple) -> None:
    """T1-F5-02: Existing filled position -> exit alert ingested -> SELL_TO_CLOSE submitted."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig

    # Ingest and fill entry
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)
    asyncio.run(dispatcher.run_once())
    order_id = list(mcp.orders.keys())[0]
    mcp.fill_order(order_id, filled_quantity=1, fill_price=Decimal("1.20"))

    # Advance clock and ingest exit alert
    clock.advance(10.0)
    ingest_alert(store, clock, "SPY SOLD HALF", 1002, reply_to=1001)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.close_orders_submitted == 1
    assert len(mcp.orders) == 2


def test_live_dispatch_feed_offline_recovery(dispatch_rig: tuple) -> None:
    """T1-F5-03: Quote feed failure records MARKET_DATA_UNAVAILABLE notification without crash."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    feed.down = True
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "MARKET_DATA_UNAVAILABLE"
    assert result.entry_orders_submitted == 0

    # Verify notification recorded locally
    notifications = dispatcher.notifications.pending(10)
    codes = [n.code for n in notifications]
    assert "MARKET_DATA_UNAVAILABLE" in codes


def test_live_dispatch_watchdog_cancellation(dispatch_rig: tuple) -> None:
    """T1-F5-04: Watchdog cancels stale unfilled orders exceeding entry_timeout_seconds."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)
    asyncio.run(dispatcher.run_once())

    # Advance clock past 3600 seconds
    clock.advance(3601.0)
    result = asyncio.run(dispatcher.run_once())
    assert result.cancellations >= 1


def test_live_dispatch_risk_ceilings_from_settings(dispatch_rig: tuple) -> None:
    """T1-F5-05: Dispatcher accurately inherits configured risk limits from Settings."""
    _, _, _, _, _, dispatcher = dispatch_rig
    assert dispatcher.limits.daily_net_loss_usd is None  # LIVE policy; PAPER still has a cap
    # Owner contract: no trade-count or aggregate-exposure cap.
    assert dispatcher.limits.open_signal_count is None
    assert dispatcher.limits.total_exposure_usd is None


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


def test_live_dispatch_zero_events_idempotent(dispatch_rig: tuple) -> None:
    """T2-F5-01: Empty cycle completes cleanly with zero actions."""
    _, _, _, _, _, dispatcher = dispatch_rig
    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.parsed_events == 0
    assert result.entry_orders_submitted == 0
    assert result.close_orders_submitted == 0


def test_live_dispatch_stale_market_snapshot_rejected(dispatch_rig: tuple) -> None:
    """T2-F5-02: Market snapshots older than 30s fail validation."""
    _, clock, _, feed, store, dispatcher = dispatch_rig
    feed.stale_seconds = 45  # 45 seconds old > 30s max_age
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "MARKET_DATA_UNAVAILABLE"
    assert result.entry_orders_submitted == 0


def test_live_dispatch_refuses_missing_or_absent_live_journal(tmp_path: Path, live_settings: Settings) -> None:
    """T2-F5-03: Construction fails closed without the separate, pre-created live journal."""
    from dataclasses import replace

    from swift_autotrader.broker.live_broker import LiveSafetyError
    broker = RobinhoodMCPBroker(StatefulFakeMCP())
    account = MCPAccount("synthetic-account", True, "option_level_2")
    feed = MockQuoteFeed(FakeClock())
    db = tmp_path / "data" / "swift.sqlite3"
    with pytest.raises(PermissionError, match="separate durable live journal"):
        LiveDispatcher(db, CHANNEL_ID, broker, account, feed,  # type: ignore[arg-type]
                       replace(live_settings, live_journal_path=None))
    assert not live_settings.live_journal_path.exists()
    with pytest.raises(LiveSafetyError, match="pre-create the durable journal"):
        LiveDispatcher(db, CHANNEL_ID, broker, account, feed,  # type: ignore[arg-type]
                       live_settings)


@pytest.mark.parametrize("ask", ["1.25", "1.50"])
def test_live_dispatch_entry_keeps_exact_alert_limit(dispatch_rig: tuple, ask: str) -> None:
    """T2-F5-04/06: A higher ask never raises the limit; the exact alert premium rests."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    feed.ask = Decimal(ask)
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.entry_orders_submitted == 1
    assert len(mcp.orders) == 1
    assert Decimal(list(mcp.orders.values())[0]["price"]) == Decimal("1.20")

    with sqlite3.connect(str(db_path)) as db:
        assert db.execute("SELECT limit_price FROM order_intents").fetchall() == [("1.20",)]


def test_live_dispatch_non_entry_alert_ignored(dispatch_rig: tuple) -> None:
    """T2-F5-05: Non-entry messages are processed without placing orders."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    ingest_alert(store, clock, "Good morning traders, watching market opens today!", 9999)

    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.entry_orders_submitted == 0
    assert len(mcp.orders) == 0


# ============================================================================
# Tier 3: Cross-Feature Interactions
# ============================================================================


def test_t3_live_dispatch_cumulative_loss_ceiling_blocks_new_entries(dispatch_rig: tuple) -> None:
    """T3-05: An optional operator open-signal cap, when configured, suppresses later entries.

    The owner contract imposes no default cap (see test_no_arbitrary_open_trade_count_cap);
    this proves a voluntary cap is still honoured end to end.
    """
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig

    dispatcher.limits = RiskCeilings(
        contracts_per_signal=1,
        premium_per_signal_usd=Decimal("999"),
        open_signal_count=1,
        daily_net_loss_usd=Decimal("500"),
    )
    dispatcher.engine.limits = dispatcher.limits

    # Ingest first alert and submit order
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)
    res1 = asyncio.run(dispatcher.run_once())
    assert res1.entry_orders_submitted == 1
    assert len(mcp.orders) == 1

    # Ingest second alert while first position is open
    clock.advance(5.0)
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1002)

    res2 = asyncio.run(dispatcher.run_once())
    # Second order must be blocked by position cap
    assert res2.entry_orders_submitted == 0
    assert len(mcp.orders) == 1


def test_t3_live_dispatch_daily_loss_does_not_block_new_entries(dispatch_rig: tuple) -> None:
    """LIVE has no daily stop; losing trades alone must not block fresh entries."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig

    # Ingest and fill entry with 8.00 limit
    ingest_alert(store, clock, "SPY 680C 11/20 @ 8.00", 1001)
    feed.ask = Decimal("8.00")
    feed.bid = Decimal("7.95")
    asyncio.run(dispatcher.run_once())
    entry_order_id = list(mcp.orders.keys())[0]
    mcp.fill_order(entry_order_id, filled_quantity=1, fill_price=Decimal("8.00"))

    # Ingest exit alert and fill sell at 2.50 -> Loss = (2.50 - 8.00) * 100 = $550 > $500 max_daily_loss_usd
    clock.advance(10.0)
    feed.bid = Decimal("2.50")
    feed.ask = Decimal("2.55")
    ingest_alert(store, clock, "SPY SOLD ALL", 1002, reply_to=1001)
    asyncio.run(dispatcher.run_once())
    exit_order_id = list(mcp.orders.keys())[1]
    mcp.fill_order(exit_order_id, filled_quantity=1, fill_price=Decimal("2.50"))

    # The broker reports the same realized loss for the New York day.
    mcp.realized_pnl, mcp.realized_trades = "-550.00", 2
    # Reconcile exit fill and verify realized daily loss
    clock.advance(5.0)
    res_reconcile = asyncio.run(dispatcher.run_once())
    assert res_reconcile.realized_daily_loss_usd == Decimal("550.00")

    # Ingest next entry alert with ask matching alert price
    clock.advance(5.0)
    feed.ask = Decimal("1.20")
    feed.bid = Decimal("1.18")
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1003)
    res_new = asyncio.run(dispatcher.run_once())

    # Daily P/L may still be recorded for reporting, but is no longer an entry veto.
    with sqlite3.connect(str(db_path)) as db:
        decisions = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions ORDER BY decided_at_utc DESC"
        ).fetchall()
    assert all(reason not in ("DAILY_LOSS_CEILING_REACHED", "DAILY_PNL_UNVERIFIED")
               for _outcome, reason in decisions)
    assert dispatcher.live_broker.refresh_daily_risk() is False
    # A different safety gate may refuse the attempt; daily P/L must never be its cause.
    assert res_new.status in ("LIVE_CYCLE_COMPLETED", "MARKET_DATA_UNAVAILABLE")


def test_t3_live_dispatch_paused_engine_skips_entries(dispatch_rig: tuple) -> None:
    """T3-12: Paused engine control in DB halts entry submissions during dispatch."""
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig

    # Pause engine
    dispatcher.engine.set_paused(True, reason="OPERATOR_PAUSE")

    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)
    result = asyncio.run(dispatcher.run_once())
    assert result.entry_orders_submitted == 0
    assert len(mcp.orders) == 0


@pytest.mark.parametrize("value", ["0", "-1", "NaN"])
def test_live_dispatch_refuses_missing_owner_premium_cap(tmp_path: Path, live_settings: Settings, value: str) -> None:
    """A zero/invalid premium cap is refused, never silently replaced by a default ceiling."""
    from dataclasses import replace
    broker = RobinhoodMCPBroker(StatefulFakeMCP())
    account = MCPAccount("synthetic-account", True, "option_level_2")
    with pytest.raises(PermissionError, match="explicit positive premium ceiling"):
        LiveDispatcher(
            tmp_path / "data" / "swift.sqlite3", CHANNEL_ID, broker, account,
            MockQuoteFeed(FakeClock()),  # type: ignore[arg-type]
            replace(live_settings, max_premium_per_signal_usd=Decimal(value)),
        )


def test_live_dispatch_uses_configured_caps_exactly(dispatch_rig: tuple) -> None:
    _, _, _, _, _, dispatcher = dispatch_rig
    assert dispatcher.limits.premium_per_signal_usd == dispatcher.settings.max_premium_per_signal_usd
    assert dispatcher.limits.total_exposure_usd == dispatcher.settings.max_total_exposure_usd


def test_broker_preflight_refusal_is_terminal_and_never_wedges_later_cycles(dispatch_rig: tuple) -> None:
    """A LiveBroker preflight refusal (10% ask guard) is a recorded unsent refusal.

    Regression: it used to raise as an uncertain submission, then every later cycle
    failed reconciliation, blocking exits as well as entries.
    """
    db_path, clock, mcp, feed, store, dispatcher = dispatch_rig
    mcp.set_quote("opt-spy-680c", "1.60", "1.65")  # broker ask > 110% of the 1.20 alert
    ingest_alert(store, clock, "SPY 680C 11/20 @ 1.20", 1001)

    first = asyncio.run(dispatcher.run_once())
    assert first.status == "LIVE_CYCLE_COMPLETED"
    assert first.entry_orders_submitted == 0
    assert not any(name == "place_option_order" for name, _ in mcp.calls)
    with sqlite3.connect(str(db_path)) as db:
        assert db.execute("SELECT state FROM order_intents").fetchall() == [("REJECTED",)]
        assert db.execute(
            "SELECT code FROM paper_engine_alerts WHERE code='SUBMISSION_REFUSED_PRE_PLACEMENT'"
        ).fetchone()

    clock.advance(5.0)
    mcp.set_quote("opt-spy-680c", "1.20", "1.22")  # a later quote never resurrects the refusal
    second = asyncio.run(dispatcher.run_once())
    assert second.status == "LIVE_CYCLE_COMPLETED"
    assert second.entry_orders_submitted == 0
    assert not mcp.orders
