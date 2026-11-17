"""Synthetic-only mocked official MCP tool contract tests; no network or credentials."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from swift_autotrader.broker.base import (
    Broker,
    BrokerOrderState,
)
from swift_autotrader.broker.mcp_transport import (
    MCPTransportUnavailable,
    OAuthEnrollment,
    _endpoint,
)
from swift_autotrader.broker.robinhood_mcp import (
    READ_TOOLS,
    WRITE_TOOLS,
    BrokerWritesDisabledError,
    MCPAccount,
    MCPContractError,
    MCPInstrument,
    MCPReadError,
    RobinhoodLiveBroker,
    RobinhoodMCPBroker,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight


def wait(coro):
    return asyncio.run(coro)


CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
ACCOUNT = MCPAccount("synthetic-account", True, "option_level_2")
INSTRUMENT = MCPInstrument("synthetic-option-id", CONTRACT, True, "synthetic-chain")


def intent(side=OrderSide.BUY_TO_OPEN, price="1.20"):
    return OrderIntent("synthetic-logical-order", "synthetic-once-key", CONTRACT,
                       side, 2, Decimal(price))


class FakeMCP:
    def __init__(self, *, omit=(), schemas=None):
        self.calls = []
        self.fail = None
        self.responses = {}
        self.names = (READ_TOOLS | WRITE_TOOLS) - set(omit)
        self.schemas = schemas or {}

    async def list_tools(self):
        if self.fail == "list_tools":
            raise TimeoutError("synthetic timeout")
        tools = []
        from swift_autotrader.broker.robinhood_mcp import _REQUIRED
        for name in sorted(self.names):
            properties = {f: {"type": "string"} for f in _REQUIRED[name]}
            tools.append({"name": name, "inputSchema": {
                "type": "object", "properties": self.schemas.get(name, properties),
            }})
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail == name:
            raise ConnectionError("synthetic disconnect")
        answer = self.responses.get(name)
        if callable(answer):
            answer = answer(arguments)
        if answer is None:
            raise AssertionError(f"Unexpected MCP call: {name}")
        return {"structuredContent": {"data": answer}}


def setup():
    fake = FakeMCP()
    fake.responses["get_accounts"] = {"accounts": [
        {"account_number": "unrelated-readonly", "agentic_allowed": False,
         "option_level": "option_level_2"},
        {"account_number": "synthetic-account", "agentic_allowed": True,
         "option_level": "option_level_2"},
    ]}
    fake.responses["get_portfolio"] = {"buying_power": {"buying_power": "1000.00"}}
    fake.responses["get_option_chains"] = {"chains": [{
        "id": "synthetic-chain", "underlying_symbol": "SPY",
        "expiration_dates": [CONTRACT.expiration.isoformat()],
    }]}
    fake.responses["get_option_instruments"] = {"instruments": [{
        "id": "synthetic-option-id", "chain_id": "synthetic-chain",
        "chain_symbol": "SPY", "expiration_date": "2026-11-20",
        "strike_price": "680.0000", "type": "call", "state": "active",
        "tradability": "tradable",
    }]}
    fake.responses["get_option_quotes"] = {"quotes": [{
        "instrument_id": "synthetic-option-id", "bid_price": "1.12", "ask_price": "1.25",
        "updated_at": "2026-11-12T18:05:00Z",
    }]}
    fake.responses["review_option_order"] = {"order_checks": [], "approval_required": False}
    fake.responses["get_option_orders"] = {"orders": []}
    fake.responses["get_option_positions"] = {"positions": []}
    return fake, RobinhoodMCPBroker(fake)


def test_mcp_endpoint_is_strictly_pinned():
    assert _endpoint("https://agent.robinhood.com/mcp/trading")
    for bad in (
        "http://agent.robinhood.com/mcp/trading", "https://evil.test/mcp/trading",
        "https://agent.robinhood.com.evil.test/mcp/trading",
        "https://agent.robinhood.com/mcp/trading?auth=x",
        "https://user@agent.robinhood.com/mcp/trading",
        "https://agent.robinhood.com/mcp/other",
    ):
        with pytest.raises(MCPTransportUnavailable):
            _endpoint(bad)


def test_oauth_enrollment_requires_explicit_callbacks_and_loopback():
    async def handler(_url):
        return None
    async def callback():
        return None
    OAuthEnrollment(object(), handler, callback)
    with pytest.raises(ValueError):
        OAuthEnrollment(None, handler, callback)
    with pytest.raises(ValueError):
        OAuthEnrollment(object(), handler, callback, redirect_uri="https://evil.test/cb")


def test_tools_discovery_only_lists_and_does_not_trade():
    fake, adapter = setup()
    state = wait(adapter.discover())
    assert state.status == "READ_ONLY_SCHEMA_OK"
    assert state.recognized_read_tools == tuple(sorted(READ_TOOLS))
    assert state.real_writes_enabled is False
    assert fake.calls == []


def test_discovery_fails_closed_missing_tools_schema_and_duplicates():
    missing = FakeMCP(omit={"get_option_quotes"})
    state = wait(RobinhoodMCPBroker(missing).discover())
    assert "get_option_quotes" in state.missing_tools
    wrong = FakeMCP(schemas={"get_option_quotes": {"wrong": {"type": "string"}}})
    adapter = RobinhoodMCPBroker(wrong)
    assert "get_option_quotes" in wait(adapter.discover()).schema_mismatches
    with pytest.raises(MCPContractError):
        wait(adapter._call("get_option_quotes", {"instrument_ids": ["x"]}))


def test_account_selection_distinguishes_read_only_and_eligible():
    fake, adapter = setup()
    accounts = wait(adapter.accounts())
    assert len(accounts) == 2
    assert accounts[0].eligible is False
    assert wait(adapter.external_account()) == ACCOUNT
    fake.responses["get_accounts"]["accounts"][1]["option_level"] = "option_level_0"
    with pytest.raises(MCPContractError):
        wait(adapter.external_account())
    fake.responses["get_accounts"]["accounts"][1]["option_level"] = "option_level_2"
    fake.responses["get_accounts"]["accounts"].append({
        "account_number": "second-allowed", "agentic_allowed": True,
        "option_level": "option_level_3",
    })
    with pytest.raises(MCPContractError):
        wait(adapter.external_account())


def test_buying_power_read_uses_only_external_account():
    fake, adapter = setup()
    assert wait(adapter.buying_power(ACCOUNT)) == Decimal("1000.00")
    with pytest.raises(MCPContractError):
        wait(adapter.buying_power(MCPAccount("read-only", False, "option_level_2")))
    fake.responses["get_portfolio"] = {"buying_power": None}
    with pytest.raises(MCPContractError):
        wait(adapter.buying_power(ACCOUNT))


def test_exact_contract_chain_lookup_and_quote_not_price_chasing():
    fake, adapter = setup()
    found = wait(adapter.resolve_instrument(CONTRACT))
    assert found == INSTRUMENT
    quote = wait(adapter.quote(found, now=datetime(2026, 11, 12, 18, 5, 15, tzinfo=UTC)))
    assert quote.ask == Decimal("1.25")
    assert adapter.future_order_payload(ACCOUNT, found, intent())["price"] == "1.20"
    assert fake.calls[-1][0] == "get_option_quotes"
    with pytest.raises(MCPContractError):
        wait(adapter.quote(found, now=datetime(2026, 11, 12, 18, 6, 15, tzinfo=UTC)))


def test_conflicting_contracts_or_missing_chain_stay_blocked():
    fake, adapter = setup()
    record = fake.responses["get_option_instruments"]["instruments"][0]
    record["strike_price"] = "681"
    with pytest.raises(MCPContractError):
        wait(adapter.resolve_instrument(CONTRACT))
    record["strike_price"] = "680"
    fake.responses["get_option_instruments"]["instruments"].append(record.copy())
    with pytest.raises(MCPContractError):
        wait(adapter.resolve_instrument(CONTRACT))
    fake.responses["get_option_chains"] = {"chains": []}
    with pytest.raises(MCPContractError):
        wait(adapter.resolve_instrument(CONTRACT))


def test_review_payload_is_exact_price_and_does_not_submit():
    fake, adapter = setup()
    review = wait(adapter.review_order(ACCOUNT, INSTRUMENT, intent(price="0.95")))
    assert review.checked and review.unblocked and not review.submitted
    name, arguments = fake.calls[-1]
    assert name == "review_option_order"
    assert arguments["price"] == "0.95" and arguments["type"] == "limit"
    assert arguments["time_in_force"] == "gfd" and len(arguments["legs"]) == 1
    assert not any(name in WRITE_TOOLS for name, _ in fake.calls)
    fake.responses["review_option_order"] = {
        "order_checks": [{"severity": "warning", "code": "PERMISSION_REQUIRED"}],
        "approval_required": True,
    }
    restricted = wait(adapter.review_order(ACCOUNT, INSTRUMENT, intent()))
    assert not restricted.unblocked and restricted.approval_required
    assert restricted.alerts == ("PERMISSION_REQUIRED",)
    fake.responses["review_option_order"] = {"order_checks": []}
    with pytest.raises(MCPContractError):
        wait(adapter.review_order(ACCOUNT, INSTRUMENT, intent()))


def test_future_sell_close_and_buy_open_are_exact_and_idempotent():
    adapter = RobinhoodMCPBroker(FakeMCP())
    entry = adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent())
    assert entry["legs"][0]["side"] == "buy"
    assert entry["legs"][0]["position_effect"] == "open"
    assert entry["price"] == "1.20"
    assert entry["ref_id"] == adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent())["ref_id"]
    close = adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent(OrderSide.SELL_TO_CLOSE))
    assert close["legs"][0]["side"] == "sell"
    assert close["legs"][0]["position_effect"] == "close"
    assert adapter.future_cancel_payload(ACCOUNT, "synthetic-broker-order")["order_id"]


def test_broker_writes_cannot_be_unlocked_by_environment(monkeypatch):
    monkeypatch.setenv("LIVE_ENABLED", "true")
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "true")
    fake, adapter = setup()
    for command in (lambda: adapter.submit_order(intent()),
                    lambda: adapter.cancel_order("synthetic-logical"),
                    lambda: adapter.get_order("synthetic-logical")):
        with pytest.raises(BrokerWritesDisabledError):
            command()
    with pytest.raises(BrokerWritesDisabledError):
        wait(adapter._call("place_option_order", {}))
    with pytest.raises(BrokerWritesDisabledError):
        wait(adapter._call("cancel_option_order", {}))
    assert fake.calls == []


def test_unknown_state_and_partial_fill_mapping():
    adapter = RobinhoodMCPBroker(FakeMCP())
    ref = adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent())["ref_id"]
    order = {
        "id": "synthetic-broker-id", "ref_id": ref, "state": "partially_filled",
        "filled_quantity": "1", "accepted_at": "2026-11-12T18:00:00Z",
        "fills": [{"id": "synthetic-fill", "quantity": "1", "price": "1.10",
                   "timestamp": "2026-11-12T18:00:01Z"}],
    }
    status = adapter.normalize_order(order, intent())
    assert status.state is BrokerOrderState.PARTIALLY_FILLED
    assert status.filled_quantity == 1 and status.fills[0].price == Decimal("1.10")
    order["state"] = "pending_cancelled"
    assert adapter.normalize_order(order, intent()).state is BrokerOrderState.CANCEL_PENDING
    order["state"] = "cancelled"
    assert adapter.normalize_order(order, intent()).state is BrokerOrderState.CANCELED
    order["state"] = "filled"
    order["filled_quantity"] = "2"
    with pytest.raises(MCPContractError):
        adapter.normalize_order(order, intent())
    order["state"] = "impossible-state"
    with pytest.raises(MCPContractError):
        adapter.normalize_order(order, intent())
    order["state"] = "filled"
    order["ref_id"] = "other-ref"
    with pytest.raises(MCPContractError):
        adapter.normalize_order(order, intent())


def test_order_reconciliation_by_exact_persisted_ref_never_resubmits():
    fake, adapter = setup()
    ref = adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent())["ref_id"]
    fake.responses["get_option_orders"] = {"orders": [{
        "id": "synthetic-broker-id", "ref_id": ref, "state": "filled",
        "filled_quantity": 2, "accepted_at": "2026-11-12T18:00:00Z",
        "fills": [
            {"id": "fill-1", "quantity": 1, "price": "1.19", "timestamp": "2026-11-12T18:00:01Z"},
            {"id": "fill-2", "quantity": 1, "price": "1.20", "timestamp": "2026-11-12T18:00:02Z"},
        ],
    }]}
    status = wait(adapter.reconcile_order(ACCOUNT, intent()))
    assert status.state == BrokerOrderState.FILLED and len(status.fills) == 2
    assert fake.calls == [("get_option_orders", {"account_number": "synthetic-account"})]
    fake.responses["get_option_orders"] = {"orders": []}
    assert wait(adapter.reconcile_order(ACCOUNT, intent())).state == BrokerOrderState.UNKNOWN
    assert all(name not in WRITE_TOOLS for name, _ in fake.calls)


def test_timeout_and_schema_errors_do_not_retry_order():
    fake, adapter = setup()
    fake.fail = "get_option_orders"
    with pytest.raises(MCPReadError):
        wait(adapter.reconcile_order(ACCOUNT, intent()))
    assert len(fake.calls) == 1
    assert all(name not in WRITE_TOOLS for name, _ in fake.calls)
    fake.fail = "list_tools"
    with pytest.raises(MCPReadError):
        wait(RobinhoodMCPBroker(fake).discover())


def test_preflight_snapshot_stays_nontradable_without_verified_market_hours_and_tick():
    fake, adapter = setup()
    preflight = wait(adapter.read_only_preflight(CONTRACT, account=ACCOUNT))
    assert preflight.instrument_verified
    assert preflight.available_buying_power_usd == 1000
    assert preflight.market_session_open is False
    assert preflight.premium_tick == 0


def test_read_only_positions_and_order_pagination_rejects_cycles():
    fake, adapter = setup()
    fake.responses["get_option_positions"] = {"positions": [{"option_id": "synthetic-option-id"}]}
    assert len(wait(adapter.positions(ACCOUNT, option_id="synthetic-option-id"))) == 1
    fake.responses["get_option_orders"] = {"orders": [], "next_cursor": "repeat"}
    with pytest.raises(MCPContractError):
        wait(adapter.orders(ACCOUNT, limit_pages=4))


def test_position_reconciliation_does_not_assume_missing_means_closed():
    fake, adapter = setup()
    state = wait(adapter.reconcile_position(ACCOUNT, "synthetic-option-id"))
    assert state.state == "UNKNOWN" and state.quantity is None
    fake.responses["get_option_positions"] = {"positions": [{
        "option_id": "synthetic-option-id", "quantity": "2", "type": "long",
    }]}
    assert wait(adapter.reconcile_position(ACCOUNT, "synthetic-option-id")).state == "OPEN"
    fake.responses["get_option_positions"]["positions"][0]["quantity"] = "0"
    closed = wait(adapter.reconcile_position(ACCOUNT, "synthetic-option-id"))
    assert closed.state == "CLOSED" and closed.confirmed_from_broker
    assert fake.calls[-1][1]["nonzero"] is False


def test_order_fill_total_missing_is_not_assumed_filled():
    adapter = RobinhoodMCPBroker(FakeMCP())
    ref = adapter.future_order_payload(ACCOUNT, INSTRUMENT, intent())["ref_id"]
    order = {
        "id": "synthetic-order", "ref_id": ref, "state": "filled",
        "filled_quantity": "2", "accepted_at": "2026-11-12T18:00:00Z",
    }
    with pytest.raises(MCPContractError, match="fills"):
        adapter.normalize_order(order, intent())


def test_get_option_orders_extracts_documented_next_url():
    fake, adapter = setup()
    called = []

    def results(args):
        called.append(args.copy())
        if "cursor" not in args:
            return {"orders": [], "next": "https://example.test/path?cursor=page2"}
        return {"orders": []}

    fake.responses["get_option_orders"] = results
    assert wait(adapter.orders(ACCOUNT)) == []
    assert len(called) == 2 and called[-1]["cursor"] == "page2"


def test_observed_robinhood_contract_and_nested_quote_shape():
    """Sanitized public read-only option response schema, no account access."""
    fake, adapter = setup()
    fake.responses["get_option_chains"]["chains"][0] = {
        "id": "synthetic-chain", "symbol": "SPY",
        "expiration_dates": [CONTRACT.expiration.isoformat()],
        "trade_value_multiplier": "100.0000",
        "min_ticks": {"above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00"},
    }
    fake.responses["get_option_instruments"] = {"instruments": [{
        "id": "synthetic-option-id", "chain_id": "synthetic-chain",
        "chain_symbol": "SPY", "expiration_date": "2026-11-20",
        "strike_price": "680.0000", "type": "call", "state": "active",
        "tradability": "tradable", "trade_value_multiplier": "100.0000",
        "min_ticks": {"above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00"},
    }]}
    fake.responses["get_option_quotes"] = {"results": [{"quote": {
        "instrument_id": "synthetic-option-id", "bid_price": "1.12",
        "ask_price": "1.25", "bid_size": 12, "ask_size": 21,
        "updated_at": "2026-11-12T18:05:00.000000000Z",
    }, "close": {"price": "1.20"}}]}
    instrument = wait(adapter.resolve_instrument(CONTRACT))
    assert instrument.minimum_tick == Decimal("0.01")
    assert instrument.multiplier == 100
    quote = wait(adapter.quote(instrument, now=datetime(2026, 11, 12, 18, 5, 10, tzinfo=UTC)))
    assert quote.bid == Decimal("1.12") and quote.ask == Decimal("1.25")
    assert quote.bid_size == 12 and quote.ask_size == 21
    assert not any(name in WRITE_TOOLS for name, _ in fake.calls)


def test_robinhood_fields_fail_closed_if_unknown_or_conflicting():
    fake, adapter = setup()
    item = fake.responses["get_option_instruments"]["instruments"][0]
    item["trade_value_multiplier"] = "99.5"
    with pytest.raises(MCPContractError):
        wait(adapter.resolve_instrument(CONTRACT))
    item["trade_value_multiplier"] = "100.0000"
    item["min_ticks"] = {"above_tick": "0.05", "below_tick": "0.01",
                         "cutoff_price": "3.00"}
    assert wait(adapter.resolve_instrument(CONTRACT)).minimum_tick is None
    item["min_ticks"]["below_tick"] = "0"
    with pytest.raises(MCPContractError):
        wait(adapter.resolve_instrument(CONTRACT))


def test_opaque_option_pagination_cursor_is_accepted_without_url_following():
    from swift_autotrader.broker.robinhood_mcp import _next_cursor

    assert _next_cursor({"next": "cD03NjkuMDAwMA=="}) == "cD03NjkuMDAwMA=="
    assert _next_cursor({"next": "https://api.example.test/?cursor=abc%3D%3D"}) == "abc=="
    with pytest.raises(MCPContractError):
        _next_cursor({"next": "./../../unsafe-path"})


def test_pinned_mcp_account_refuses_permission_transfer_or_ambiguity():
    fake, _ = setup()
    adapter = RobinhoodMCPBroker(fake, approved_account_number=ACCOUNT.account_number)
    assert wait(adapter.external_account()) == ACCOUNT
    records = fake.responses["get_accounts"]["accounts"]
    records[1]["agentic_allowed"] = False
    records[0]["agentic_allowed"] = True
    with pytest.raises(MCPContractError, match="no fallback"):
        wait(adapter.external_account())
    records[1]["agentic_allowed"] = True
    with pytest.raises(MCPContractError):
        wait(adapter.external_account())


def test_pinned_mcp_account_blocks_other_account_rpc_before_transport():
    fake, _ = setup()
    adapter = RobinhoodMCPBroker(fake, approved_account_number=ACCOUNT.account_number)
    other = MCPAccount("unrelated-readonly", True, "option_level_2")
    with pytest.raises(MCPContractError, match="differs"):
        wait(adapter.buying_power(other))
    with pytest.raises(MCPContractError, match="differs"):
        wait(adapter.review_order(other, INSTRUMENT, intent()))
    assert not fake.calls
    assert wait(adapter.buying_power(ACCOUNT)) == Decimal("1000.00")
    assert fake.calls[-1][1]["account_number"] == ACCOUNT.account_number


def test_sdk_snake_case_schema_survives_discovery_and_account_scoped_reads():
    fake, _ = setup()
    original_list = fake.list_tools

    async def list_tools():
        response = await original_list()
        for tool in response.tools:
            tool["input_schema"] = tool.pop("inputSchema")
        return response

    fake.list_tools = list_tools
    adapter = RobinhoodMCPBroker(fake, approved_account_number=ACCOUNT.account_number)
    assert wait(adapter.discover()).status == "READ_ONLY_SCHEMA_OK"
    account = wait(adapter.external_account())
    assert wait(adapter.buying_power(account)) == Decimal("1000.00")
    assert wait(adapter.orders(account)) == []
    assert wait(adapter.positions(account)) == []
    assert not any(name in WRITE_TOOLS for name, _ in fake.calls)


def test_sdk_conflicting_schema_aliases_are_rejected_before_broker_read():
    fake, adapter = setup()
    original_list = fake.list_tools

    async def list_tools():
        response = await original_list()
        response.tools[0]["input_schema"] = {"properties": {"conflict": {}}}
        return response

    fake.list_tools = list_tools
    with pytest.raises(MCPContractError, match="Conflicting"):
        wait(adapter.discover())
    assert fake.calls == []


def test_live_writes_disabled_by_default():
    fake, adapter = setup()
    with pytest.raises(BrokerWritesDisabledError):
        wait(adapter._call("place_option_order", {"account_number": "ACC1", "legs": [], "quantity": "1"}))


def test_live_writes_allowed_when_enabled():
    fake, _ = setup()
    adapter = RobinhoodMCPBroker(fake, allow_live_writes=True)
    fake.responses["place_option_order"] = lambda args: {
        "order": {"id": "ord_live_123", "state": "confirmed"}
    }
    result = wait(adapter._call("place_option_order", {
        "account_number": "ACC1", "legs": [{"option_id": "opt1", "side": "buy", "ratio_quantity": 1}],
        "quantity": "1",
    }))
    assert result["order"]["id"] == "ord_live_123"
    assert ("place_option_order", {
        "account_number": "ACC1", "legs": [{"option_id": "opt1", "side": "buy", "ratio_quantity": 1}],
        "quantity": "1",
    }) in fake.calls


def setup_live_broker():
    fake, _ = setup()
    orders_by_id: dict[str, dict] = {}

    def handle_place(args):
        broker_order_id = f"rh-broker-ord-{len(orders_by_id) + 1}"
        order = {
            "id": broker_order_id,
            "ref_id": args.get("ref_id"),
            "state": "confirmed",
            "filled_quantity": 0,
            "accepted_at": "2026-11-12T18:00:00Z",
            "fills": [],
        }
        orders_by_id[broker_order_id] = order
        return {"order": order}

    def handle_cancel(args):
        oid = args.get("order_id")
        if oid in orders_by_id:
            orders_by_id[oid]["state"] = "cancelled"
        return {"status": "success"}

    def handle_get_orders(args):
        oid = args.get("order_id")
        if oid and oid in orders_by_id:
            return {"orders": [orders_by_id[oid]]}
        return {"orders": list(orders_by_id.values())}

    fake.responses["place_option_order"] = handle_place
    fake.responses["cancel_option_order"] = handle_cancel
    fake.responses["get_option_orders"] = handle_get_orders

    mcp_broker = RobinhoodMCPBroker(fake, allow_live_writes=True)
    live_broker = RobinhoodLiveBroker(mcp_broker, ACCOUNT)
    return fake, mcp_broker, live_broker, orders_by_id


def test_robinhood_live_broker_protocol_conformance_and_fail_closed():
    fake, mcp_broker, live_broker, _ = setup_live_broker()

    # Must conform to Broker protocol
    assert isinstance(live_broker, Broker)

    # Fail closed when writes disabled on MCP broker
    readonly_mcp = RobinhoodMCPBroker(fake, allow_live_writes=False)
    with pytest.raises(BrokerWritesDisabledError, match="allow_live_writes=True"):
        RobinhoodLiveBroker(readonly_mcp, ACCOUNT)

    # Fail closed with ineligible account
    ineligible_account = MCPAccount("bad-acc", False, "option_level_2")
    with pytest.raises(MCPContractError, match="option-eligible"):
        RobinhoodLiveBroker(mcp_broker, ineligible_account)

    ineligible_level = MCPAccount("bad-lvl", True, "option_level_0")
    with pytest.raises(MCPContractError, match="option-eligible"):
        RobinhoodLiveBroker(mcp_broker, ineligible_level)

    # Dynamic fail-closed check if allow_live_writes becomes False
    mcp_broker.allow_live_writes = False
    test_intent = intent()
    with pytest.raises(BrokerWritesDisabledError, match="disabled"):
        live_broker.submit_order(test_intent)
    with pytest.raises(BrokerWritesDisabledError, match="disabled"):
        live_broker.cancel_order(test_intent.logical_order_id)


def test_robinhood_live_broker_submit_order_places_and_reconciles():
    fake, _, live_broker, orders_by_id = setup_live_broker()
    ord_intent = intent(price="1.20")

    status = live_broker.submit_order(ord_intent)

    # Check status returned is reconciled and accepted
    assert status.logical_order_id == ord_intent.logical_order_id
    assert status.state == BrokerOrderState.ACCEPTED
    assert status.filled_quantity == 0
    assert status.requested_quantity == 2
    assert status.limit_price == Decimal("1.20")
    assert status.contract == CONTRACT

    # Verify place_option_order was dispatched with exact parameters
    place_calls = [c for c in fake.calls if c[0] == "place_option_order"]
    assert len(place_calls) == 1
    args = place_calls[0][1]
    assert args["account_number"] == ACCOUNT.account_number
    assert args["quantity"] == "2"
    assert args["price"] == "1.20"
    assert args["type"] == "limit"
    assert args["time_in_force"] == "gfd"
    assert len(args["legs"]) == 1
    assert args["legs"][0]["option_id"] == "synthetic-option-id"
    assert args["legs"][0]["side"] == "buy"
    assert args["legs"][0]["position_effect"] == "open"

    # Verify cached instrument resolution on subsequent submit
    calls_before = len(fake.calls)
    second_intent = OrderIntent("logical-2", "once-key-2", CONTRACT, OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"))
    live_broker.submit_order(second_intent)
    chains_calls = [c for c in fake.calls[calls_before:] if c[0] in ("get_option_chains", "get_option_instruments")]
    assert len(chains_calls) == 0  # Reused cached instrument


def test_robinhood_live_broker_get_order_and_fills():
    fake, _, live_broker, orders_by_id = setup_live_broker()
    ord_intent = intent()

    # Unknown logical order ID
    unknown_status = live_broker.get_order("nonexistent-order")
    assert unknown_status.state == BrokerOrderState.UNKNOWN

    # Submit order
    live_broker.submit_order(ord_intent)

    # Poll status before fill
    status = live_broker.get_order(ord_intent.logical_order_id)
    assert status.state == BrokerOrderState.ACCEPTED
    assert status.filled_quantity == 0

    # Simulate fill at broker
    broker_id = live_broker._broker_id_map[ord_intent.logical_order_id]
    orders_by_id[broker_id]["state"] = "filled"
    orders_by_id[broker_id]["filled_quantity"] = 2
    orders_by_id[broker_id]["fills"] = [
        {"id": "fill-101", "quantity": 1, "price": "1.19", "timestamp": "2026-11-12T18:01:00Z"},
        {"id": "fill-102", "quantity": 1, "price": "1.20", "timestamp": "2026-11-12T18:01:01Z"},
    ]

    status_filled = live_broker.get_order(ord_intent.logical_order_id)
    assert status_filled.state == BrokerOrderState.FILLED
    assert status_filled.filled_quantity == 2
    assert len(status_filled.fills) == 2
    assert status_filled.fills[0].price == Decimal("1.19")
    assert status_filled.fills[1].price == Decimal("1.20")


def test_robinhood_live_broker_cancel_order_bug_fix_and_reconciliation():
    """Verify bug fix: cancel_order calls cancel_order_by_id (NOT cancel_broker_order)."""
    fake, _, live_broker, orders_by_id = setup_live_broker()
    ord_intent = intent()

    # Unknown order returns UNKNOWN without calling cancel
    unknown = live_broker.cancel_order("does-not-exist")
    assert unknown.state == BrokerOrderState.UNKNOWN
    assert not any(c[0] == "cancel_option_order" for c in fake.calls)

    # Submit valid order
    live_broker.submit_order(ord_intent)
    broker_id = live_broker._broker_id_map[ord_intent.logical_order_id]

    # Cancel order — this previously crashed calling non-existent cancel_broker_order
    cancel_status = live_broker.cancel_order(ord_intent.logical_order_id)

    # Verify cancel tool was called on FakeMCP
    cancel_calls = [c for c in fake.calls if c[0] == "cancel_option_order"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1]["account_number"] == ACCOUNT.account_number
    assert cancel_calls[0][1]["order_id"] == broker_id

    # Verify returned status reflects cancelled state
    assert cancel_status.state == BrokerOrderState.CANCELED
    assert cancel_status.logical_order_id == ord_intent.logical_order_id


def test_robinhood_live_broker_cancel_recovers_broker_id_if_unmapped():
    """If place_order didn't store broker_id directly, get_order discovers it before cancel."""
    fake, _, live_broker, orders_by_id = setup_live_broker()

    # Override place_option_order to NOT return order id in place response
    def minimal_place(args):
        broker_order_id = "discovered-broker-id"
        order = {
            "id": broker_order_id,
            "ref_id": args.get("ref_id"),
            "state": "confirmed",
            "filled_quantity": 0,
            "accepted_at": "2026-11-12T18:00:00Z",
            "fills": [],
        }
        orders_by_id[broker_order_id] = order
        return {"result": "order placed"}  # No 'order' dict in response

    fake.responses["place_option_order"] = minimal_place
    ord_intent = intent()

    live_broker.submit_order(ord_intent)
    # Manually clear broker_id_map to simulate missing initial map
    live_broker._broker_id_map.pop(ord_intent.logical_order_id, None)

    # cancel_order will call get_order to recover the broker_id
    status = live_broker.cancel_order(ord_intent.logical_order_id)
    assert status.state == BrokerOrderState.CANCELED
    assert live_broker._broker_id_map.get(ord_intent.logical_order_id) == "discovered-broker-id"

    cancel_calls = [c for c in fake.calls if c[0] == "cancel_option_order"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1]["order_id"] == "discovered-broker-id"


