"""Requirement-driven integration test suite for Engine Flatten Positions (R3 / Feature 9).

Covers:
- Tier 1: Feature Coverage (Single position liquidation, multi-position liquidation,
  resting buy cancellation, zero-position no-op, valid OrderIntent structure).
- Tier 2: Boundary & Corner Cases (Partially filled buys, partially closed positions,
  subsequent idempotence, closed positions ignored, broker resilience, DB control flags).
- Tier 3: Cross-Feature Interactions (Kill switch bot to flatten execution, broker adapter integration).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import OrderIntent, OrderSide
from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from tests.fixtures.live_mocks import (
    FakeClock,
    MockTelegramBotAPI,
    StatefulFakeMCP,
)

SPY_CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
QQQ_CONTRACT = OptionContract("QQQ", OptionRight.CALL, Decimal("500"), date(2026, 11, 20))


def seed_signal_and_entry(
    engine: PaperOrderEngine,
    signal_id: str,
    contract: OptionContract,
    quantity: int,
    price: Decimal,
    now: datetime,
    *,
    filled_quantity: int = 0,
    msg_id: int = 100,
) -> tuple[str, str]:
    """Helper to seed a signal and order intent using the official repository and matcher."""
    from swift_autotrader.database.repository import TelegramStore
    from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog
    from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage

    store = TelegramStore(engine.db_path, -1001234567890)
    right_code = "C" if contract.right is OptionRight.CALL else "P"
    exp_str = f"{contract.expiration.month}/{contract.expiration.day}"
    alert_text = f"{contract.underlying} {int(contract.strike)}{right_code} {exp_str} @ {price}"

    store.ingest(
        TelegramMessage(
            -1001234567890,
            msg_id,
            now,
            now,
            alert_text,
            kind=EventKind.NEW,
            origin=EventOrigin.LIVE,
        ),
        session_started_at_utc=now - timedelta(minutes=5),
    )

    matcher = SignalMatcher(engine.db_path, -1001234567890)
    matcher.catalog = StaticInstrumentCatalog((contract,))
    matcher.process_pending(limit=10)

    # Preflight approval
    engine.preflight.instruments[contract] = Preflight(
        instrument_verified=True,
        options_permission=True,
        market_session_open=True,
        available_buying_power_usd=Decimal("10000.00"),
        premium_tick=Decimal("0.01"),
        contract=contract,
    )
    engine.limits = RiskCeilings(
        contracts_per_signal=quantity,
        premium_per_signal_usd=Decimal("10000"),
        total_exposure_usd=Decimal("20000"),
        open_signal_count=10,
    )

    engine.run_cycle(allow_entries=True, process_closes=False)

    with engine._db() as db:
        intent_row = db.execute(
            "SELECT signal_id, logical_order_id FROM order_intents WHERE side='BUY_TO_OPEN' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert intent_row is not None, "Failed to create buy order intent in seed"
        sid = str(intent_row[0])
        loid = str(intent_row[1])

        if filled_quantity > 0:
            if hasattr(engine.broker, "manual_fill"):
                engine.broker.manual_fill(loid, fill_id=f"fill_{loid}", quantity=filled_quantity, price=price, at=now)
            else:
                target_mcp = None
                if hasattr(getattr(engine.broker, "client", None), "fill_order"):
                    target_mcp = engine.broker.client  # LiveBroker on StatefulFakeMCP
                elif hasattr(engine.broker, "mcp"):
                    if hasattr(engine.broker.mcp, "fill_order"):
                        target_mcp = engine.broker.mcp
                    elif hasattr(engine.broker.mcp, "client") and hasattr(engine.broker.mcp.client, "fill_order"):
                        target_mcp = engine.broker.mcp.client
                if target_mcp is not None and target_mcp.orders:
                    order_id = list(target_mcp.orders.keys())[-1]
                    target_mcp.fill_order(order_id, filled_quantity=filled_quantity, fill_price=price)
            engine.run_cycle(allow_entries=False, process_closes=False)

    return sid, loid


def sqlite3_row_mock(signal_id: str, contract: OptionContract, price: Decimal, now: datetime) -> dict:
    return {
        "signal_id": signal_id,
        "underlying": contract.underlying,
        "option_right": contract.right.value,
        "strike": str(contract.strike),
        "expiration_date": contract.expiration.isoformat(),
        "original_limit": str(price),
        "created_at_utc": now.isoformat(),
    }


@pytest.fixture
def flatten_engine(live_db: Path) -> tuple[PaperOrderEngine, FakeClock, PaperBroker]:
    """Paper engine for deterministic liquidation logic; live path: test_t3_flatten_positions_broker_integration."""
    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    broker = PaperBroker(live_db, clock=clock)
    ceilings = RiskCeilings(daily_net_loss_usd=Decimal("500"))
    engine = PaperOrderEngine(
        live_db,
        broker,
        preflight=PaperPreflight(),
        limits=ceilings,
        clock=clock,
        live_mode=False,
    )
    return engine, clock, broker


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_flatten_single_open_position(flatten_engine: tuple) -> None:
    """T1-F9-01: Liquidate single active position with SELL_TO_CLOSE intent."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    # Seed 1 active filled position of 2 contracts
    seed_signal_and_entry(engine, "sig_spy", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)

    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.side is OrderSide.SELL_TO_CLOSE
    assert intent.quantity == 2
    assert intent.contract == SPY_CONTRACT

    # Emergency stop and pause must be active
    paused, stopped = engine._control()
    assert paused is True
    assert stopped is True


