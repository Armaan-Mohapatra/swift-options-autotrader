"""Unit test suite for PaperOrderEngine.flatten_positions and cancel_pending_buys.

Covers:
- Emergency stop and paused flags engagement.
- Resting BUY_TO_OPEN order cancellation.
- SELL_TO_CLOSE liquidation OrderIntent generation for held positions.
- Multi-position portfolio liquidation.
- Zero-position empty portfolio no-op.
- cancel_pending_buys isolated buy cancellation.
- Subsequent call idempotence (no duplicate liquidation orders).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import OrderIntent, OrderSide
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from tests.fixtures.live_mocks import FakeClock

SPY_CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
QQQ_CONTRACT = OptionContract("QQQ", OptionRight.CALL, Decimal("500"), date(2026, 11, 20))


@pytest.fixture
def unit_engine(live_db: Path) -> tuple[PaperOrderEngine, FakeClock, PaperBroker]:
    """Provide a isolated PaperOrderEngine in live_mode with PaperBroker and FakeClock."""
    clock = FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))
    broker = PaperBroker(live_db, clock=clock)
    ceilings = RiskCeilings(max_daily_loss_usd=Decimal("1000"), max_concurrent_positions=5)
    engine = PaperOrderEngine(
        live_db,
        broker,
        preflight=PaperPreflight(),
        limits=ceilings,
        clock=clock,
        live_mode=True,
    )
    return engine, clock, broker


def _seed_position(
    engine: PaperOrderEngine,
    signal_id: str,
    contract: OptionContract,
    quantity: int,
    price: Decimal,
    now: datetime,
    *,
    filled_quantity: int = 0,
) -> tuple[str, str]:
    """Helper to insert signal and entry order intent directly for unit testing."""
    order_id = f"{signal_id}:entry"
    idempotency_key = f"key_{signal_id}"

    with engine._db() as db:
        db.execute(
            "INSERT INTO option_signals("
            "signal_id, underlying, option_right, strike, expiration_date, original_limit, created_at_utc, state"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, 'OPEN')",
            (
                signal_id,
                contract.underlying,
                contract.right.value,
                str(contract.strike),
                contract.expiration.isoformat(),
                str(price),
                now.isoformat(),
            ),
        )
        sig_row = db.execute("SELECT * FROM option_signals WHERE signal_id=?", (signal_id,)).fetchone()

    intent, _ = engine._intent(sig_row, OrderSide.BUY_TO_OPEN, quantity, price, now)
    engine._submit_existing(intent, now)

    if filled_quantity > 0:
        engine.broker.manual_fill(
            intent.logical_order_id,
            fill_id=f"fill_{intent.logical_order_id}",
            quantity=filled_quantity,
            price=price,
            at=now,
        )
        engine.run_cycle(allow_entries=False, process_closes=False)

    return signal_id, intent.logical_order_id


def test_flatten_sets_emergency_and_paused_flags(unit_engine: tuple) -> None:
    """flatten_positions engages emergency_stop and paused flags in paper_engine_control."""
    engine, clock, _ = unit_engine
    now = clock.now()

    engine.flatten_positions(now)

    paused, stopped = engine._control()
    assert paused is True
    assert stopped is True


def test_flatten_cancels_resting_buy_orders(unit_engine: tuple) -> None:
    """flatten_positions cancels resting BUY_TO_OPEN orders."""
    engine, clock, _ = unit_engine
    now = clock.now()

    # Create an unfilled resting buy
    sid, loid = _seed_position(engine, "sig_resting", SPY_CONTRACT, 2, Decimal("1.50"), now, filled_quantity=0)

    # Flatten portfolio
    intents = engine.flatten_positions(now)
    assert len(intents) == 0  # No positions held, so no SELL_TO_CLOSE intents

    with engine._db() as db:
        row = db.execute("SELECT state FROM order_intents WHERE logical_order_id=?", (loid,)).fetchone()
        assert row[0] == "CANCELED"


def test_flatten_produces_sell_to_close_intents_for_held_positions(unit_engine: tuple) -> None:
    """flatten_positions produces SELL_TO_CLOSE OrderIntent objects for all held positions."""
    engine, clock, _ = unit_engine
    now = clock.now()

    # Create a filled position of 3 contracts
    _seed_position(engine, "sig_held", SPY_CONTRACT, 3, Decimal("2.10"), now, filled_quantity=3)

    intents = engine.flatten_positions(now)
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, OrderIntent)
    assert intent.side is OrderSide.SELL_TO_CLOSE
    assert intent.quantity == 3
    assert intent.contract == SPY_CONTRACT
    assert intent.limit_price == Decimal("2.10")


def test_flatten_multiple_positions_all_liquidated(unit_engine: tuple) -> None:
    """flatten_positions liquidates multiple distinct positions across tickers."""
    engine, clock, _ = unit_engine
    now = clock.now()

    _seed_position(engine, "sig_spy", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)
    _seed_position(engine, "sig_qqq", QQQ_CONTRACT, 4, Decimal("3.00"), now, filled_quantity=4)

    intents = engine.flatten_positions(now)
    assert len(intents) == 2
    by_underlying = {i.contract.underlying: i for i in intents}
    assert "SPY" in by_underlying
    assert "QQQ" in by_underlying
    assert by_underlying["SPY"].quantity == 2
    assert by_underlying["QQQ"].quantity == 4


def test_flatten_empty_positions_returns_empty_list(unit_engine: tuple) -> None:
    """flatten_positions on an empty portfolio returns empty list with controls engaged."""
    engine, clock, _ = unit_engine
    now = clock.now()

    intents = engine.flatten_positions(now)
    assert intents == []
    paused, stopped = engine._control()
    assert paused is True
    assert stopped is True


def test_cancel_pending_buys_unit(unit_engine: tuple) -> None:
    """cancel_pending_buys cancels resting buys without liquidating held positions or stopping engine."""
    engine, clock, _ = unit_engine
    now = clock.now()

    # Seed 1 resting buy and 1 held position
    _, resting_loid = _seed_position(engine, "sig_rest", SPY_CONTRACT, 2, Decimal("1.00"), now, filled_quantity=0)
    _, held_loid = _seed_position(engine, "sig_held", QQQ_CONTRACT, 3, Decimal("2.00"), now, filled_quantity=3)

    cancels = engine.cancel_pending_buys(now)
    assert cancels == 1

    # Check resting buy is canceled
    with engine._db() as db:
        resting_row = db.execute("SELECT state FROM order_intents WHERE logical_order_id=?", (resting_loid,)).fetchone()
        assert resting_row[0] == "CANCELED"

        held_row = db.execute("SELECT state FROM order_intents WHERE logical_order_id=?", (held_loid,)).fetchone()
        assert held_row[0] == "FILLED"

    # Engine control is still unstopped
    paused, stopped = engine._control()
    assert stopped is False


def test_flatten_idempotence(unit_engine: tuple) -> None:
    """Consecutive flatten_positions calls do not create duplicate liquidation orders."""
    engine, clock, _ = unit_engine
    now = clock.now()

    _seed_position(engine, "sig_repeat", SPY_CONTRACT, 2, Decimal("1.20"), now, filled_quantity=2)

    first = engine.flatten_positions(now)
    assert len(first) == 1

    second = engine.flatten_positions(now)
    assert len(second) == 0
