"""Tier 4 Real-World Application Scenarios for Swift Options AutoTrader.

Covers end-to-end operational workflows simulating production trading sessions:
- Scenario 1: Normal Live Ingestion to Execution & Profitable Scale-Out (F1, F2, F3, F4, F5)
- Scenario 2: Runaway Market Gap-Up with Slippage Protection (F1, F3, F4, F5)
- Scenario 3: Daily Loss Limit Circuit Breaker Tripped (F1, F3, F5)
- Scenario 4: Mobile Emergency Remote Flatten via Kill Switch Bot (F1, F2, F7, F8, F9)
- Scenario 5: Cross-Platform Supervisor Crash Recovery During High Volatility (F5, F7)
- Scenario 6: Adversarial Unauthorized Attacker Kill Switch Rejection (F7, F8)
- Scenario 7: Concurrent Rapid Telegram Signals with Position Cap Saturation (F1, F3, F5)
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from swift_autotrader.analytics.metrics import PaperAnalytics
from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodMCPBroker,
)
from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings, rejection_reason
from swift_autotrader.paper_dispatch import MarketEvidenceUnavailable, MarketSnapshot, VerifiedQuote
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from tests.fixtures.live_mocks import (
    FakeClock,
    MockProcessManager,
    MockTelegramBotAPI,
    StatefulFakeMCP,
    live_journal,
)
from tests.integration.test_engine_flatten import seed_signal_and_entry

CHANNEL_ID = -1001234567890
SPY_CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 12, 18))
QQQ_CONTRACT = OptionContract("QQQ", OptionRight.CALL, Decimal("520"), date(2026, 12, 18))


class ScenarioQuoteFeed:
    """Configurable quote feed for scenario simulation supporting single or multiple contracts."""

    def __init__(
        self,
        clock: FakeClock,
        contract: OptionContract = SPY_CONTRACT,
        contracts: Mapping[OptionContract, tuple[Decimal, Decimal]] | None = None,
    ) -> None:
        self.clock = clock
        self.contract = contract
        self.contracts: dict[OptionContract, tuple[Decimal, Decimal]] = (
            dict(contracts) if contracts is not None else {contract: (Decimal("1.20"), Decimal("1.22"))}
        )
        self.down = False
        self.market_open = True
        self.bid = Decimal("1.20")
        self.ask = Decimal("1.22")
        self.bid_size = 10
        self.ask_size = 10
        self.stale_seconds = 0
        self.available_buying_power = Decimal("10000.00")

    def set_quote(self, contract: OptionContract, bid: Decimal, ask: Decimal) -> None:
        """Update quotes for a specific contract."""
        self.contracts[contract] = (bid, ask)
        if contract == self.contract:
            self.bid = bid
            self.ask = ask

    async def snapshot(self, entries: list, active: list, now: datetime) -> MarketSnapshot:
        if self.down:
            raise MarketEvidenceUnavailable("Broker market feed failure")

        observed_time = now - timedelta(seconds=self.stale_seconds)
        # Synchronize primary contract if mutated via feed.bid / feed.ask directly
        self.contracts[self.contract] = (self.bid, self.ask)

        quotes: dict[OptionContract, VerifiedQuote] = {}
        instruments: dict[OptionContract, Preflight] = {}
        for c, (bid, ask) in self.contracts.items():
            quotes[c] = VerifiedQuote(
                quote_id=f"quote-{c.underlying.lower()}-{int(now.timestamp())}",
                contract=c,
                bid=bid,
                ask=ask,
                bid_size=self.bid_size,
                ask_size=self.ask_size,
                observed_at_utc=observed_time,
                source="MCP_READONLY",
                evidence_reference="evidence_hash_e2e",
            )
            instruments[c] = Preflight(
                instrument_verified=True,
                options_permission=True,
                market_session_open=self.market_open,
                available_buying_power_usd=self.available_buying_power,
                premium_tick=Decimal("0.01"),
                contract=c,
            )
        return MarketSnapshot(
            instruments=instruments,
            quotes=quotes,
            source="MCP_READONLY",
            as_of_utc=observed_time,
        )


def ingest_scenario_message(
    store: TelegramStore,
    clock: FakeClock,
    text: str,
    msg_id: int,
    reply_to: int | None = None,
) -> None:
    """Helper to ingest realistic Telegram messages."""
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
# Tier 4: Real-World Application Scenarios
# ============================================================================


def test_scenario_1_normal_ingestion_execution_scaleout(tmp_path: Path) -> None:
    """Scenario 1: Normal Live Ingestion to Execution & Profitable Scale-Out.

    Exercises: F1 (Telegram Alerts), F2 (Real-time MCP Fills), F3 (Live Risk Ceilings),
               F4 (Intelligent Slippage Resolution), F5 (Live Market Dispatcher).
    Flow:
    1. Swift alert: 'SPY 680C 12/18 @ 1.20'
    2. NBBO Ask at $1.22; the entry limit stays the exact $1.20 alert premium
    3. LiveDispatcher places BUY_TO_OPEN via the canonical LiveBroker to StatefulFakeMCP
    4. MCP order filled at $1.20
    5. Exit alert arrives: 'SPY SOLD HALF'
    6. LiveDispatcher places SELL_TO_CLOSE at $1.80
    7. MCP close order filled at $1.80
    8. PaperAnalytics verifies realized gross profit = +$60.00, win rate = 100%.
    """
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="live-acc-sc1", buying_power=Decimal("10000.00"), clock=clock)
    mcp_broker = RobinhoodMCPBroker(mcp)
    account = MCPAccount("live-acc-sc1", True, "option_level_2")
    feed = ScenarioQuoteFeed(clock, contract=SPY_CONTRACT)
    store = TelegramStore(db_path, CHANNEL_ID)

    session_file = tmp_path / "secrets" / "test.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"session_bytes")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="live-acc-sc1",
        db_path=db_path,
        max_daily_loss_usd=Decimal("500"),
        live_journal_path=live_journal(db_path.parent / "live_journal.sqlite3"),
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

    # 1. Swift alert arrives
    ingest_scenario_message(store, clock, "SPY 680C 12/18 @ 1.20", msg_id=101)

    # 3. First dispatch cycle: Ingest alert and place BUY order
    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.parsed_events == 1
    assert result.entry_orders_submitted == 1
    assert len(mcp.orders) == 1

    buy_order_id = list(mcp.orders.keys())[0]
    buy_order = mcp.orders[buy_order_id]
    assert buy_order["direction"] == "buy"
    assert buy_order["state"] == "confirmed"

    # 4. Fill buy order on MCP
    mcp.fill_order(buy_order_id, filled_quantity=1, fill_price=Decimal("1.20"))
    clock.advance(seconds=15)

    # Reconcile buy fill
    asyncio.run(dispatcher.run_once())
    with dispatcher.engine._db() as db:
        sig = db.execute("SELECT * FROM option_signals").fetchone()
        assert sig is not None
        assert sig["state"] == "OPEN"
        buys, sells = dispatcher.engine._summary(str(sig["signal_id"]))
        assert buys == 1
        assert sells == 0

    # 5. Exit alert arrives: 'SPY SOLD HALF'
    feed.bid = Decimal("1.80")
    feed.ask = Decimal("1.82")
    ingest_scenario_message(store, clock, "SPY SOLD HALF", msg_id=102, reply_to=101)
    clock.advance(seconds=15)

    # 6. Second dispatch cycle: Process exit and submit SELL_TO_CLOSE
    result_exit = asyncio.run(dispatcher.run_once())
    assert result_exit.close_orders_submitted == 1
    assert len(mcp.orders) == 2

    sell_order_id = list(mcp.orders.keys())[1]
    sell_order = mcp.orders[sell_order_id]
    assert sell_order["direction"] == "sell"
    assert Decimal(sell_order["quantity"]) == 1

    # 7. Fill sell order on MCP
    mcp.fill_order(sell_order_id, filled_quantity=1, fill_price=Decimal("1.80"))
    clock.advance(seconds=15)

    # Final reconciliation cycle
    asyncio.run(dispatcher.run_once())

    with dispatcher.engine._db() as db:
        sig_final = db.execute("SELECT * FROM option_signals").fetchone()
        assert sig_final["state"] == "CLOSED"
        buys, sells = dispatcher.engine._summary(str(sig_final["signal_id"]))
        assert buys == 1
        assert sells == 1

    # 8. Realized P&L verified by PaperAnalytics
    analytics = PaperAnalytics(db_path)
    report = analytics.snapshot()
    # 1 contract * 100 multiplier * ($1.80 - $1.20) = +$60.00
    assert report["realized_gross_usd"] == "60.00"
    assert report["completed_positions"] == 1
    assert report["winning_completed_positions"] == 1


def test_scenario_2_runaway_market_gap_up_slippage_protection(tmp_path: Path) -> None:
    """Scenario 2: Runaway Market Gap-Up with Slippage Protection.

    Exercises: F1 (Telegram Alerts), F3 (Live Risk Ceilings),
               F4 (Intelligent Slippage Resolution), F5 (Live Market Dispatcher).
    Flow:
    1. Rapid momentum alert arrives: 'QQQ 520C 12/18 @ 2.00'
    2. Market ask spikes to $2.35 (+17.5% > 10% tolerance ceiling $2.20)
    3. LiveBroker's 10% ask guard refuses the entry in preflight (the limit is never raised).
    4. Zero orders placed on MCP; the exact-limit intent is a recorded unsent refusal.
    5. A later normal quote never resurrects or re-prices the refused entry.
    """
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="live-acc-sc2", buying_power=Decimal("10000.00"), clock=clock)
    mcp.add_instrument(
        instrument_id="opt-qqq-520c",
        chain_id="synthetic-qqq-chain",
        symbol="QQQ",
        strike="520.0000",
        expiration="2026-12-18",
        option_type="call",
        bid="2.30",
        ask="2.35",
    )
    mcp_broker = RobinhoodMCPBroker(mcp)
    account = MCPAccount("live-acc-sc2", True, "option_level_2")
    feed = ScenarioQuoteFeed(clock, contract=QQQ_CONTRACT)
    feed.bid = Decimal("2.30")
    feed.ask = Decimal("2.35")  # Spiked ask > 10% of 2.00 ($2.20 max)

    store = TelegramStore(db_path, CHANNEL_ID)
    session_file = tmp_path / "secrets" / "test.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"session_bytes")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="live-acc-sc2",
        db_path=db_path,
        max_daily_loss_usd=Decimal("500"),
        live_journal_path=live_journal(db_path.parent / "live_journal.sqlite3"),
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

    # 1. Ingest Telegram message
    ingest_scenario_message(store, clock, "QQQ 520C 12/18 @ 2.00", msg_id=201)

    # 2. The gap-up never raises the limit, and a >10% ask is a REJECTION guard at LiveBroker:
    #    nothing is placed at Robinhood.
    result = asyncio.run(dispatcher.run_once())
    assert result.status == "LIVE_CYCLE_COMPLETED"
    assert result.parsed_events == 1
    assert result.entry_orders_submitted == 0
    assert result.close_orders_submitted == 0
    assert len(mcp.orders) == 0
    assert not any(name == "place_option_order" for name, _ in mcp.calls)

    # 3. Durable audit: the exact alert limit was recorded, refused unsent, never chased.
    import sqlite3
    with sqlite3.connect(str(db_path)) as db:
        assert db.execute("SELECT limit_price, state FROM order_intents").fetchall() == [
            ("2.00", "REJECTED")]
        assert {r[0] for r in db.execute("SELECT code FROM paper_engine_alerts")} == {
            "SUBMISSION_REFUSED_PRE_PLACEMENT"}

    # 4. A later cycle with a normal quote never resurrects or re-prices the refused entry.
    mcp.set_quote("opt-qqq-520c", "1.98", "2.02")
    clock.advance(5)
    assert asyncio.run(dispatcher.run_once()).entry_orders_submitted == 0
    assert len(mcp.orders) == 0


def test_scenario_3_live_daily_loss_is_not_an_entry_gate(tmp_path: Path) -> None:
    """LIVE permits entries after losses; independent PAPER risk math stays covered.

    Exercises: F1 (Telegram Alerts), F3 (Live Risk Ceilings), F5 (Live Market Dispatcher).
    Flow:
    1. Session starts with max daily loss ceiling of $500
    2. Trade 1 executes and closes with a realized loss of $520 ($8.00 entry -> $2.80 exit)
    3. Reconciled realized loss breaches ceiling ($520 > $500 max_daily_loss_usd)
    4. New incoming entry alert arrives ('SPY 680C 12/18 @ 1.20')
    5. LiveDispatcher.run_once() does not use daily P&L as an entry prerequisite.
    6. Entry may submit when all non-retired safety checks pass.
    7. Standalone mathematical boundary checks verify under-ceiling, exact, and breached thresholds.
    """
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="live-acc-sc3", buying_power=Decimal("10000.00"), clock=clock)
    mcp_broker = RobinhoodMCPBroker(mcp)
    account = MCPAccount("live-acc-sc3", True, "option_level_2")
    feed = ScenarioQuoteFeed(clock, contract=SPY_CONTRACT)
    store = TelegramStore(db_path, CHANNEL_ID)
    session_file = tmp_path / "secrets" / "test.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"session_bytes")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="live-acc-sc3",
        db_path=db_path,
        max_daily_loss_usd=Decimal("500"),
        live_journal_path=live_journal(db_path.parent / "live_journal.sqlite3"),
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

    # 1. Trade 1: Entry @ $8.00
    feed.bid = Decimal("7.95")
    feed.ask = Decimal("8.00")
    ingest_scenario_message(store, clock, "SPY 680C 12/18 @ 8.00", msg_id=301)
    res_entry = asyncio.run(dispatcher.run_once())
    assert res_entry.entry_orders_submitted == 1
    assert len(mcp.orders) == 1
    buy_order_id = list(mcp.orders.keys())[0]
    mcp.fill_order(buy_order_id, filled_quantity=1, fill_price=Decimal("8.00"))

    # 2. Trade 1: Exit alert arrives, market drops to $2.80 -> Loss = (2.80 - 8.00) * 100 = -$520
    clock.advance(seconds=15)
    feed.bid = Decimal("2.80")
    feed.ask = Decimal("2.85")
    ingest_scenario_message(store, clock, "SPY SOLD ALL", msg_id=302, reply_to=301)
    res_exit = asyncio.run(dispatcher.run_once())
    assert res_exit.close_orders_submitted == 1
    assert len(mcp.orders) == 2
    sell_order_id = list(mcp.orders.keys())[1]
    mcp.fill_order(sell_order_id, filled_quantity=1, fill_price=Decimal("2.80"))

    # 3. Reconcile exit fill and verify cumulative realized daily loss
    clock.advance(seconds=15)
    res_rec = asyncio.run(dispatcher.run_once())
    assert res_rec.realized_daily_loss_usd == Decimal("520.00")

    # 4. Subsequent entry alert arrives
    clock.advance(seconds=15)
    feed.bid = Decimal("1.20")
    feed.ask = Decimal("1.22")
    ingest_scenario_message(store, clock, "SPY 680C 12/18 @ 1.20", msg_id=303)

    # 5. Daily P&L does not veto the intent; an independently stale quote can refuse send.
    res_allowed = asyncio.run(dispatcher.run_once())
    assert res_allowed.status == "LIVE_CYCLE_COMPLETED"
    assert res_allowed.entry_orders_submitted == 0
    assert len(mcp.orders) == 2

    # 6. Verify durable decision and alert in SQLite
    import sqlite3
    with sqlite3.connect(str(db_path)) as db:
        decisions = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions ORDER BY decided_at_utc DESC"
        ).fetchall()
        assert decisions[0] == ("INTENT_RECORDED", None)
        assert db.execute(
            "SELECT 1 FROM paper_engine_alerts WHERE code='DAILY_LOSS_CEILING_REACHED'"
        ).fetchone() is None

        alerts = db.execute(
            "SELECT code FROM paper_engine_alerts WHERE code='DAILY_LOSS_CEILING_REACHED'"
        ).fetchall()
        assert not alerts

    # 7. Local notification scan
    dispatcher.notifications.scan()
    pending = dispatcher.notifications.pending(10)
    assert not any(n.code == "DAILY_LOSS_CEILING_REACHED" for n in pending)

    # 8. Boundary unit checks preserved
    ceilings = RiskCeilings(
        contracts_per_signal=1,
        premium_per_signal_usd=Decimal("500"),
        daily_net_loss_usd=Decimal("500"),
    )
    preflight_ok = Preflight(
        instrument_verified=True,
        options_permission=True,
        market_session_open=True,
        available_buying_power_usd=Decimal("5000.00"),
        premium_tick=Decimal("0.01"),
        contract=SPY_CONTRACT,
    )
    # Under ceiling ($200 loss < $500 ceiling): allowed
    assert rejection_reason(
        SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight_ok,
        outstanding_usd=Decimal("0"), active_signals=0,
        realized_net_pnl_today_usd=Decimal("-200.00"),
    ) is None
    # Breached ceiling ($520 loss > $500 ceiling): tripped
    assert rejection_reason(
        SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight_ok,
        outstanding_usd=Decimal("0"), active_signals=0,
        realized_net_pnl_today_usd=Decimal("-520.00"),
    ) == "DAILY_LOSS_CEILING_REACHED"
    # Exact boundary ($500 loss == $500 ceiling): tripped
    assert rejection_reason(
        SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight_ok,
        outstanding_usd=Decimal("0"), active_signals=0,
        realized_net_pnl_today_usd=Decimal("-500.00"),
    ) == "DAILY_LOSS_CEILING_REACHED"


def test_scenario_4_mobile_emergency_remote_flatten(tmp_path: Path) -> None:
    """Scenario 4: Mobile Emergency Remote Flatten via Kill Switch Bot.

    Exercises: F1, F2, F7, F8, F9.
    Flow:
    1. Active positions held in engine
    2. Operator issues /flatten command from authorized Telegram chat
    3. Bot challenges with 2-step token
    4. Operator replies /confirm_flatten <token>
    5. Bot executes emergency stop and liquidation:
       SELL_TO_CLOSE orders placed on MCP, bot confirms completion.
    """
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="live-acc-sc4", buying_power=Decimal("10000.00"), clock=clock)
    live_broker = LiveBroker(live_journal(db_path.parent / "live_journal.sqlite3"), mcp,
                             account_number="live-acc-sc4", writes_enabled=True,
                             kill_switch=lambda: False, signal_gate=lambda _signal: "ENTRY",
                             clock=clock)

    engine = PaperOrderEngine(
        db_path,
        live_broker,
        preflight=PaperPreflight(),
        limits=RiskCeilings(),
        clock=clock,
        live_mode=True,
    )

    now = clock.now()

    # Seed position: one SPY contract (one per original entry) bought and filled
    seed_signal_and_entry(engine, "sig_held", SPY_CONTRACT, 1, Decimal("1.20"), now, filled_quantity=1)

    # Setup Kill Switch
    bot = MockTelegramBotAPI(
        bot_token="test_token_sc4",
        allowed_chat_ids=(123456789,),
        token_ttl_seconds=60.0,
    )

    # Step 1: Operator issues /flatten
    reply = bot.handle_command(123456789, "/flatten", engine=engine)
    assert "CONFIRM FLATTEN" in reply
    assert len(bot.pending_tokens) == 1
    token = list(bot.pending_tokens.keys())[0]

    # Step 2: Operator confirms with token
    confirm_reply = bot.handle_command(123456789, f"/confirm_flatten {token}", engine=engine)
    assert "FLATTEN EXECUTED" in confirm_reply

    # Verify liquidation SELL order was submitted to broker/MCP (order 1 is seed buy, order 2 is flatten sell)
    assert len(mcp.orders) == 2
    sell_order = list(mcp.orders.values())[1]
    assert sell_order["legs"][0]["side"] == "sell"
    assert sell_order["legs"][0]["position_effect"] == "close"
    assert Decimal(sell_order["quantity"]) == 1

    # Verify engine state is paused / emergency stopped
    paused, emergency_stop = engine._control()
    assert paused is True
    assert emergency_stop is True


def test_scenario_5_supervisor_crash_recovery_high_volatility(tmp_path: Path) -> None:
    """Scenario 5: Supervisor Crash Recovery During High Volatility.

    Exercises: F5, F7.
    Flow:
    1. Supervisor launches and manages 4 worker processes
    2. Writes HEALTHY status to health.json
    3. Simulates sudden unhandled crash of live-dispatch worker
    4. Supervisor detects dead worker on heartbeat poll
    5. Marks DEGRADED, applies backoff, and respawns live-dispatch worker
    6. System recovers to HEALTHY status with 0 leaked processes.
    """
    pm = MockProcessManager(tmp_path)

    # 1. Launch 4 supervised workers
    workers = ["telegram-ingest", "live-dispatch", "kill-switch", "health-monitor"]
    for w in workers:
        pm.spawn(w, ["python3", "-m", f"swift_autotrader.{w}"])

    assert len(pm.workers) == 4
    for w in workers:
        assert pm.workers[w].poll() is None

    # 2. Write initial healthy state
    pm.write_health_heartbeat(state="HEALTHY")
    health_file = tmp_path / "state" / "supervisor-health.json"
    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["status"] == "HEALTHY"
    assert all(data["workers"].values())

    # 3. Sudden crash of live-dispatch worker
    pm.workers["live-dispatch"].crash(exit_code=1)
    assert pm.workers["live-dispatch"].poll() == 1

    # 4. Supervisor detection and degradation
    pm.write_health_heartbeat(state="DEGRADED")
    data_degraded = json.loads(health_file.read_text(encoding="utf-8"))
    assert data_degraded["status"] == "DEGRADED"
    assert data_degraded["workers"]["live-dispatch"] is False

    # 5. Restart remains pending until the configured backoff expires.
    assert pm.check_workers(now_ts=1000.0) == []
    assert pm.workers["live-dispatch"].poll() == 1
    data_waiting = json.loads(health_file.read_text(encoding="utf-8"))
    assert data_waiting["workers"]["live-dispatch"] is False
    restarted = pm.check_workers(now_ts=1002.0)
    assert "live-dispatch" in restarted
    assert pm.workers["live-dispatch"].poll() is None

    # 6. Restored to HEALTHY
    pm.write_health_heartbeat(state="HEALTHY")
    data_recovered = json.loads(health_file.read_text(encoding="utf-8"))
    assert data_recovered["status"] == "HEALTHY"
    assert all(data_recovered["workers"].values())

    # Cleanup
    pm.stop_all()


def test_scenario_6_unauthorized_attacker_killswitch_rejection(tmp_path: Path) -> None:
    """Scenario 6: Adversarial Unauthorized Attacker Kill Switch Rejection.

    Exercises: F7, F8.
    Verifies that commands from unauthorized chat IDs or senders are rejected
    with UNAUTHORIZED without executing any action or generating tokens.
    """
    bot = MockTelegramBotAPI(
        bot_token="test_token_sc6",
        allowed_chat_ids=(123456789,),  # Only this ID is authorized
    )

    # Attacker tries /flatten from unauthorized chat ID 888888
    reply = bot.handle_command(888888, "/flatten")
    assert "UNAUTHORIZED" in reply
    assert len(bot.pending_tokens) == 0


def test_scenario_7_concurrent_rapid_signals_have_no_trade_count_cap(tmp_path: Path) -> None:
    """Scenario 7: Concurrent rapid Telegram signals; no trade-count cap (owner contract).

    Exercises: F1 (Telegram Alerts), F3 (Live Risk Ceilings), F5 (Live Market Dispatcher).
    Flow:
    1. Owner contract: no trade-count or aggregate cap (buying power is the only bound)
    2. Multi-contract quote feed provides verified quotes for SPY, AAPL, NVDA, and QQQ
    3. Three rapid concurrent signals arrive before dispatch cycle runs:
       - Alert 1: SPY 680C 12/18 @ 1.20
       - Alert 2: AAPL 250C 12/18 @ 1.50
       - Alert 3: NVDA 140C 12/18 @ 3.00
    4. LiveDispatcher.run_once() submits one contract for each of the 3 alerts.
    5. Positions fill; a 4th alert ('QQQ 500C 12/18 @ 2.00') also enters.
    6. Standalone checks: a VOLUNTARY operator open-signal cap is still honoured.
    """
    db_path = tmp_path / "data" / "swift.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_path)

    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    mcp = StatefulFakeMCP(account_number="live-acc-sc7", buying_power=Decimal("10000.00"), clock=clock)
    mcp_broker = RobinhoodMCPBroker(mcp)
    account = MCPAccount("live-acc-sc7", True, "option_level_2")

    aapl_contract = OptionContract("AAPL", OptionRight.CALL, Decimal("250"), date(2026, 12, 18))
    nvda_contract = OptionContract("NVDA", OptionRight.CALL, Decimal("140"), date(2026, 12, 18))
    qqq_contract_500 = OptionContract("QQQ", OptionRight.CALL, Decimal("500"), date(2026, 12, 18))

    feed = ScenarioQuoteFeed(
        clock,
        contracts={
            SPY_CONTRACT: (Decimal("1.20"), Decimal("1.22")),
            aapl_contract: (Decimal("1.50"), Decimal("1.52")),
            nvda_contract: (Decimal("3.00"), Decimal("3.05")),
            qqq_contract_500: (Decimal("2.00"), Decimal("2.05")),
        },
    )

    store = TelegramStore(db_path, CHANNEL_ID)
    session_file = tmp_path / "secrets" / "test.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"session_bytes")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="live-acc-sc7",
        db_path=db_path,
        max_daily_loss_usd=Decimal("500"),
        live_journal_path=live_journal(db_path.parent / "live_journal.sqlite3"),
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

    # 1. Rapid concurrent burst: 3 alerts arrive in rapid succession
    ingest_scenario_message(store, clock, "SPY 680C 12/18 @ 1.20", msg_id=701)
    clock.advance(seconds=1)
    ingest_scenario_message(store, clock, "AAPL 250C 12/18 @ 1.50", msg_id=702)
    clock.advance(seconds=1)
    ingest_scenario_message(store, clock, "NVDA 140C 12/18 @ 3.00", msg_id=703)

    # 2. First dispatch cycle: owner contract has NO trade-count cap, so every original
    #    ENTRY submits exactly one contract; only buying power bounds exposure.
    res = asyncio.run(dispatcher.run_once())
    assert res.status == "LIVE_CYCLE_COMPLETED"
    assert res.parsed_events == 3
    assert res.entry_orders_submitted == 3
    assert len(mcp.orders) == 3
    assert all(Decimal(o["quantity"]) == 1 for o in mcp.orders.values())

    import sqlite3
    with sqlite3.connect(str(db_path)) as db:
        decisions = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions ORDER BY decided_at_utc ASC"
        ).fetchall()
        assert decisions == [("INTENT_RECORDED", None)] * 3

    # 3. Fill the active orders on MCP
    order_ids = list(mcp.orders.keys())
    for oid, price in zip(order_ids, ("1.20", "1.50", "3.00"), strict=True):
        mcp.fill_order(oid, 1, Decimal(price))
    clock.advance(seconds=15)
    asyncio.run(dispatcher.run_once())

    # 4. Across-cycle: a 4th alert while three positions are open still enters.
    ingest_scenario_message(store, clock, "QQQ 500C 12/18 @ 2.00", msg_id=704)
    clock.advance(seconds=5)
    res4 = asyncio.run(dispatcher.run_once())
    assert res4.entry_orders_submitted == 1
    assert len(mcp.orders) == 4

    # 5. Boundary unit checks preserved
    ceilings = RiskCeilings(
        contracts_per_signal=1,
        premium_per_signal_usd=Decimal("500"),
        daily_net_loss_usd=Decimal("500"),
        open_signal_count=2,
    )
    preflight = Preflight(
        instrument_verified=True,
        options_permission=True,
        market_session_open=True,
        available_buying_power_usd=Decimal("10000.00"),
        premium_tick=Decimal("0.01"),
        contract=SPY_CONTRACT,
    )
    assert rejection_reason(SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight, outstanding_usd=Decimal("0"), active_signals=0) is None
    assert rejection_reason(SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight, outstanding_usd=Decimal("120"), active_signals=1) is None
    assert rejection_reason(SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight, outstanding_usd=Decimal("240"), active_signals=2) == "OPEN_SIGNAL_CAP_EXCEEDED"
    assert rejection_reason(SPY_CONTRACT, Decimal("1.20"), 1, ceilings, preflight, outstanding_usd=Decimal("360"), active_signals=3) == "OPEN_SIGNAL_CAP_EXCEEDED"