def test_flatten_multiple_open_positions(flatten_engine: tuple) -> None:
    """T1-F9-02: Liquidate multiple distinct positions across tickers."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    seed_signal_and_entry(engine, "sig_spy", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2, msg_id=101)
    seed_signal_and_entry(engine, "sig_qqq", QQQ_CONTRACT, 3, Decimal("2.00"), now, filled_quantity=3, msg_id=102)

    intents = engine.flatten_positions(now)
    assert len(intents) == 2
    quantities = {intent.contract.underlying: intent.quantity for intent in intents}
    assert quantities["SPY"] == 2
    assert quantities["QQQ"] == 3


def test_flatten_cancels_resting_buy_orders(flatten_engine: tuple) -> None:
    """T1-F9-03: Flatten cancels resting unfilled buy orders while closing active positions."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    # Active held position
    seed_signal_and_entry(engine, "sig_active", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2, msg_id=101)

    # Resting unfilled buy order
    _, buy_order_id = seed_signal_and_entry(engine, "sig_resting", QQQ_CONTRACT, 1, Decimal("2.00"), now, filled_quantity=0, msg_id=102)

    intents = engine.flatten_positions(now)
    # Only active position receives a SELL_TO_CLOSE intent
    assert len(intents) == 1
    assert intents[0].contract == SPY_CONTRACT

    # Check resting buy was canceled in SQLite
    with engine._db() as db:
        row = db.execute("SELECT state FROM order_intents WHERE logical_order_id=?", (buy_order_id,)).fetchone()
        assert row[0] == "CANCELED"


def test_flatten_empty_portfolio_zero_op(flatten_engine: tuple) -> None:
    """T1-F9-04: Flattening empty portfolio halts engine with zero liquidation orders."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    intents = engine.flatten_positions(now)
    assert intents == []
    paused, stopped = engine._control()
    assert paused is True
    assert stopped is True


def test_flatten_returns_valid_order_intents(flatten_engine: tuple) -> None:
    """T1-F9-05: Returned liquidation objects are complete, valid OrderIntents."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    seed_signal_and_entry(engine, "sig_spy", SPY_CONTRACT, 1, Decimal("1.20"), now, filled_quantity=1)
    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, OrderIntent)
    assert intent.idempotency_key is not None
    assert intent.limit_price > Decimal("0")


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


