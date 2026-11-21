"""Offline deterministic test doubles and fixtures for live unattended trading.

Provides:
- StatefulFakeMCP: In-memory stateful MCP double with order lifecycle, deterministic ref_ids,
  dynamic fills, cancellations, and instrument/quote resolution.
- MockTelethonClient: Simulates Telegram MTProto interactive session login, 2FA, channel check,
  and message streaming.
- MockTelegramBotAPI: In-memory bot API dispatcher simulating getUpdates long-polling,
  sendMessage replies, sender allowlisting, and two-step token confirmation.
- MockProcessManager: Process supervisor double managing worker process lifecycles, health
  heartbeat writes to state/health.json, crash backoff, and circuit breaker trip detection.
- FakeClock: Deterministic timezone-aware UTC clock.
- execute_flatten_positions: Safe adapter for position liquidation and buy cancellation.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from swift_autotrader.broker.base import OrderIntent
from swift_autotrader.broker.robinhood_mcp import (
    READ_TOOLS,
    WRITE_TOOLS,
)
from swift_autotrader.notifications.kill_switch import TelegramKillSwitchBot
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.supervisor import ServiceSupervisor

# Advertised input properties of the real Robinhood MCP tools (LiveBroker verifies these).
_TOOL_PROPERTIES: dict[str, set[str]] = {
    "get_accounts": set(),
    "get_portfolio": {"account_number"},
    "get_realized_pnl": {"account_number", "start_date", "end_date"},
    "get_option_chains": {"underlying_symbol"},
    "get_option_instruments": {"chain_id", "expiration_dates", "strike_price", "type", "state"},
    "get_option_quotes": {"instrument_ids"},
    "get_option_orders": {"account_number", "order_id", "state", "created_at_gte", "cursor"},
    "get_option_positions": {"account_number", "nonzero", "option_ids", "cursor"},
    "review_option_order": {"account_number", "legs", "quantity"},
    "place_option_order": {"account_number", "ref_id", "legs", "quantity", "price", "type",
                           "time_in_force", "market_hours"},
    "cancel_option_order": {"account_number", "order_id"},
}


class SessionPasswordNeededError(Exception):
    """Simulated Telethon 2FA password requirement error."""


class FakeClock:
    """Deterministic timezone-aware UTC clock for test suites."""

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is not None:
            if initial_time.tzinfo is None:
                self._current = initial_time.replace(tzinfo=UTC)
            else:
                self._current = initial_time.astimezone(UTC)
        else:
            self._current = datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def __call__(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> datetime:
        self._current += timedelta(seconds=seconds)
        return self._current

    def set(self, target: datetime) -> None:
        if target.tzinfo is None:
            self._current = target.replace(tzinfo=UTC)
        else:
            self._current = target.astimezone(UTC)

    def monotonic(self) -> float:
        return self._current.timestamp()


class StatefulFakeMCP:
    """In-memory stateful MCP double simulating Robinhood Agentic MCP tools.

    Provides deterministic ref_id mapping, dynamic order lifecycle transitions
    (confirmed -> filled / partially_filled / cancelled), fill simulations,
    cancellations, and quote snapshots without network access.
    """

    def __init__(
        self,
        *,
        account_number: str = "synthetic-account",
        buying_power: Decimal = Decimal("10000.00"),
        omit_tools: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.account_number = account_number
        self.buying_power = buying_power
        self.clock = clock
        self.orders: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, dict[str, Any]] = {}
        self.chains: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.faults: dict[str, Exception] = {}
        self.order_counter: int = 1000
        self.names = (READ_TOOLS | WRITE_TOOLS | {"get_realized_pnl"}) - set(omit_tools)
        # Real-shape account state used by LiveBroker's realized-plus-unrealized accounting.
        self.realized_pnl = "0"
        self.realized_trades = 0
        self.orders_page_size: int | None = None  # None = one page; else paginate with `next`
        self.positions_override: list[dict[str, Any]] | None = None
        self.prior_close: dict[str, str] = {}

        self._seed_default_data()

    def _seed_default_data(self) -> None:
        # Default chain
        self.chains["SPY"] = {
            "id": "synthetic-spy-chain",
            "underlying_symbol": "SPY",
            "expiration_dates": ["2026-12-18", "2026-11-27", "2026-12-18"],
        }
        self.chains["QQQ"] = {
            "id": "synthetic-qqq-chain",
            "underlying_symbol": "QQQ",
            "expiration_dates": ["2026-12-18", "2026-11-27"],
        }
        self.chains["AAPL"] = {
            "id": "synthetic-aapl-chain",
            "underlying_symbol": "AAPL",
            "expiration_dates": ["2026-12-18"],
        }
        self.chains["NVDA"] = {
            "id": "synthetic-nvda-chain",
            "underlying_symbol": "NVDA",
            "expiration_dates": ["2026-12-18"],
        }

        # Seed SPY 680C 2026-12-18
        self.add_instrument(
            instrument_id="opt-spy-680c",
            chain_id="synthetic-spy-chain",
            symbol="SPY",
            strike="680.0000",
            expiration="2026-12-18",
            option_type="call",
            bid="1.20",
            ask="1.22",
        )

        # Seed QQQ 500C 2026-12-18
        self.add_instrument(
            instrument_id="opt-qqq-500c",
            chain_id="synthetic-qqq-chain",
            symbol="QQQ",
            strike="500.0000",
            expiration="2026-12-18",
            option_type="call",
            bid="2.00",
            ask="2.02",
        )

        # Seed AAPL 250C 2026-12-18
        self.add_instrument(
            instrument_id="opt-aapl-250c",
            chain_id="synthetic-aapl-chain",
            symbol="AAPL",
            strike="250.0000",
            expiration="2026-12-18",
            option_type="call",
            bid="1.50",
            ask="1.55",
        )

        # Seed NVDA 140C 2026-12-18
        self.add_instrument(
            instrument_id="opt-nvda-140c",
            chain_id="synthetic-nvda-chain",
            symbol="NVDA",
            strike="140.0000",
            expiration="2026-12-18",
            option_type="call",
            bid="3.00",
            ask="3.05",
        )

    def add_instrument(
        self,
        *,
        instrument_id: str,
        chain_id: str,
        symbol: str,
        strike: str,
        expiration: str,
        option_type: str,
        bid: str = "1.00",
        ask: str = "1.05",
        tick: str = "0.01",
    ) -> None:
        self.instruments[instrument_id] = {
            "id": instrument_id,
            "chain_id": chain_id,
            "chain_symbol": symbol,
            "strike_price": strike,
            "expiration_date": expiration,
            "type": option_type.lower(),
            "state": "active",
            "tradability": "tradable",
            "trade_value_multiplier": "100.0000",
            "min_ticks": {
                "above_tick": tick,
                "below_tick": tick,
                "cutoff_price": "3.00",
            },
        }
        self.set_quote(instrument_id, Decimal(bid), Decimal(ask))

    def _now_iso(self) -> str:
        return (self.clock() if self.clock else datetime.now(UTC)).isoformat()

    def set_quote(
        self,
        instrument_id: str,
        bid: Decimal | str,
        ask: Decimal | str,
        bid_size: int = 10,
        ask_size: int = 10,
        updated_at: datetime | None = None,
    ) -> None:
        now_str = updated_at.isoformat() if updated_at is not None else self._now_iso()
        self.quotes[instrument_id] = {
            "instrument_id": instrument_id,
            "bid_price": str(bid),
            "ask_price": str(ask),
            "bid_size": bid_size,
            "ask_size": ask_size,
            "updated_at": now_str,
        }

    def fail_tool(self, name: str, exception: Exception) -> None:
        self.faults[name] = exception

    def clear_faults(self) -> None:
        self.faults.clear()

    async def list_tools(self) -> SimpleNamespace:
        if "list_tools" in self.faults:
            raise self.faults["list_tools"]
        tools = []
        for name in sorted(self.names):
            required_fields = _TOOL_PROPERTIES.get(name, set())
            properties = {f: {"type": "string"} for f in required_fields}
            tools.append({
                "name": name,
                "inputSchema": {"type": "object", "properties": properties},
            })
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name in self.faults:
            raise self.faults[name]

        if name == "get_accounts":
            return {"structuredContent": {"data": {
                "accounts": [
                    {
                        "account_number": self.account_number,
                        "agentic_allowed": True,
                        "option_level": "option_level_2",
                    }
                ]
            }}}

        if name == "get_portfolio":
            return {"structuredContent": {"data": {
                "buying_power": {"buying_power": str(self.buying_power)}
            }}}

        if name == "get_option_chains":
            sym = arguments.get("underlying_symbol", "")
            chain = self.chains.get(sym)
            chains = [chain] if chain else []
            return {"structuredContent": {"data": {"chains": chains}}}

        if name == "get_option_instruments":
            chain_id = arguments.get("chain_id")
            insts = [
                inst for inst in self.instruments.values()
                if chain_id is None or inst.get("chain_id") == chain_id
            ]
            return {"structuredContent": {"data": {"instruments": insts}}}

        if name == "get_option_quotes":
            instrument_ids = arguments.get("instrument_ids", [])
            quotes = [
                self.quotes[iid] for iid in instrument_ids if iid in self.quotes
            ]
            results = []
            for quote in quotes:
                bid, ask = Decimal(quote["bid_price"]), Decimal(quote["ask_price"])
                mark = (bid + ask) / 2
                results.append({
                    "quote": dict(quote, mark_price=str(mark)),
                    "close": {"price": self.prior_close.get(quote["instrument_id"], str(mark))},
                })
            # `results` is the real shape; the flat `quotes` list is kept for older readers.
            return {"structuredContent": {"data": {"quotes": quotes, "results": results}}}

        if name == "get_realized_pnl":
            point = {"realized_gain": self.realized_pnl, "number_of_trades": self.realized_trades}
            return {"structuredContent": {"data": {
                "total_returns": self.realized_pnl, "data_points": [point]}}}

        if name == "review_option_order":
            return {"structuredContent": {"data": {
                "order_checks": [],
                "approval_required": False,
            }}}

        if name == "place_option_order":
            legs = arguments.get("legs", [])
            ref_id = arguments.get("ref_id")
            if not ref_id and legs:
                ref_id = legs[0].get("ref_id")

            # Enforce idempotency: if order with this ref_id already exists, return it
            if ref_id:
                existing = next(
                    (o for o in self.orders.values() if o.get("ref_id") == ref_id),
                    None,
                )
                if existing is not None:
                    return {"structuredContent": {"data": {"order": existing}}}

            if not ref_id:
                ref_id = str(uuid.uuid4())

            self.order_counter += 1
            broker_order_id = f"rh_ord_{self.order_counter}"

            now_iso = self._now_iso()
            order_record = {
                "id": broker_order_id,
                "ref_id": ref_id,
                "account_number": arguments.get("account_number", self.account_number),
                "state": "confirmed",
                "direction": legs[0].get("side", "buy") if legs else "buy",
                "type": "limit",
                "quantity": f"{Decimal(str(arguments.get('quantity', 1))):.4f}",
                "filled_quantity": "0",
                "processed_quantity": "0.0000",
                "agent_initiated": True,
                "placed_agent": "agent",
                "price": f"{Decimal(str(arguments.get('price', '1.00'))):.4f}",
                "legs": [dict(leg, executions=[]) for leg in legs],
                "created_at": now_iso,
                "updated_at": now_iso,
                "accepted_at": now_iso,
                "fills": [],
                "executions": [],
            }
            self.orders[broker_order_id] = order_record
            return {"structuredContent": {"data": {"order": order_record}}}

        if name == "cancel_option_order":
            order_id = arguments.get("order_id")
            if order_id in self.orders:
                self.orders[order_id]["state"] = "cancelled"
                self.orders[order_id]["updated_at"] = self._now_iso()
                return {"structuredContent": {"data": {
                    "order_id": order_id,
                    "state": "cancelled",
                }}}
            return {"structuredContent": {"data": {
                "order_id": order_id,
                "state": "unknown",
            }}}

        if name == "get_option_orders":
            account = arguments.get("account_number")
            order_id = arguments.get("order_id")
            matched = list(self.orders.values())
            if order_id:
                matched = [o for o in matched if o.get("id") == order_id]
            elif account:
                matched = [o for o in matched if o.get("account_number") == account]
            matched = [dict(o) for o in matched]
            next_url = None
            if self.orders_page_size:
                start = int(str(arguments.get("cursor", "0")).rsplit("=", 1)[-1] or 0)
                end = start + self.orders_page_size
                next_url = f"https://api.example/orders/?cursor={end}" if end < len(matched) else None
                matched = matched[start:end]
            return {"structuredContent": {"data": {"orders": matched, "next": next_url}}}

        if name == "get_option_positions":
            if self.positions_override is not None:
                return {"structuredContent": {"data": {
                    "positions": list(self.positions_override), "next": None}}}
            return {"structuredContent": {"data": {
                "positions": self._derived_positions(), "next": None}}}

        raise AssertionError(f"Unhandled MCP tool: {name}")

    def _derived_positions(self) -> list[dict[str, Any]]:
        """Real-shape long positions derived from executed buys minus executed sells."""
        held: dict[str, list[Decimal]] = {}
        for order in self.orders.values():
            leg = order["legs"][0]
            qty = Decimal(order.get("processed_quantity", "0"))
            if qty == 0:
                continue
            row = held.setdefault(leg["option_id"], [Decimal(0), Decimal(0)])
            sign = 1 if leg.get("side") == "buy" else -1
            row[0] += sign * qty
            if sign == 1:
                row[1] += qty * Decimal(order["fills"][0]["price"]) * 100
        positions = []
        for option_id, (quantity, cost) in held.items():
            if quantity > 0:
                positions.append({
                    "option_id": option_id, "type": "long", "quantity": f"{quantity:.4f}",
                    "average_price": str(cost / quantity),
                    "trade_value_multiplier": "100.0000"})
        return positions

    def fill_order(
        self,
        broker_order_id: str,
        filled_quantity: int | None = None,
        fill_price: Decimal | str | None = None,
    ) -> None:
        """Simulate broker execution fill for an order."""
        if broker_order_id not in self.orders:
            raise KeyError(f"Order not found: {broker_order_id}")
        order = self.orders[broker_order_id]
        qty = filled_quantity if filled_quantity is not None else int(order["quantity"])
        price = str(fill_price) if fill_price is not None else order.get("price", "1.20")
        now_str = self._now_iso()
        order["state"] = "filled"
        order["filled_quantity"] = str(qty)
        order["processed_quantity"] = f"{qty}.0000"
        order["updated_at"] = now_str
        fill_rec = {
            "id": f"fill_{uuid.uuid4().hex[:8]}",
            "quantity": str(qty),
            "price": price,
            "timestamp": now_str,
        }
        order["fills"] = [fill_rec]
        order["executions"] = [fill_rec]
        order["legs"][0]["executions"] = [dict(fill_rec)]

    def partial_fill(
        self,
        broker_order_id: str,
        filled_quantity: int,
        fill_price: Decimal | str | None = None,
    ) -> None:
        """Simulate broker partial execution fill."""
        if broker_order_id not in self.orders:
            raise KeyError(f"Order not found: {broker_order_id}")
        order = self.orders[broker_order_id]
        price = str(fill_price) if fill_price is not None else order.get("price", "1.20")
        now_str = self._now_iso()
        order["state"] = "partially_filled"
        order["filled_quantity"] = str(filled_quantity)
        order["processed_quantity"] = f"{filled_quantity}.0000"
        order["updated_at"] = now_str
        fill_rec = {
            "id": f"fill_{uuid.uuid4().hex[:8]}",
            "quantity": str(filled_quantity),
            "price": price,
            "timestamp": now_str,
        }
        order["fills"] = [fill_rec]
        order["executions"] = [fill_rec]
        order["legs"][0]["executions"] = [dict(fill_rec)]


class MockTelethonClient:
    """Simulates Telegram MTProto interactive session login and streaming."""

    def __init__(
        self,
        *,
        authorized: bool = False,
        two_factor_required: bool = False,
        two_factor_password: str = "cloud-pass-123",
        channel_id: int = -1001234567890,
        channel_exists: bool = True,
    ) -> None:
        self.authorized = authorized
        self.two_factor_required = two_factor_required
        self.two_factor_password = two_factor_password
        self.channel_id = channel_id
        self.channel_exists = channel_exists
        self.is_connected = False
        self.handlers: list[Callable[..., Any]] = []
        self.phone_requested: str | None = None

    async def connect(self) -> None:
        self.is_connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str) -> SimpleNamespace:
        if not phone or not phone.startswith("+"):
            raise ValueError("Phone number must include international country code")
        self.phone_requested = phone
        return SimpleNamespace(phone_code_hash="mock-phone-code-hash-abc")

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        password: str | None = None,
        *,
        phone_code_hash: str | None = None,
    ) -> SimpleNamespace:
        if code == "00000":
            raise ValueError("Invalid phone code")
        if self.two_factor_required and password is None:
            raise SessionPasswordNeededError("Two-factor authentication password required")
        if self.two_factor_required and password != self.two_factor_password:
            raise ValueError("Invalid 2FA password")
        self.authorized = True
        return SimpleNamespace(id=98765432, first_name="SwiftTrader")

    async def get_entity(self, peer_id: Any) -> SimpleNamespace:
        if not self.channel_exists:
            raise ValueError(f"Could not find entity {peer_id}")
        target_id = getattr(peer_id, "id", peer_id)
        if target_id != self.channel_id:
            raise ValueError(f"Entity {peer_id} not found in user dialogs")
        return SimpleNamespace(id=self.channel_id, title="SwiftTrades VIP Alerts")

    def on(self, event_filter: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.handlers.append(handler)
            return handler
        return decorator

    async def emit_message(
        self,
        chat_id: int,
        text: str,
        message_id: int = 100,
        *,
        edit_date: datetime | None = None,
        reply_to_id: int | None = None,
    ) -> None:
        payload = SimpleNamespace(
            id=message_id,
            date=datetime.now(UTC),
            raw_text=text,
            edit_date=edit_date,
            sender_id=42,
            reply_to=SimpleNamespace(reply_to_msg_id=reply_to_id) if reply_to_id else None,
        )
        wrapped = SimpleNamespace(chat_id=chat_id, message=payload)
        for h in self.handlers:
            res = h(wrapped)
            if asyncio.iscoroutine(res):
                await res

    async def disconnect(self) -> None:
        self.is_connected = False


class MockTelegramBotAPI(TelegramKillSwitchBot):
    """In-memory Bot API dispatcher for mobile emergency kill switch.

    Simulates getUpdates long-polling and sendMessage replies with
    chat/sender allowlisting, operational state queries, pause/resume,
    and two-step token confirmation for position liquidation.
    """

    def __init__(
        self,
        bot_token: str = "mock_bot_token_123",
        *,
        allowed_chat_ids: Sequence[int] = (123456789,),
        token_ttl_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            bot_token=bot_token,
            allowed_chat_ids=allowed_chat_ids,
            token_ttl_seconds=token_ttl_seconds,
        )


class MockProcessHandle:
    """Mock process handle representing a supervised worker."""

    def __init__(self, pid: int, cmd: list[str], env: dict[str, str]) -> None:
        self.pid = pid
        self.cmd = list(cmd)
        self.env = dict(env)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def crash(self, exit_code: int = 1) -> None:
        self.returncode = exit_code


class MockProcessManager(ServiceSupervisor):
    """Supervisor test double managing worker process lifecycles.

    Tracks worker process restarts, exponential backoff, health.json
    heartbeats, and circuit breaker trip detection without leaking OS processes.
    """

    def __init__(self, root: Path) -> None:
        self._pid_counter = 5000
        super().__init__(root, spawn_fn=self._mock_spawn)

    def _mock_spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> MockProcessHandle:
        self._pid_counter += 1
        return MockProcessHandle(self._pid_counter, cmd, env)


def execute_flatten_positions(
    engine: PaperOrderEngine,
    now: datetime,
) -> list[OrderIntent]:
    """Safely execute position liquidation and buy cancellation on an engine."""
    return engine.flatten_positions(now)


def live_journal(path: Path) -> Path:
    """Pre-create the separate durable LiveBroker journal (never overwrites)."""
    from swift_autotrader.broker.live_broker import LiveBroker

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        LiveBroker.create_journal(path)
    return path
