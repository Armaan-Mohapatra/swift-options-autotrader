"""Unit tests for Robinhood MCP response normalization, reconciliation, and durable order linkage."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import (
    BrokerOrderState,
)
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    MCPContractError,
    MCPInstrument,
    RobinhoodLiveBroker,
    RobinhoodMCPBroker,
)
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight

CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
ACCOUNT = MCPAccount("test-live-acc", True, "option_level_2")
INSTRUMENT = MCPInstrument("test-option-id-123", CONTRACT, True, "test-chain-id")


def make_intent(
    logical_id: str = "order-log-1",
    side: OrderSide = OrderSide.BUY_TO_OPEN,
    qty: int = 1,
    price: str = "1.25",
) -> OrderIntent:
    return OrderIntent(
        logical_order_id=logical_id,
        idempotency_key=f"idemp-{logical_id}",
        contract=CONTRACT,
        side=side,
        quantity=qty,
        limit_price=Decimal(price),
    )


def get_expected_ref(intent: OrderIntent) -> str:
    return RobinhoodMCPBroker.future_order_payload(ACCOUNT, INSTRUMENT, intent)["ref_id"]


class MockMCPClient:
    def __init__(self, orders=None):
        self.calls = []
        self.orders = orders or []

    async def list_tools(self):
        from swift_autotrader.broker.robinhood_mcp import READ_TOOLS, WRITE_TOOLS
        return {"tools": [{"name": n} for n in (READ_TOOLS | WRITE_TOOLS)]}

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "get_option_orders":
            return {"structuredContent": {"data": {"orders": self.orders}}}
        if name == "review_option_order":
            return {"structuredContent": {"data": {"order_checks": [], "approval_required": False}}}
        if name == "place_option_order":
            order_data = {
                "id": "rh-ord-999",
                "ref_id": arguments.get("ref_id"),
                "state": "unconfirmed",
                "price": str(arguments.get("price")),
                "quantity": str(arguments.get("quantity")),
                "processed_quantity": "0.0000",
                "pending_quantity": str(arguments.get("quantity")),
                "created_at": "2026-11-16T14:30:00Z",
                "legs": arguments.get("legs", []),
            }
            self.orders.append(order_data)
            return {"structuredContent": {"data": {"order": order_data}}}
        if name == "cancel_option_order":
            order_id = arguments.get("order_id")
            for o in self.orders:
                if o.get("id") == order_id:
                    o["state"] = "cancelled"
            return {"structuredContent": {"data": {"canceled": True, "order_id": order_id}}}
        return {"structuredContent": {"data": {}}}


def test_normalize_order_unconfirmed_to_submitted():
    ord_intent = make_intent("order-1")
    ref = get_expected_ref(ord_intent)
    raw = {
        "id": "rh-101",
        "ref_id": ref,
        "state": "unconfirmed",
        "quantity": "1.0000",
        "price": "1.2500",
        "processed_quantity": "0.0000",
        "pending_quantity": "1.0000",
        "created_at": "2026-11-16T15:00:00Z",
    }
    status = RobinhoodMCPBroker.normalize_order(raw, ord_intent)
    assert status.state == BrokerOrderState.SUBMITTED
    assert status.broker_order_id == "rh-101"
    assert status.logical_order_id == ord_intent.logical_order_id
    assert status.requested_quantity == 1
    assert status.filled_quantity == 0
    assert status.fills == ()


def test_normalize_order_confirmed_and_queued_to_accepted():
    ord_intent = make_intent("order-2", qty=2, price="2.00")
    ref = get_expected_ref(ord_intent)
    for state_name in ("confirmed", "queued"):
        raw = {
            "id": f"rh-{state_name}",
            "ref_id": ref,
            "state": state_name,
            "quantity": "2.0000",
            "price": "2.0000",
            "processed_quantity": "0.0000",
            "created_at": "2026-11-16T15:01:00Z",
        }
        status = RobinhoodMCPBroker.normalize_order(raw, ord_intent)
        assert status.state == BrokerOrderState.ACCEPTED
        assert status.broker_order_id == f"rh-{state_name}"
        assert status.requested_quantity == 2


def test_normalize_order_cancelled_and_canceled_to_canceled():
    ord_intent = make_intent("order-3", qty=1, price="1.10")
    ref = get_expected_ref(ord_intent)
    for state_name in ("cancelled", "canceled"):
        raw = {
            "id": f"rh-{state_name}",
            "ref_id": ref,
            "state": state_name,
            "quantity": "1",
            "price": "1.10",
            "processed_quantity": "0",
            "created_at": "2026-11-16T15:02:00Z",
        }
        status = RobinhoodMCPBroker.normalize_order(raw, ord_intent)
        assert status.state == BrokerOrderState.CANCELED
        assert status.broker_order_id == f"rh-{state_name}"


def test_normalize_order_missing_ref_id_matches_by_broker_order_id():
    ord_intent = make_intent("order-4", qty=1, price="1.50")
    raw = {
        "id": "rh-no-ref-100",
        "state": "filled",
        "quantity": "1.0000",
        "processed_quantity": "1.0000",
        "price": "1.5000",
        "created_at": "2026-11-16T15:05:00Z",
        "executions": [
            {
                "id": "exec-1",
                "quantity": "1.0000",
                "price": "1.5000",
                "timestamp": "2026-11-16T15:05:01Z",
            }
        ],
    }
    status = RobinhoodMCPBroker.normalize_order(
        raw,
        ord_intent,
        expected_broker_order_id="rh-no-ref-100",
    )
    assert status.state == BrokerOrderState.FILLED
    assert status.broker_order_id == "rh-no-ref-100"
    assert status.logical_order_id == ord_intent.logical_order_id
    assert status.filled_quantity == 1
    assert len(status.fills) == 1
    assert status.fills[0].price == Decimal("1.5000")


def test_normalize_order_fallback_timestamp_created_or_updated():
    ord_intent = make_intent("order-5")
    ref = get_expected_ref(ord_intent)
    raw = {
        "id": "rh-fallback-ts",
        "ref_id": ref,
        "state": "confirmed",
        "quantity": "1",
        "price": "1.25",
        "processed_quantity": "0",
        "updated_at": "2026-11-16T15:10:00Z",
    }
    status = RobinhoodMCPBroker.normalize_order(raw, ord_intent)
    assert status.accepted_at_utc is not None
    assert status.accepted_at_utc.year == 2026


def test_normalize_order_executions_list_fallback():
    ord_intent = make_intent("order-6", qty=2, price="1.80")
    ref = get_expected_ref(ord_intent)
    raw = {
        "id": "rh-exec-fallback",
        "ref_id": ref,
        "state": "filled",
        "quantity": "2.0000",
        "processed_quantity": "2.0000",
        "price": "1.8000",
        "created_at": "2026-11-16T15:20:00Z",
        "executions": [
            {
                "id": "exec-part-1",
                "quantity": "1.0000",
                "price": "1.8000",
                "timestamp": "2026-11-16T15:20:05Z",
            },
            {
                "id": "exec-part-2",
                "quantity": "1.0000",
                "price": "1.8000",
                "timestamp": "2026-11-16T15:20:06Z",
            },
        ],
    }
    status = RobinhoodMCPBroker.normalize_order(raw, ord_intent)
    assert status.state == BrokerOrderState.FILLED
    assert status.filled_quantity == 2
    assert len(status.fills) == 2
    assert status.fills[0].fill_id == "exec-part-1"
    assert status.fills[1].fill_id == "exec-part-2"


def test_durable_broker_id_persistence_and_recovery(tmp_path: Path):
    from tests.fixtures.live_mocks import StatefulFakeMCP

    db_path = tmp_path / "swift.sqlite3"
    apply_migrations(db_path)

    # Seed an option_signals and order_intents row
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO option_signals(signal_id,source_event_id,underlying,option_right,strike,expiration_date,original_limit,created_at_utc,state) "
            "VALUES('sig-1', 1, 'SPY', 'CALL', '680', '2026-11-20', '1.25', '2026-11-16T14:30:00Z', 'PROCESSED')"
        )
        db.execute(
            "INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,side,requested_quantity,limit_price,state) "
            "VALUES('order-log-1', 'sig-1', 'idemp-order-log-1', 'BUY_TO_OPEN', 1, '1.25', 'SUBMITTED')"
        )

    fake_mcp = StatefulFakeMCP(account_number="test-live-acc")
    mcp_broker = RobinhoodMCPBroker(fake_mcp, allow_live_writes=True)
    loop = asyncio.new_event_loop()

    try:
        broker1 = RobinhoodLiveBroker(mcp_broker, ACCOUNT, loop=loop, db_path=db_path)
        broker1._resolved_instruments[CONTRACT] = INSTRUMENT

        intent_obj = make_intent("order-log-1")
        submit_status = broker1.submit_order(intent_obj)
        assert submit_status.state in (BrokerOrderState.ACCEPTED, BrokerOrderState.SUBMITTED)
        assert submit_status.broker_order_id is not None
        assigned_broker_id = submit_status.broker_order_id

        # Verify broker_order_id was persisted into sqlite database
        with sqlite3.connect(db_path) as db:
            persisted_id = db.execute(
                "SELECT broker_order_id FROM order_intents WHERE logical_order_id='order-log-1'"
            ).fetchone()[0]
        assert persisted_id == assigned_broker_id

        # Simulate process crash and restart: create a new broker instance from same db
        broker2 = RobinhoodLiveBroker(mcp_broker, ACCOUNT, loop=loop, db_path=db_path)
        assert "order-log-1" in broker2._broker_id_map
        assert broker2._broker_id_map["order-log-1"] == assigned_broker_id

        # Query order on broker2 using logical ID; should resolve using persisted broker order id
        queried_status = broker2.get_order("order-log-1")
        assert queried_status.broker_order_id == assigned_broker_id
        assert queried_status.state in (BrokerOrderState.ACCEPTED, BrokerOrderState.SUBMITTED)

        # Cancel order on broker2
        cancel_status = broker2.cancel_order("order-log-1")
        assert cancel_status.state == BrokerOrderState.CANCELED

    finally:
        loop.close()


@pytest.mark.parametrize("mutations", [
    {"processed_quantity": "0.5"},
    {"processed_quantity": None},
    {"processed_quantity": "0", "filled_quantity": "1"},
    {"quantity": "2"},
    {"price": "1.26"},
])
def test_order_normalization_rejects_conflicting_quantity_and_price(mutations):
    order_intent = make_intent()
    raw = {"id": "synthetic-id", "ref_id": get_expected_ref(order_intent),
           "state": "confirmed", "quantity": "1", "price": "1.25",
           "processed_quantity": "0", "created_at": "2026-11-16T15:00:00Z"}
    raw.update(mutations)
    with pytest.raises(MCPContractError):
        RobinhoodMCPBroker.normalize_order(raw, order_intent)


def test_missing_processed_quantity_and_unbound_identity_rejected():
    order_intent = make_intent()
    raw = {"id": "synthetic-id", "ref_id": get_expected_ref(order_intent),
           "state": "confirmed", "quantity": "1", "price": "1.25",
           "created_at": "2026-11-16T15:00:00Z"}
    with pytest.raises(MCPContractError):
        RobinhoodMCPBroker.normalize_order(raw, order_intent)
    raw["processed_quantity"] = "0"
    del raw["ref_id"]
    with pytest.raises(MCPContractError):
        RobinhoodMCPBroker.normalize_order(raw, order_intent)


def test_fill_without_own_timestamp_is_not_inferred():
    order_intent = make_intent()
    raw = {"id": "synthetic-id", "ref_id": get_expected_ref(order_intent),
           "state": "filled", "quantity": "1", "price": "1.25",
           "processed_quantity": "1", "created_at": "2026-11-16T15:00:00Z",
           "last_transaction_at": "2026-11-16T15:01:00Z",
           "executions": [{"id": "synthetic-fill", "quantity": "1", "price": "1.25"}]}
    with pytest.raises(MCPContractError):
        RobinhoodMCPBroker.normalize_order(raw, order_intent)


@pytest.mark.parametrize("review", [
    {"order_checks": [], "approval_required": True},
    {"order_checks": [{"severity": "warning", "code": "REVIEW"}], "approval_required": False},
    {"order_checks": []},
])
def test_provider_review_blocks_placement(review, monkeypatch):
    from swift_autotrader.broker.robinhood_mcp import normalize_read_only_review
    adapter = RobinhoodMCPBroker(MockMCPClient(), allow_live_writes=True)
    async def fake_review(*args):
        return normalize_read_only_review(review)
    async def forbidden(*args, **kwargs):
        pytest.fail("Order placement must not be called")
    monkeypatch.setattr(adapter, "review_order", fake_review)
    monkeypatch.setattr(adapter, "_call", forbidden)
    with pytest.raises(MCPContractError):
        asyncio.run(adapter.place_order(ACCOUNT, INSTRUMENT, make_intent()))


def test_durable_identity_conflict_cannot_be_silently_ignored(tmp_path):
    db = tmp_path / "journal.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE order_intents(logical_order_id TEXT PRIMARY KEY, broker_order_id TEXT)")
        conn.execute("INSERT INTO order_intents VALUES ('logical-id', 'existing-broker-id')")
    broker = RobinhoodLiveBroker(RobinhoodMCPBroker(MockMCPClient(), allow_live_writes=True), ACCOUNT, db_path=db)
    with pytest.raises(MCPContractError, match="conflicting"):
        broker._persist_broker_id("logical-id", "other-broker-id")
    with pytest.raises(MCPContractError):
        broker._persist_broker_id("unknown-id", "other-broker-id")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT broker_order_id FROM order_intents").fetchone()[0] == "existing-broker-id"
