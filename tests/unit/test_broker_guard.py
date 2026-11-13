"""Deny-only live adapter stays intact after Phase 3 paper implementation."""
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.broker.robinhood_mcp import (
    BrokerWritesDisabledError,
    RobinhoodMCPBroker,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight


def _synthetic_intent():
    return OrderIntent(
        "synthetic:entry", "synthetic-idempotency",
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13)),
        OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"),
    )


def test_real_broker_write_methods_fail_closed():
    adapter = RobinhoodMCPBroker()
    with pytest.raises(BrokerWritesDisabledError):
        adapter.submit_order(_synthetic_intent())
    with pytest.raises(BrokerWritesDisabledError):
        adapter.cancel_order("synthetic-order")


def test_paper_requires_explicit_migrated_local_db(tmp_path):
    with pytest.raises(RuntimeError, match="migrate"):
        PaperBroker(tmp_path / "not-created.sqlite3")


def test_paper_cannot_reach_live_broker(tmp_path):
    from swift_autotrader.database.migrate import apply_migrations
    path = tmp_path / "paper.sqlite3"
    apply_migrations(path)
    broker = PaperBroker(path, clock=lambda: datetime(2026, 11, 12, tzinfo=UTC))
    assert broker.submit_order(_synthetic_intent()).state.value == "ACCEPTED"
    assert broker.get_order("synthetic:entry").filled_quantity == 0
