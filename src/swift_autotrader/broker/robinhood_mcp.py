"""Read-only Robinhood Trading MCP adapter, with guarded future-write payloads.

Official tool names are verified in Robinhood's public MCP help center. Their
live input/output schema is discovered at connection time and fail-closed. No
transport is opened by construction, and no method calls place/cancel tools.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

from swift_autotrader.broker.base import (
    BrokerFill,
    BrokerOrderState,
    BrokerOrderStatus,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.risk import Preflight
from swift_autotrader.signals.identity import OptionContract


class BrokerWritesDisabledError(PermissionError):
    """No real brokerage writes are available in GitHub Phase 4."""


class MCPContractError(RuntimeError):
    """The MCP tool or data contract is missing, ambiguous or unsafe."""


class MCPReadError(RuntimeError):
    """Read-only MCP call failed; do not assume a trade or fill occurred."""


class MCPAsyncClient(Protocol):
    async def list_tools(self) -> Any: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_option_chains", "get_option_instruments",
    "get_option_quotes", "get_option_orders", "get_option_positions",
    "review_option_order",
})
WRITE_TOOLS = frozenset({"place_option_order", "cancel_option_order"})
_REQUIRED = {
    "get_accounts": set(),
    "get_portfolio": {"account_number"},
    "get_option_chains": set(),
    "get_option_instruments": set(),
    "get_option_quotes": {"instrument_ids"},
    "get_option_orders": {"account_number"},
    "get_option_positions": {"account_number"},
    "review_option_order": {"account_number", "legs", "quantity"},
    "place_option_order": {"account_number", "legs", "quantity"},
    "cancel_option_order": {"account_number", "order_id"},
}


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        result = value.model_dump(exclude_none=False)
        if isinstance(result, dict):
            return result
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise MCPContractError("MCP response is not a JSON object")


def _data(response: Any) -> dict[str, Any]:
    result = _obj(response)
    if result.get("isError") or result.get("is_error"):
        raise MCPReadError("MCP tool returned an error")
    levels = [result]
    content = result.get("structuredContent", result.get("structured_content"))
    if content is not None:
        result = _obj(content)
        levels.append(result)
    if "data" in result:
        result = _obj(result["data"])
        levels.append(result)
    # `next` may sit beside `data` or inside it. Surface it on the returned object;
    # two different values are ambiguous, so never pick one.
    for key in ("next", "next_cursor"):
        values = {repr(level[key]) for level in levels if level.get(key) is not None}
        if len(values) > 1:
            raise MCPContractError("Conflicting pagination fields in MCP response")
        if values and result.get(key) is None:
            result[key] = next(level[key] for level in levels if level.get(key) is not None)
    return result


def _rows(data: Mapping[str, Any], *fields: str) -> list[dict[str, Any]]:
    for field in fields:
        if field in data:
            value = data[field]
            if not isinstance(value, list):
                raise MCPContractError("MCP collection has unexpected shape")
            return [_obj(item) for item in value]
    raise MCPContractError("MCP collection field missing")


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MCPContractError(f"{field} is not an exact decimal")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MCPContractError(f"{field} is malformed") from exc
    if not numeric.is_finite() or numeric < 0:
        raise MCPContractError(f"{field} must be finite nonnegative")
    return numeric


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MCPContractError("UTC timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MCPContractError("Malformed timestamp") from exc
    if parsed.utcoffset() is None:
        raise MCPContractError("Naive timestamp is unsafe")
    return parsed.astimezone(UTC)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise MCPContractError("Missing identifier")
    return value


def _next_cursor(response: Mapping[str, Any]) -> str | None:
    """Extract a bounded cursor from official next-URL or next_cursor fields."""
    cursor = response.get("next_cursor")
    if cursor is None and response.get("next") is not None:
        next_url = response["next"]
        if not isinstance(next_url, str) or len(next_url) > 4096:
            raise MCPContractError("Malformed broker pagination URL")
        parsed = urlsplit(next_url)
        # Robinhood tools may return an opaque cursor directly, not a next URL.
        # Do not follow arbitrary URLs; return only a bounded cursor token.
        if (not parsed.scheme and not parsed.netloc and not parsed.query and
            re.fullmatch(r"[A-Za-z0-9_+/=-]{1,512}", next_url)):
            cursor = next_url
        else:
            query = parse_qs(parsed.query)
            cursors = query.get("cursor", [])
            if len(cursors) != 1:
                raise MCPContractError("Broker pagination cursor absent")
            cursor = cursors[0]
    if cursor is not None and (
        not isinstance(cursor, str) or not 1 <= len(cursor) <= 512
    ):
        raise MCPContractError("Malformed broker pagination cursor")
    return cursor


@dataclass(frozen=True, slots=True)
class MCPAccount:
    account_number: str
    agentic_allowed: bool
    options_level: str

    @property
    def eligible(self) -> bool:
        return self.agentic_allowed and self.options_level.lower() in {
            "option_level_2", "option_level_3", "level_2", "level_3",
        }


@dataclass(frozen=True, slots=True)
class MCPInstrument:
    option_id: str
    contract: OptionContract
    tradable: bool
    chain_id: str | None
    minimum_tick: Decimal | None = None
    multiplier: int | None = None


@dataclass(frozen=True, slots=True)
class MCPQuote:
    option_id: str
    bid: Decimal
    ask: Decimal
    observed_at_utc: datetime
    bid_size: int | None = None
    ask_size: int | None = None


@dataclass(frozen=True, slots=True)
class OrderReview:
    checked: bool
    alerts: tuple[str, ...]
    approval_required: bool
    submitted: bool = False

    @property
    def unblocked(self) -> bool:
        return self.checked and not self.alerts and not self.approval_required


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    option_id: str
    quantity: int | None
    state: str  # OPEN, CLOSED, UNKNOWN; never infer a missing row is closed
    confirmed_from_broker: bool


@dataclass(frozen=True, slots=True)
class ReadOnlyDiagnostic:
    status: str
    recognized_read_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    schema_mismatches: tuple[str, ...]
    real_writes_enabled: bool = False


class RobinhoodMCPBroker:
    """No-live-write async read facade; never implement call_tool for write names.

    This is intentionally NOT a PaperBroker subtype. PaperOrderEngine rejects it,
    even if future connection code has successfully authenticated.
    """

    def __init__(
        self, client: MCPAsyncClient | None = None, *, timeout_seconds: float = 15.0,
        approved_account_number: str | None = None,
    ):
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("MCP read timeout outside range")
        if approved_account_number is not None:
            _identifier(approved_account_number)
        self.approved_account_number = approved_account_number
        self.client = client
        self.timeout_seconds = timeout_seconds
        self._discovered: dict[str, dict[str, Any]] | None = None

    async def discover(self) -> ReadOnlyDiagnostic:
        if self.client is None:
            raise MCPContractError("MCP read client is not connected")
        try:
            advertised = await asyncio.wait_for(
                self.client.list_tools(), timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise MCPReadError("MCP tool discovery unavailable") from exc
        definitions = advertised if isinstance(advertised, list) else _obj(advertised).get("tools")
        if not isinstance(definitions, list):
            raise MCPContractError("No usable MCP tools/list response")
        seen: dict[str, dict[str, Any]] = {}
        for tool in definitions:
            item = _obj(tool)
            name = item.get("name")
            if isinstance(name, str) and name in READ_TOOLS | WRITE_TOOLS:
                if name in seen:
                    raise MCPContractError("Duplicate MCP tool advertisement")
                camel = item.get("inputSchema")
                snake = item.get("input_schema")
                if camel is not None and snake is not None and camel != snake:
                    raise MCPContractError("Conflicting MCP tool schema aliases")
                if camel is None and isinstance(snake, Mapping):
                    item["inputSchema"] = snake
                seen[name] = item
        missing = sorted(READ_TOOLS - set(seen))
        mismatches = []
        for name, required in _REQUIRED.items():
            if name not in seen:
                continue
            schema = seen[name].get("inputSchema", seen[name].get("input_schema"))
            if not isinstance(schema, Mapping):
                mismatches.append(name)
                continue
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping) or not required <= set(properties):
                mismatches.append(name)
        self._discovered = seen
        return ReadOnlyDiagnostic(
            status="READ_ONLY_SCHEMA_OK" if not missing and not mismatches
            else "READ_ONLY_SCHEMA_INCOMPLETE",
            recognized_read_tools=tuple(sorted(READ_TOOLS & set(seen))),
            missing_tools=tuple(missing), schema_mismatches=tuple(sorted(mismatches)),
        )

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_TOOLS:
            raise BrokerWritesDisabledError("MCP write tools are forbidden")
        if (
            self.approved_account_number is not None
            and name in {
                "get_portfolio", "get_option_orders", "get_option_positions",
                "review_option_order",
            }
            and arguments.get("account_number") != self.approved_account_number
        ):
            raise MCPContractError("Requested account differs from approved MCP account")
        if self._discovered is None:
            await self.discover()
        assert self._discovered is not None
        schema = self._discovered.get(name, {}).get("inputSchema", {})
        if name not in self._discovered or (name in _REQUIRED and not _REQUIRED[name] <= set(
            schema.get("properties", {}) if isinstance(schema, Mapping) else {}
        )):
            raise MCPContractError("Required read-only MCP tool/schema missing")
        if self.client is None:
            raise MCPContractError("MCP read client is not connected")
        try:
            response = await asyncio.wait_for(
                self.client.call_tool(name, arguments), timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise MCPReadError("Read-only MCP call interrupted; reconcile before retry") from exc
        return _data(response)

    async def accounts(self) -> tuple[MCPAccount, ...]:
        data = await self._call("get_accounts", {})
        result = []
        for item in _rows(data, "accounts"):
            eligible = item.get("agentic_allowed")
            if type(eligible) is not bool:
                raise MCPContractError("Agentic account flag missing")
            result.append(MCPAccount(
                _identifier(item.get("account_number")), eligible,
                str(item.get("option_level") or ""),
            ))
        return tuple(result)

    async def external_account(self) -> MCPAccount:
        accounts = await self.accounts()
        matches = [a for a in accounts if a.agentic_allowed]
        if len(matches) != 1 or not matches[0].eligible:
            raise MCPContractError("One option-approved external-agent account required")
        if (self.approved_account_number is not None
                and matches[0].account_number != self.approved_account_number):
            raise MCPContractError("Approved MCP account changed; no fallback permitted")
        return matches[0]

    async def buying_power(self, account: MCPAccount) -> Decimal:
        if not account.eligible:
            raise MCPContractError("External account lacks permission")
        data = await self._call("get_portfolio", {"account_number": account.account_number})
        buying_power = data.get("buying_power")
        if not isinstance(buying_power, Mapping):
            raise MCPContractError("Broker buying power unavailable")
        return _money(buying_power.get("buying_power"), "buying_power")

    async def resolve_instrument(self, contract: OptionContract) -> MCPInstrument:
        if contract.expiration < datetime.now(UTC).date():
            raise MCPContractError("Expired options cannot be opened")
        chains = _rows(await self._call(
            "get_option_chains", {"underlying_symbol": contract.underlying}
        ), "chains", "results")
        matching_chains = [x for x in chains if contract.expiration.isoformat() in (
            x.get("expiration_dates") or []
        ) and (x.get("underlying_symbol") or x.get("symbol")) == contract.underlying]
        if not matching_chains:
            raise MCPContractError("Verified option chain/expiration absent")
        matches: list[MCPInstrument] = []
        for chain in matching_chains:
            chain_id = _identifier(chain.get("id"))
            args = {
                "chain_id": chain_id,
                "expiration_dates": contract.expiration.isoformat(),
                "strike_price": str(contract.strike),
                "type": contract.right.value.lower(), "state": "active",
            }
            instruments: list[dict[str, Any]] = []
            seen_cursors: set[str] = set()
            for _ in range(10):
                response = await self._call("get_option_instruments", args)
                instruments.extend(_rows(response, "instruments", "results"))
                cursor = _next_cursor(response)
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise MCPContractError("Repeated contract pagination cursor")
                seen_cursors.add(cursor)
                args["cursor"] = cursor
            else:
                raise MCPContractError("Incomplete option-chain pagination")
            for item in instruments:
                option_id = item.get("id")
                try:
                    strike = _money(item.get("strike_price"), "strike_price")
                    expire = date.fromisoformat(str(item.get("expiration_date")))
                except ValueError as exc:
                    raise MCPContractError("Invalid option instrument expiry") from exc
                if (item.get("chain_id") != chain_id or expire != contract.expiration
                    or strike != contract.strike or item.get("type") != contract.right.value.lower()
                    or (item.get("chain_symbol") or item.get("underlying_symbol"))
                    != contract.underlying):
                    continue
                tradable = (
                    item.get("tradability") == "tradable"
                    and item.get("state") == "active"
                )
                # Optional vendor-advertised instrument rules; never infer a tick or multiplier.
                tick_raw = item.get("minimum_tick")
                tick = _money(tick_raw, "minimum_tick") if tick_raw is not None else None
                # Robinhood exposes a two-tier price-dependent tick schedule.
                # Without the order premium here, only a constant schedule can
                # safely be collapsed to a single minimum tick.
                tiers = item.get("min_ticks")
                if tiers is not None:
                    if not isinstance(tiers, Mapping):
                        raise MCPContractError("Malformed option tick schedule")
                    above = _money(tiers.get("above_tick"), "above_tick")
                    below = _money(tiers.get("below_tick"), "below_tick")
                    _money(tiers.get("cutoff_price"), "cutoff_price")
                    if above <= 0 or below <= 0:
                        raise MCPContractError("Nonpositive option tick")
                    if above == below:
                        if tick is not None and tick != above:
                            raise MCPContractError("Conflicting option ticks")
                        tick = above
                    else:
                        # An order-price-dependent tick cannot be checked here.
                        tick = None
                if tick is not None and tick <= 0:
                    raise MCPContractError("Nonpositive option tick")
                multiplier_raw = item.get("multiplier")
                vendor_multiplier = item.get("trade_value_multiplier")
                multiplier = None
                if multiplier_raw is not None or vendor_multiplier is not None:
                    primary = _money(multiplier_raw if multiplier_raw is not None
                                     else vendor_multiplier, "multiplier")
                    if primary < 1 or primary != primary.to_integral_value():
                        raise MCPContractError("Unknown option multiplier")
                    if vendor_multiplier is not None and multiplier_raw is not None and (
                        _money(vendor_multiplier, "trade_value_multiplier") != primary
                    ):
                        raise MCPContractError("Conflicting option multipliers")
                    multiplier = int(primary)
                matches.append(MCPInstrument(_identifier(option_id), contract, tradable,
                                             chain_id, tick, multiplier))
        if len(matches) != 1 or not matches[0].tradable:
            raise MCPContractError("Exact, unique, tradable option instrument not verified")
        return matches[0]

    async def quote(self, instrument: MCPInstrument, *, max_age_seconds: int = 30,
                    now: datetime | None = None) -> MCPQuote:
        data = await self._call("get_option_quotes", {"instrument_ids": [instrument.option_id]})
        rows = _rows(data, "quotes", "results")
        # Official quote tool returns results[].quote plus an optional close;
        # synthetic older transports return a flat quotes[] list.
        quotes = [_obj(x["quote"]) if "quote" in x else x for x in rows]
        matches = [x for x in quotes if x.get("instrument_id") == instrument.option_id]
        if len(matches) != 1:
            raise MCPContractError("Exact quote unavailable")
        bid = _money(matches[0].get("bid_price"), "bid")
        ask = _money(matches[0].get("ask_price"), "ask")
        observed = _timestamp(matches[0].get("updated_at"))
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("Current time must be timezone-aware")
        age = (current.astimezone(UTC) - observed).total_seconds()
        if age < 0 or age > max_age_seconds or ask < bid:
            raise MCPContractError("Option quote is stale or crossed")
        # Robinhood's published overview confirms quote tools, but not every
        # response field. In their absence size remains unknown, NOT infinite.
        def opt_size(name: str) -> int | None:
            val = matches[0].get(name)
            if val is None:
                return None
            if type(val) is not int or val < 0:
                raise MCPContractError("Invalid displayed option depth")
            return val
        return MCPQuote(instrument.option_id, bid, ask, observed,
                        opt_size("bid_size"), opt_size("ask_size"))

    @staticmethod
    def order_arguments(account: MCPAccount, instrument: MCPInstrument,
                        intent: OrderIntent) -> dict[str, Any]:
        if (
            not account.eligible or not instrument.tradable
            or intent.contract != instrument.contract
        ):
            raise MCPContractError("Unverified account or option contract")
        side = "buy" if intent.side is OrderSide.BUY_TO_OPEN else "sell"
        effect = "open" if intent.side is OrderSide.BUY_TO_OPEN else "close"
        return {
            "account_number": account.account_number,
            "legs": [{"option_id": instrument.option_id, "side": side,
                      "position_effect": effect, "ratio_quantity": 1}],
            "type": "limit", "quantity": str(intent.quantity),
            "price": str(intent.limit_price), "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }

    async def review_order(self, account: MCPAccount, instrument: MCPInstrument,
                           intent: OrderIntent) -> OrderReview:
        args = self.order_arguments(account, instrument, intent)
        args.update({"chain_symbol": instrument.contract.underlying, "underlying_type": "equity"})
        result = await self._call("review_option_order", args)
        checks = result.get("order_checks")
        if not isinstance(checks, list):
            raise MCPContractError("Broker review checks missing")
        problems: list[str] = []
        for check in checks:
            record = _obj(check)
            if record.get("severity") not in {"pass", "ok", "info"}:
                problems.append(str(record.get("code") or "UNKNOWN_ALERT"))
        approval = result.get("approval_required")
        if type(approval) is not bool:
            raise MCPContractError("Broker approval state not verified")
        return OrderReview(True, tuple(problems), approval)

    @staticmethod
    def future_order_payload(account: MCPAccount, instrument: MCPInstrument,
                             intent: OrderIntent) -> dict[str, Any]:
        """Schema-shaped preview; NEVER submits, even when an order is reviewed."""
        payload = RobinhoodMCPBroker.order_arguments(account, instrument, intent)
        # Deterministic UUID avoids a new broker order on a retry of same intent.
        key = uuid5(UUID("ec9692cb-8082-4fe0-9168-4855fb0d541c"), intent.idempotency_key)
        payload["ref_id"] = str(key)
        return payload

    @staticmethod
    def future_cancel_payload(account: MCPAccount, broker_order_id: str) -> dict[str, str]:
        if not account.eligible:
            raise MCPContractError("External-agent account required")
        return {"account_number": account.account_number, "order_id": _identifier(broker_order_id)}

    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        raise BrokerWritesDisabledError("All real brokerage order submission is disabled")

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise BrokerWritesDisabledError("All real brokerage cancellation is disabled")

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise BrokerWritesDisabledError("Use read-only async reconciliation; no live engine")

    async def orders(self, account: MCPAccount, *, order_id: str | None = None,
                     limit_pages: int = 3) -> list[dict[str, Any]]:
        if not account.eligible:
            raise MCPContractError("External-agent account required")
        if not 1 <= limit_pages <= 10:
            raise ValueError("Too many history pages")
        cursor = None
        seen = set()
        orders: list[dict[str, Any]] = []
        for _ in range(limit_pages):
            args = {"account_number": account.account_number}
            if order_id:
                args["order_id"] = _identifier(order_id)
            if cursor:
                args["cursor"] = cursor
            response = await self._call("get_option_orders", args)
            orders.extend(_rows(response, "orders"))
            next_cursor = _next_cursor(response)
            if not next_cursor:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise MCPContractError("Invalid pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise MCPContractError("Incomplete order-history pagination")
        return orders

    async def positions(self, account: MCPAccount, *, option_id: str | None = None,
                        include_closed: bool = False) -> list[dict[str, Any]]:
        if not account.eligible:
            raise MCPContractError("External-agent account required")
        args: dict[str, Any] = {
            "account_number": account.account_number, "nonzero": not include_closed,
        }
        if option_id:
            args["option_ids"] = _identifier(option_id)
        return _rows(await self._call("get_option_positions", args), "positions")

    async def reconcile_position(self, account: MCPAccount, option_id: str) -> PositionSnapshot:
        """Verify long position, including explicitly returned closed/zero positions."""
        option_id = _identifier(option_id)
        records = await self.positions(account, option_id=option_id, include_closed=True)
        matches = [r for r in records if r.get("option_id") == option_id]
        if not matches:
            return PositionSnapshot(option_id, None, "UNKNOWN", False)
        if len(matches) != 1:
            raise MCPContractError("Ambiguous brokerage position")
        record = matches[0]
        if record.get("type") not in {None, "long"}:
            raise MCPContractError("Only long single-leg options are supported")
        raw = record.get("quantity")
        if isinstance(raw, bool) or not str(raw).isdigit():
            raise MCPContractError("Broker position quantity not validated")
        count = int(raw)
        return PositionSnapshot(option_id, count, "OPEN" if count else "CLOSED", True)

    @staticmethod
    def normalize_order(order: Mapping[str, Any], intent: OrderIntent,
                        *, expected_broker_order_id: str | None = None
                        ) -> BrokerOrderStatus:
        broker_id = _identifier(order.get("id"))
        if expected_broker_order_id and broker_id != expected_broker_order_id:
            raise MCPContractError("Order broker ID does not match persisted intent")
        ref = order.get("ref_id")
        expected_ref = RobinhoodMCPBroker.future_order_payload(
            MCPAccount("unused", True, "option_level_2"),
            MCPInstrument("unused", intent.contract, True, None), intent,
        )["ref_id"]
        if ref != expected_ref:
            raise MCPContractError("Broker order idempotency key mismatch")
        status_map = {
            "queued": BrokerOrderState.ACCEPTED, "confirmed": BrokerOrderState.ACCEPTED,
            "partially_filled": BrokerOrderState.PARTIALLY_FILLED,
            "filled": BrokerOrderState.FILLED,
            "pending_cancelled": BrokerOrderState.CANCEL_PENDING,
            "cancelled": BrokerOrderState.CANCELED,
            "rejected": BrokerOrderState.REJECTED,
            "failed": BrokerOrderState.REJECTED,
            "voided": BrokerOrderState.REJECTED,
        }
        if order.get("state") not in status_map:
            raise MCPContractError("Unknown option order state")
        filled = order.get("filled_quantity")
        if isinstance(filled, bool) or not str(filled).isdigit():
            raise MCPContractError("Missing filled quantity")
        quantity = int(filled)
        if quantity > intent.quantity:
            raise MCPContractError("Unexpected overfill")
        fills: list[BrokerFill] = []
        for item in order.get("fills", []):
            f = _obj(item)
            qty = f.get("quantity")
            if not str(qty).isdigit() or int(qty) < 1:
                raise MCPContractError("Invalid broker fill quantity")
            fills.append(BrokerFill(
                _identifier(f.get("id")), int(qty),
                _money(f.get("price"), "fill_price"),
                _timestamp(f.get("timestamp")),
            ))
        if sum(x.quantity for x in fills) != quantity:
            raise MCPContractError("Broker fills not independently reconciled")
        normalized_state = status_map[order["state"]]
        if (normalized_state is BrokerOrderState.FILLED and quantity != intent.quantity
            or normalized_state is BrokerOrderState.PARTIALLY_FILLED
            and not 0 < quantity < intent.quantity
            or normalized_state is BrokerOrderState.ACCEPTED and quantity != 0):
            raise MCPContractError("Broker quantity conflicts with order state")
        accepted = order.get("accepted_at")
        accepted_at = _timestamp(accepted) if accepted else None
        if status_map[order["state"]] not in {BrokerOrderState.REJECTED} and accepted_at is None:
            raise MCPContractError("Broker acceptance time missing")
        return BrokerOrderStatus(
            logical_order_id=intent.logical_order_id,
            state=status_map[order["state"]], filled_quantity=quantity,
            accepted_at_utc=accepted_at, fills=tuple(fills),
            rejected_reason=str(order.get("reject_reason"))
            if status_map[order["state"]] is BrokerOrderState.REJECTED else None,
            contract=intent.contract, side=intent.side,
            requested_quantity=intent.quantity, limit_price=intent.limit_price,
            idempotency_key=intent.idempotency_key,
        )

    async def reconcile_order(self, account: MCPAccount, intent: OrderIntent,
                              *, broker_order_id: str | None = None
                              ) -> BrokerOrderStatus:
        """Never resubmit after ambiguous timeout; inspect order ID/ref only."""
        rows = await self.orders(account, order_id=broker_order_id, limit_pages=5)
        expected_ref = self.future_order_payload(
            account, MCPInstrument("identity-only", intent.contract, True, None), intent,
        )["ref_id"]
        matches = [row for row in rows if row.get("ref_id") == expected_ref]
        if len(matches) == 1:
            return self.normalize_order(
                matches[0], intent, expected_broker_order_id=broker_order_id,
            )
        if matches:
            raise MCPContractError("Multiple broker orders share an idempotency key")
        return BrokerOrderStatus(
            logical_order_id=intent.logical_order_id,
            state=BrokerOrderState.UNKNOWN, filled_quantity=0,
            accepted_at_utc=None, contract=intent.contract, side=intent.side,
            requested_quantity=intent.quantity, limit_price=intent.limit_price,
            idempotency_key=intent.idempotency_key,
        )

    async def read_only_preflight(self, contract: OptionContract, *, account: MCPAccount,
                                  known_tick: Decimal | None = None) -> Preflight:
        """Read-only snapshot for future review; missing tick/session stays DENY."""
        instrument = await self.resolve_instrument(contract)
        available = await self.buying_power(account)
        # No documented option-tick or session source established. Never invent.
        return Preflight(
            instrument_verified=instrument.tradable,
            options_permission=account.eligible, market_session_open=False,
            available_buying_power_usd=available,
            premium_tick=known_tick or Decimal("0"), contract=contract,
        )
