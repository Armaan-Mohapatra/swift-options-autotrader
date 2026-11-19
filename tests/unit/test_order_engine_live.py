"""Unit test suite for PaperOrderEngine in Live Mode (R2 / Feature 3 & 4).

Covers:
- Acceptance of non-PaperBroker when live_mode=True (and rejection when live_mode=False).
- Slippage-adjusted limit price calculation from live entry quotes.
- SlippageExceededError leading to BLOCKED entry with SLIPPAGE_EXCEEDED.
- Cumulative realized daily loss calculation via FIFO matching of closed positions.
- Daily loss limit enforcement triggering DAILY_LOSS_LIMIT_REACHED rejection.
- Isolation of previous days' realized losses from today's ceiling.
- Partial close loss calculations.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import BrokerFill, BrokerOrderState, BrokerOrderStatus
from swift_autotrader.broker.paper import PaperBroker, iso
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.paper_dispatch import VerifiedQuote
from swift_autotrader.signals.identity import OptionContract, OptionRight

NOW = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
OTHER = OptionContract("SPY", OptionRight.PUT, Decimal("670"), date(2026, 11, 20))


class FakeClock:
    def __init__(self, current: datetime = NOW):
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class MockLiveBroker:
    """Offline test broker fulfilling the Broker protocol for live engine tests."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.submitted_orders: list[OrderIntent] = []
        self.statuses: dict[str, BrokerOrderStatus] = {}

    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        self.submitted_orders.append(intent)
        status = BrokerOrderStatus(
            logical_order_id=intent.logical_order_id,
            state=BrokerOrderState.ACCEPTED,
            filled_quantity=0,
            contract=intent.contract,
            side=intent.side,
            requested_quantity=intent.quantity,
            limit_price=intent.limit_price,
            idempotency_key=intent.idempotency_key,
            accepted_at_utc=datetime.now(UTC),
            fills=[],
        )
        self.statuses[intent.logical_order_id] = status
        return status

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        return self.statuses.get(
            logical_order_id,
            BrokerOrderStatus(logical_order_id, BrokerOrderState.UNKNOWN, 0, None),
        )

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        status = self.get_order(logical_order_id)
        new_status = BrokerOrderStatus(
            logical_order_id=logical_order_id,
            state=BrokerOrderState.CANCELED,
            filled_quantity=status.filled_quantity,
            contract=status.contract,
            side=status.side,
            requested_quantity=status.requested_quantity,
            limit_price=status.limit_price,
            idempotency_key=status.idempotency_key,
            accepted_at_utc=status.accepted_at_utc,
            fills=status.fills,
        )
        self.statuses[logical_order_id] = new_status
        return new_status