def test_robinhood_live_broker_sell_to_close_order():
    fake, _, live_broker, _ = setup_live_broker()
    close_intent = intent(side=OrderSide.SELL_TO_CLOSE, price="2.50")

    status = live_broker.submit_order(close_intent)
    assert status.state == BrokerOrderState.ACCEPTED
    assert status.side == OrderSide.SELL_TO_CLOSE

    place_calls = [c for c in fake.calls if c[0] == "place_option_order"]
    assert len(place_calls) == 1
    legs = place_calls[0][1]["legs"]
    assert legs[0]["side"] == "sell"
    assert legs[0]["position_effect"] == "close"


def test_robinhood_live_broker_with_event_loop():
    fake, _, _, _ = setup_live_broker()
    loop = asyncio.new_event_loop()
    try:
        mcp = RobinhoodMCPBroker(fake, allow_live_writes=True)
        live_broker = RobinhoodLiveBroker(mcp, ACCOUNT, loop=loop)
        ord_intent = intent()

        status = live_broker.submit_order(ord_intent)
        assert status.state == BrokerOrderState.ACCEPTED

        get_status = live_broker.get_order(ord_intent.logical_order_id)
        assert get_status.state == BrokerOrderState.ACCEPTED

        cancel_status = live_broker.cancel_order(ord_intent.logical_order_id)
        assert cancel_status.state == BrokerOrderState.CANCELED
    finally:
        loop.close()


def test_fake_mcp_ref_id_idempotency_deduplication():
    """Verify StatefulFakeMCP returns existing order on duplicate ref_id submission."""
    from tests.fixtures.live_mocks import StatefulFakeMCP

    mcp = StatefulFakeMCP()
    args = {
        "account_number": "synthetic-account",
        "ref_id": "idemp_test_999",
        "price": "1.50",
        "quantity": 1,
        "legs": [{"side": "buy", "ref_id": "idemp_test_999"}],
    }
    res1 = wait(mcp.call_tool("place_option_order", args))
    ord1 = res1["structuredContent"]["data"]["order"]
    assert ord1["id"] == "rh_ord_1001"
    assert ord1["ref_id"] == "idemp_test_999"

    # Second duplicate submission with exact same ref_id
    res2 = wait(mcp.call_tool("place_option_order", args))
    ord2 = res2["structuredContent"]["data"]["order"]
    assert ord2["id"] == ord1["id"]
    assert ord2["ref_id"] == ord1["ref_id"]
    assert len(mcp.orders) == 1


