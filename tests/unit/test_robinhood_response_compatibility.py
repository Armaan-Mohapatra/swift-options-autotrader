"""Unit tests for Robinhood MCP response normalization, reconciliation, and durable order linkage."""
from __future__ import annotations

from datetime import UTC, date, datetime
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
    RobinhoodMCPBroker,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight

CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 12, 18))
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


def _live(tmp_path: Path, fake=None, *, journal: Path | None = None):
    """The canonical LiveBroker on the offline real-shape MCP fake."""
    from swift_autotrader.broker.live_broker import LiveBroker
    from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP, live_journal

    if fake is None:
        fake = StatefulFakeMCP(account_number=ACCOUNT.account_number,
                               clock=FakeClock(datetime(2026, 11, 16, 15, 0, tzinfo=UTC)))
    journal = journal or live_journal(tmp_path / "live_journal.sqlite3")
    broker = LiveBroker(journal, fake, account_number=ACCOUNT.account_number,
                        writes_enabled=True, kill_switch=lambda: False,
                        signal_gate=lambda _signal: "ENTRY", clock=fake.clock)
    return broker, fake


def _spy_entry(logical_id: str = "sig-1:entry") -> OrderIntent:
    return OrderIntent(logical_id, f"idemp-{logical_id}", CONTRACT, OrderSide.BUY_TO_OPEN,
                       1, Decimal("1.22"))


def test_durable_broker_id_persistence_and_recovery(tmp_path: Path):
    """Restart recovers the broker order from the durable live journal; never re-places."""
    broker1, fake = _live(tmp_path)
    try:
        status = broker1.submit_order(_spy_entry())
        assert status.state is BrokerOrderState.ACCEPTED
    finally:
        broker1.close()
    places = sum(1 for name, _ in fake.calls if name == "place_option_order")
    assert places == 1 and len(fake.orders) == 1
    broker_id = next(iter(fake.orders))

    # Simulated crash/restart: a new broker instance on the same journal.
    broker2, _ = _live(tmp_path, fake, journal=broker1.path)
    try:
        assert broker2._load("sig-1:entry").broker_id == broker_id
        assert broker2.get_order("sig-1:entry").state is BrokerOrderState.ACCEPTED
        assert broker2.submit_order(_spy_entry()).state is BrokerOrderState.ACCEPTED
        assert broker2.cancel_order("sig-1:entry").state is BrokerOrderState.CANCELED
        cancels = [a for name, a in fake.calls if name == "cancel_option_order"]
        assert [a["order_id"] for a in cancels] == [broker_id]
    finally:
        broker2.close()
    assert sum(1 for name, _ in fake.calls if name == "place_option_order") == 1


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
def test_provider_review_blocks_placement(review, tmp_path):
    """Robinhood's own pre-trade review is never bypassed: a blocked review sends nothing."""
    from swift_autotrader.broker.live_broker import LiveSafetyError

    broker, fake = _live(tmp_path)
    original = fake.call_tool

    async def reviewed(name, arguments):
        if name == "review_option_order":
            return {"structuredContent": {"data": review}}
        return await original(name, arguments)

    fake.call_tool = reviewed
    try:
        with pytest.raises((LiveSafetyError, MCPContractError)):
            broker.submit_order(_spy_entry())
    finally:
        broker.close()
    assert not any(name == "place_option_order" for name, _ in fake.calls)


def test_durable_identity_conflict_cannot_be_silently_ignored(tmp_path):
    """A changed broker order ID for a journaled order is refused, never overwritten."""
    from swift_autotrader.broker.live_broker import LiveSafetyError

    broker, fake = _live(tmp_path)
    try:
        status = broker.submit_order(_spy_entry())
        original_id = broker._load("sig-1:entry").broker_id
        assert original_id is not None
        with pytest.raises(LiveSafetyError, match="broker order ID changed"):
            broker._persist_status("sig-1:entry", status, "other-broker-id")
        with pytest.raises(LiveSafetyError, match="journal record missing"):
            broker._persist_status("unknown-id", status, "other-broker-id")
        assert broker._load("sig-1:entry").broker_id == original_id
        with pytest.raises(LiveSafetyError, match="idempotency identity reused"):
            broker.submit_order(OrderIntent("sig-1:entry", "a-different-key", CONTRACT,
                                            OrderSide.BUY_TO_OPEN, 1, Decimal("1.22")))
    finally:
        broker.close()