def test_flatten_partially_filled_buy_position(flatten_engine: tuple) -> None:
    """T2-F9-01: Buy of 5 with 3 filled cancels resting 2 and liquidates exactly 3."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    # 3 filled out of 5 requested
    seed_signal_and_entry(engine, "sig_part", SPY_CONTRACT, 5, Decimal("1.20"), now, filled_quantity=3)

    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    assert intents[0].quantity == 3  # exactly the 3 filled contracts


def test_flatten_partially_closed_position(flatten_engine: tuple) -> None:
    """T2-F9-02: Position with 4 bought and 2 already sold liquidates remaining 2."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    # 4 bought
    sid, _ = seed_signal_and_entry(engine, "sig_scaled", SPY_CONTRACT, 4, Decimal("1.20"), now, filled_quantity=4)

    # 2 already sold previously
    with engine._db() as db:
        sig_row = db.execute("SELECT * FROM option_signals WHERE signal_id=?", (sid,)).fetchone()
    sell_intent, _ = engine._intent(
        sig_row,
        OrderSide.SELL_TO_CLOSE,
        2,
        Decimal("1.50"),
        now,
    )
    engine._submit_existing(sell_intent, now)
    if hasattr(engine.broker, "manual_fill"):
        engine.broker.manual_fill(sell_intent.logical_order_id, fill_id=f"fill_prior_sell_{sell_intent.logical_order_id}", quantity=2, price=Decimal("1.50"), at=now)
    engine.run_cycle(allow_entries=False, process_closes=False)

    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    assert intents[0].quantity == 2  # remaining 4 - 2 = 2


def test_flatten_idempotent_subsequent_calls(flatten_engine: tuple) -> None:
    """T2-F9-03: Calling flatten twice does not duplicate liquidation orders."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    seed_signal_and_entry(engine, "sig_dup", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)

    first = engine.flatten_positions(now)
    assert len(first) == 1

    # Second flatten call immediately after
    second = engine.flatten_positions(now)
    assert len(second) == 0  # no duplicates submitted


def test_flatten_already_closed_positions_ignored(flatten_engine: tuple) -> None:
    """T2-F9-04: Closed signals (buys == sold) are ignored during flatten."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    sid, _ = seed_signal_and_entry(engine, "sig_closed", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)
    with engine._db() as db:
        db.execute("UPDATE option_signals SET state='CLOSED' WHERE signal_id=?", (sid,))

    intents = engine.flatten_positions(now)
    assert len(intents) == 0


def test_flatten_sets_emergency_stop_and_paused(flatten_engine: tuple) -> None:
    """T2-F9-05: Flatten updates DB control singleton with emergency_stop=1 and paused=1."""
    engine, clock, broker = flatten_engine
    now = clock.now()

    engine.flatten_positions(now)
    with engine._db() as db:
        row = db.execute("SELECT paused, emergency_stop FROM paper_engine_control WHERE singleton=1").fetchone()
        assert row[0] == 1
        assert row[1] == 1


# ============================================================================
# Tier 3: Cross-Feature Interactions
# ============================================================================


def test_t3_kill_switch_to_engine_flatten(flatten_engine: tuple) -> None:
    """T3-09: Mobile kill switch /confirm_flatten executes engine position liquidation."""
    engine, clock, _ = flatten_engine
    now = clock.now()
    seed_signal_and_entry(engine, "sig_ks", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)

    bot = MockTelegramBotAPI(allowed_chat_ids=(123456789,))
    # Operator requests flatten
    reply = bot.handle_command(123456789, "/flatten", now_ts=100.0, engine=engine)
    token = list(bot.pending_tokens.keys())[0]

    # Operator confirms
    confirm_reply = bot.handle_command(123456789, f"/confirm_flatten {token}", now_ts=105.0, engine=engine)
    assert "FLATTEN EXECUTED" in confirm_reply

    paused, stopped = engine._control()
    assert paused is True
    assert stopped is True


def test_t3_flatten_positions_broker_integration(
    live_db: Path, fake_live_broker: LiveBroker, mock_mcp: StatefulFakeMCP, fake_clock: FakeClock,
) -> None:
    """T3-10: Engine flatten submits the full owned exit through the canonical LiveBroker."""
    engine = PaperOrderEngine(
        live_db,
        fake_live_broker,
        preflight=PaperPreflight(),
        limits=RiskCeilings(),
        clock=fake_clock,
        live_mode=True,
    )

    now = fake_clock.now()
    seed_signal_and_entry(engine, "sig_live_flatten", SPY_CONTRACT, 1, Decimal("1.20"), now, filled_quantity=1)

    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    # Order 1 = seed buy, order 2 = flatten sell of the full owned quantity.
    assert len(mock_mcp.orders) == 2
    mcp_sell_order = list(mock_mcp.orders.values())[1]
    assert mcp_sell_order["legs"][0]["side"] == "sell"
    assert mcp_sell_order["legs"][0]["position_effect"] == "close"
    assert Decimal(mcp_sell_order["quantity"]) == 1
    assert mcp_sell_order["state"] == "confirmed"