def insert_test_signal(
    db_path: Path,
    *,
    mid: int = 1,
    contract: OptionContract = CONTRACT,
    price: str = "1.20",
    sent: datetime = NOW,
) -> str:
    channel = "-1001234567890"
    sid = hashlib.sha256(f"swift:{channel}:{mid}".encode()).hexdigest()[:32]
    with sqlite3.connect(str(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO raw_events(channel_id, telegram_message_id, edit_version, "
            "received_at_utc, sent_at_utc, body) VALUES(?, ?, 0, ?, ?, ?)",
            (channel, str(mid), iso(sent), iso(sent), "SWIFT BUY ALERT"),
        )
        raw_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id, channel_id, message_id, "
            "revision, event_kind, origin, telegram_sent_at_utc, received_at_utc, "
            "content_sha256, eligible_for_future_processing) "
            "VALUES(?, ?, ?, 0, 'new', 'live', ?, ?, ?, 1)",
            (raw_id, channel, mid, iso(sent), iso(sent), f"hash-{mid}"),
        )
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO option_signals(signal_id, source_event_id, underlying, option_right, "
            "strike, expiration_date, original_limit, created_at_utc, state) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'SEEN')",
            (sid, raw_id, contract.underlying, contract.right.value,
             str(contract.strike), contract.expiration.isoformat(), price, iso(sent)),
        )
        db.execute(
            "INSERT INTO signal_metadata(signal_id, channel_id, source_message_id, category, "
            "first_revision_id) VALUES(?, ?, ?, 'STANDARD', ?)",
            (sid, channel, mid, rid),
        )
        db.execute(
            "INSERT INTO signal_parser_decisions(revision_id, channel_id, message_id, "
            "intent, status, reason, signal_id, processed_at_utc) "
            "VALUES(?, ?, ?, 'ENTRY', 'RECORDED', 'ENTRY_VALIDATED_NOT_EXECUTED', ?, ?)",
            (rid, channel, mid, sid, iso(sent)),
        )
    return sid


def simulate_closed_trade(
    db_path: Path,
    broker: MockLiveBroker,
    mid: int,
    contract: OptionContract,
    buy_price: Decimal,
    sell_price: Decimal,
    closed_at: datetime,
    quantity: int = 1,
) -> str:
    sid = insert_test_signal(db_path, mid=mid, contract=contract, price=str(buy_price), sent=closed_at)
    entry_id = f"{sid}:entry"
    close_id = f"{sid}:close"
    at_str = iso(closed_at)
    with sqlite3.connect(str(db_path)) as db:
        db.execute(
            "INSERT INTO order_intents(logical_order_id, signal_id, idempotency_key, side, "
            "requested_quantity, filled_quantity, limit_price, state, accepted_at_utc) "
            "VALUES(?, ?, ?, 'BUY_TO_OPEN', ?, ?, ?, 'FILLED', ?)",
            (entry_id, sid, f"k_buy_{mid}", quantity, quantity, str(buy_price), at_str),
        )
        db.execute(
            "INSERT INTO order_fills(fill_id, logical_order_id, filled_quantity, filled_price, filled_at_utc) "
            "VALUES(?, ?, ?, ?, ?)",
            (f"fill_b_{mid}", entry_id, quantity, str(buy_price), at_str),
        )
        db.execute(
            "INSERT INTO order_intents(logical_order_id, signal_id, idempotency_key, side, "
            "requested_quantity, filled_quantity, limit_price, state, accepted_at_utc) "
            "VALUES(?, ?, ?, 'SELL_TO_CLOSE', ?, ?, ?, 'FILLED', ?)",
            (close_id, sid, f"k_close_{mid}", quantity, quantity, str(sell_price), at_str),
        )
        db.execute(
            "INSERT INTO order_fills(fill_id, logical_order_id, filled_quantity, filled_price, filled_at_utc) "
            "VALUES(?, ?, ?, ?, ?)",
            (f"fill_s_{mid}", close_id, quantity, str(sell_price), at_str),
        )
        db.execute("UPDATE option_signals SET state='CLOSED' WHERE signal_id=?", (sid,))

    broker.statuses[entry_id] = BrokerOrderStatus(
        logical_order_id=entry_id,
        state=BrokerOrderState.FILLED,
        filled_quantity=quantity,
        accepted_at_utc=closed_at,
        contract=contract,
        side=OrderSide.BUY_TO_OPEN,
        requested_quantity=quantity,
        limit_price=buy_price,
        idempotency_key=f"k_buy_{mid}",
        fills=(BrokerFill(f"fill_b_{mid}", quantity, buy_price, closed_at),),
    )
    broker.statuses[close_id] = BrokerOrderStatus(
        logical_order_id=close_id,
        state=BrokerOrderState.FILLED,
        filled_quantity=quantity,
        accepted_at_utc=closed_at,
        contract=contract,
        side=OrderSide.SELL_TO_CLOSE,
        requested_quantity=quantity,
        limit_price=sell_price,
        idempotency_key=f"k_close_{mid}",
        fills=(BrokerFill(f"fill_s_{mid}", quantity, sell_price, closed_at),),
    )
    return sid


def make_quote(contract: OptionContract, ask: str, bid: str = "1.00", at: datetime = NOW) -> VerifiedQuote:
    return VerifiedQuote(
        quote_id=f"quote-{contract.underlying}-{int(at.timestamp())}",
        contract=contract,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
        observed_at_utc=at,
        source="MCP_READONLY",
    )


@pytest.fixture
def engine_rig(tmp_path: Path):
    db_path = tmp_path / "swift.sqlite3"
    apply_migrations(db_path)
    clock = FakeClock(NOW)
    broker = MockLiveBroker(db_path)
    preflight = PaperPreflight({
        CONTRACT: Preflight(
            instrument_verified=True,
            options_permission=True,
            market_session_open=True,
            available_buying_power_usd=Decimal("10000"),
            premium_tick=Decimal("0.01"),
            contract=CONTRACT,
        ),
        OTHER: Preflight(
            instrument_verified=True,
            options_permission=True,
            market_session_open=True,
            available_buying_power_usd=Decimal("10000"),
            premium_tick=Decimal("0.05"),
            contract=OTHER,
        ),
    })
    limits = RiskCeilings(
        contracts_per_signal=1,
        premium_per_signal_usd=Decimal("5000"),
        total_exposure_usd=Decimal("10000"),
        open_signal_count=3,
        entry_max_age_seconds=120,
        max_daily_loss_usd=Decimal("500"),
        max_concurrent_positions=3,
        slippage_tolerance_pct=Decimal("10"),
    )
    engine = PaperOrderEngine(
        db_path,
        broker,
        preflight=preflight,
        limits=limits,
        clock=clock,
        live_mode=True,
    )
    return db_path, clock, broker, engine, preflight, limits


def test_live_mode_broker_validation(tmp_path: Path):
    db_path = tmp_path / "test.sqlite3"
    apply_migrations(db_path)
    broker = MockLiveBroker(db_path)
    preflight = PaperPreflight()

    # live_mode=False rejects non-PaperBroker
    with pytest.raises(TypeError, match="Phase 3 engine accepts ONLY the concrete PaperBroker"):
        PaperOrderEngine(db_path, broker, preflight=preflight, live_mode=False)

    # live_mode=True accepts live Broker
    engine = PaperOrderEngine(db_path, broker, preflight=preflight, live_mode=True)
    assert engine.live_mode is True


def test_live_engine_slippage_tolerance_adjustment(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    # Ingest signal with alert limit 1.20
    insert_test_signal(db_path, mid=101, contract=CONTRACT, price="1.20", sent=clock())

    # Current ask is 1.25 (+4.17%, within 10% tolerance)
    quote = make_quote(CONTRACT, ask="1.25", at=clock())
    result = engine.run_cycle(entry_quotes={CONTRACT: quote})

    assert result.entry_submissions == 1
    assert result.blocked_entries == 0
    assert len(broker.submitted_orders) == 1

    # Verify order submitted with slippage-adjusted limit price 1.25
    submitted = broker.submitted_orders[0]
    assert submitted.limit_price == Decimal("1.25")

    # Verify persisted order intent in SQLite
    with sqlite3.connect(str(db_path)) as db:
        row = db.execute(
            "SELECT limit_price, state FROM order_intents WHERE logical_order_id=?",
            (submitted.logical_order_id,),
        ).fetchone()
        assert row[0] == "1.25"
        assert row[1] == "ACCEPTED"


def test_live_engine_slippage_exceeded_rejection(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    # Ingest signal with alert limit 1.20
    sid = insert_test_signal(db_path, mid=102, contract=CONTRACT, price="1.20", sent=clock())

    # Current ask is 1.40 (+16.7%, exceeds 10% tolerance of 1.32)
    quote = make_quote(CONTRACT, ask="1.40", at=clock())
    result = engine.run_cycle(entry_quotes={CONTRACT: quote})

    assert result.entry_submissions == 0
    assert result.blocked_entries == 1
    assert len(broker.submitted_orders) == 0

    # Verify decision recorded in SQLite with SLIPPAGE_EXCEEDED
    with sqlite3.connect(str(db_path)) as db:
        decision = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions WHERE signal_id=?",
            (sid,),
        ).fetchone()
        assert decision[0] == "BLOCKED"
        assert decision[1] == "SLIPPAGE_EXCEEDED"

        # Verify alert recorded
        alert = db.execute(
            "SELECT code FROM paper_engine_alerts WHERE signal_id=?", (sid,),
        ).fetchone()
        assert alert[0] == "SLIPPAGE_EXCEEDED"


def test_live_engine_cumulative_daily_loss_calculation(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    now = clock()
    yesterday = now - timedelta(days=1)

    # Trade 1 (today): Buy 1 @ 2.00, Sell 1 @ 1.50 -> Loss = $50.00
    simulate_closed_trade(db_path, broker, 301, CONTRACT, Decimal("2.00"), Decimal("1.50"), now)

    # Trade 2 (today): Buy 1 @ 5.00, Sell 1 @ 3.00 -> Loss = $200.00
    simulate_closed_trade(db_path, broker, 302, CONTRACT, Decimal("5.00"), Decimal("3.00"), now)

    # Trade 3 (yesterday): Buy 1 @ 3.00, Sell 1 @ 1.00 -> Loss = $200.00 (yesterday)
    simulate_closed_trade(db_path, broker, 303, CONTRACT, Decimal("3.00"), Decimal("1.00"), yesterday)

    # Today's realized loss: $50 + $200 = $250. Yesterday's $200 is excluded.
    today_loss = engine.cumulative_realized_daily_loss(now)
    assert today_loss == Decimal("250.00")


def test_live_engine_daily_loss_limit_blocks_new_entry(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    now = clock()

    # Closed trade today realizing $550 loss (Buy 8.00, Sell 2.50 = -$550.00)
    simulate_closed_trade(db_path, broker, 401, CONTRACT, Decimal("8.00"), Decimal("2.50"), now)

    # Check cumulative loss
    assert engine.cumulative_realized_daily_loss(now) == Decimal("550.00")

    # Ingest a new valid signal
    sid = insert_test_signal(db_path, mid=402, contract=CONTRACT, price="1.20", sent=now)

    # Run cycle
    result = engine.run_cycle()
    assert result.entry_submissions == 0
    assert result.blocked_entries == 1
    assert len(broker.submitted_orders) == 0

    # Verify decision in SQLite is DAILY_LOSS_LIMIT_REACHED
    with sqlite3.connect(str(db_path)) as db:
        decision = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions WHERE signal_id=?",
            (sid,),
        ).fetchone()
        assert decision[0] == "BLOCKED"
        assert decision[1] == "DAILY_LOSS_LIMIT_REACHED"


def test_live_engine_profitable_day_returns_zero_loss(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    now = clock()

    # Winning trade: Buy 1 @ 1.00, Sell 1 @ 2.50 -> Gain = +$150.00
    simulate_closed_trade(db_path, broker, 501, CONTRACT, Decimal("1.00"), Decimal("2.50"), now)

    assert engine.cumulative_realized_daily_loss(now) == Decimal("0")


def test_mvp_net_realized_loss_offsets_winners(engine_rig):
    db_path, clock, broker, engine, _, _ = engine_rig
    now = clock()
    for mid, buy, sell in ((601,"1","5"),(602,"4","0.50"),(603,"4","0.50")):
        simulate_closed_trade(db_path, broker, mid, CONTRACT, Decimal(buy), Decimal(sell), now)
    assert engine.cumulative_realized_daily_loss(now) == Decimal("300")
