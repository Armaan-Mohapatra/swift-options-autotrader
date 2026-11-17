"""Opt-in, fail-closed live Robinhood MCP Broker implementation (Python 3.11).

INTEGRATION ASSUMPTIONS (MUST BE VERIFIED AGAINST THE LIVE MCP SCHEMAS):
1. The injected client has async ``list_tools()`` and ``call_tool(name, args)``.
   The project already has a read-only RobinhoodMCPBroker implementing instrument,
   quote, account, order and position reads, and immutable Broker data classes.
2. Advertised MCP writes are ``place_option_order`` and ``cancel_option_order``.
   The former accepts ``ref_id`` (a stable client order UUID), ``account_number``,
   ``legs``, ``quantity``, ``price``, ``type``, ``time_in_force`` and
   ``market_hours``; the latter accepts ``account_number`` and ``order_id``.
   Real ``get_option_orders`` responses do NOT echo the submitted ``ref_id``;
   they can return a different ref on the placed order. The journal therefore
   reconciles by account-scoped contract/side/quantity/price/agent-created
   timestamp and refuses ambiguous matches. ``ref_id`` alone is NOT assumed
   to provide server-side exactly-once semantics. No uncertain write is resent.
3. ``get_accounts`` returns ``accounts`` with ``account_number``, boolean
   ``agentic_allowed`` and ``option_level``. The service may also report
   ``type`` / ``brokerage_account_type``, but neither alone proves the dedicated
   account identity. The pinned number plus ``agentic_allowed`` is required.
4. ``get_portfolio`` (scoped to account_number) supplies ONLY
   ``buying_power: {buying_power: 'USD'}``. Daily P/L (New York day) is computed:
   realized = ``data.total_returns`` of ``get_realized_pnl`` (a null ``realized_gain``
   needs ``number_of_trades`` 0, otherwise DENY) plus unrealized from
   ``get_option_positions`` (nonzero=true, follow ``next``; ``quantity`` decimal,
   ``average_price`` already per contract, ``trade_value_multiplier``) and
   ``get_option_quotes`` (``results[].quote`` mark_price/updated_at <=30s,
   ``results[].close.price`` prior close). Prior-day holds: (mark - prior close) x
   multiplier x quantity; opened today: (mark - fill) x multiplier x quantity. Any
   missing or stale input is DENY. Same-day execution timestamps, not
   order-created timestamps, determine activity; overnight realized-basis
   ambiguity and mixed lots deny new entries.
5. ``get_option_orders`` returns paginated ``orders`` with ``id``, ``state``, ``type``,
   ``quantity``, ``price`` (per share), ``processed_quantity``, ``agent_initiated``,
   ``created_at`` and ``legs[]`` (option_id, side, position_effect, executions[]); it
   has NO ref_id, filled_quantity, fills or accepted_at (created_at stands in for
   acceptance). Orders are matched on option, side, quantity, limit, created time and
   agent_initiated; >1 candidate is ambiguous and an unknown outcome is never
   resent. Pagination must terminate within 10 pages. Absence NEVER proves no order.
6. ``get_option_positions`` is authoritative for long quantity. Instrument
   ``multiplier`` and ``minimum_tick`` are mandatory. ``review_option_order``
   must be unblocked; order payload must match the reviewed payload.
7. Option premiums are per share, USD, and contracts have an explicit
   multiplier (usually 100). A 10% quote deviation is a REJECTION guard;
   original entry limit is NOT raised, preserving the existing no-chase rule.
8. This synchronous structural Broker cannot be called inside a running async
   loop. Invoke from a worker thread if integrating with an async orchestrator.
9. Parser-level ADD/ADDING MORE alerts are NOT distinguishable in OrderIntent.
   Inject a trusted ``signal_gate(signal_id)`` returning exactly ``ENTRY``
   only for original eligible entries; absent/ADD/metadata signals are DENIED.
   Call ``review_expired_entries()`` periodically (e.g. once a minute). It
   cancels remaining entry quantities at >=60 minutes, never liquidates fills.
10. Daily loss stop is latched for the America/New_York date on first
    observed breach, including after restarts; Windows requires IANA tzdata.
    Schedule ``refresh_daily_risk()`` regularly to detect intraday breaches;
    infrequent monitoring can miss a loss that subsequently recovers.
11. DB path must be on a durable local volume shared by every broker worker.
    Back up the journal; an unavailable/lost journal is a hard stop. Do not use
    multiple machines writing separate journal copies. Live writes start OFF.

Do not execute this module against a real client until all assumptions, schema,
account segregation, authorization, and emergency stop behavior are verified.
No credentials are inspected or logged. The module has no CLI/autorun.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from swift_autotrader.broker.base import BrokerFill, BrokerOrderState, BrokerOrderStatus
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    MCPAsyncClient,
    MCPContractError,
    MCPInstrument,
    MCPReadError,
    RobinhoodMCPBroker,
    _data,
    _next_cursor,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.risk_limits import (
    DAILY_NET_LOSS_LIMIT_USD,
    MAX_CONTRACTS_PER_ALERT,
    PREMIUM_CEILING_USD,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight

_CLIENT_NS = UUID('ec9692cb-8082-4fe0-9168-4855fb0d541c')
_STATES = {
    'queued': BrokerOrderState.ACCEPTED, 'unconfirmed': BrokerOrderState.ACCEPTED,
    'confirmed': BrokerOrderState.ACCEPTED,
    'partially_filled': BrokerOrderState.PARTIALLY_FILLED,
    'filled': BrokerOrderState.FILLED, 'rejected': BrokerOrderState.REJECTED,
    'cancelled': BrokerOrderState.CANCELED, 'failed': BrokerOrderState.REJECTED,
    'voided': BrokerOrderState.REJECTED,
}
_CREATED_SKEW_SECONDS = 120  # tolerated broker-vs-local clock difference for created_at
_TERMINAL = {BrokerOrderState.FILLED, BrokerOrderState.CANCELED,
             BrokerOrderState.REJECTED}


class LiveSafetyError(RuntimeError):
    """Missing evidence/authorization; no new live order is allowed."""


@dataclass(frozen=True)
class _Record:
    intent: OrderIntent
    ref_id: str
    account: str
    option_id: str
    multiplier: int
    created: datetime
    state: BrokerOrderState
    filled: int
    fills: tuple[BrokerFill, ...]
    accepted: datetime | None
    rejected_reason: str | None
    broker_id: str | None
    cancel_requested: bool
    write_attempted: bool = False

    @property
    def released(self) -> bool:
        """Reserved but provably never sent: terminal, frees buying power and slot."""
        return (self.state is BrokerOrderState.REJECTED and self.broker_id is None
                and not self.write_attempted)


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LiveSafetyError('timezone-aware datetime required')
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise LiveSafetyError('timestamp unavailable')
    try:
        return _now(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except ValueError as exc:
        raise LiveSafetyError('invalid broker timestamp') from exc


def _decimal(value: Any, *, signed: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise LiveSafetyError('required exact decimal missing')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise LiveSafetyError('invalid decimal') from exc
    if not amount.is_finite() or (not signed and amount < 0):
        raise LiveSafetyError('invalid financial amount')
    return amount


def _whole(value: Any) -> int:
    """A decimal string such as "1.0000" that must be a whole, nonnegative number."""
    amount = _decimal(value)
    if amount != amount.to_integral_value():
        raise LiveSafetyError('invalid contract count')
    return int(amount)


class _LoopThread:
    """One long-lived event loop in a worker thread, so the MCP client keeps one loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever,
                                       name='live-broker-loop', daemon=True)
        self.thread.start()

    def run(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if threading.current_thread() is not self.thread:
                return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
        coro.close()
        raise LiveSafetyError('synchronous Broker called inside an event loop')

    def close(self) -> None:
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        if not self.loop.is_running():
            self.loop.close()


def _signal(intent: OrderIntent) -> str:
    return intent.logical_order_id.split(':', 1)[0]


def _serialize(intent: OrderIntent) -> str:
    return json.dumps({
        'logical_order_id': intent.logical_order_id,
        'idempotency_key': intent.idempotency_key,
        'underlying': intent.contract.underlying,
        'right': intent.contract.right.value,
        'strike': str(intent.contract.strike),
        'expiration': intent.contract.expiration.isoformat(),
        'side': intent.side.value, 'quantity': intent.quantity,
        'limit_price': str(intent.limit_price),
    }, sort_keys=True, separators=(',', ':'))


def _deserialize(encoded: str) -> OrderIntent:
    from datetime import date
    d = json.loads(encoded)
    return OrderIntent(d['logical_order_id'], d['idempotency_key'],
                       OptionContract(d['underlying'], OptionRight(d['right']),
                                      Decimal(d['strike']), date.fromisoformat(d['expiration'])),
                       OrderSide(d['side']), d['quantity'], Decimal(d['limit_price']))


class LiveBroker:
    """Synchronous Broker; all I/O is via the injected asynchronous MCP client."""

    def __init__(self, db_path: Path, client: MCPAsyncClient, *,
                 account_number: str,
                 writes_enabled: bool = False,
                 kill_switch: Callable[[], bool] | None = None,
                 signal_gate: Callable[[str], str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 timeout_seconds: float = 15.0,
                 loop: _LoopThread | None = None,
                 rehearsal: bool = False) -> None:
        if not account_number or not isinstance(account_number, str):
            raise ValueError('pin a specific dedicated MCP account number')
        if not 1 <= timeout_seconds <= 60:
            raise ValueError('invalid MCP timeout')
        self.path = Path(db_path)
        self.client = client
        self.account_number = account_number
        # Rehearsal never writes: the flag forces writes off and _call_write refuses.
        self.rehearsal = rehearsal
        self.writes_enabled = writes_enabled and not rehearsal
        self.kill_switch = kill_switch or (lambda: True)  # default: killed
        self.signal_gate = signal_gate  # default: deny entries without signal lineage
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_seconds = timeout_seconds
        self.reader = RobinhoodMCPBroker(client, timeout_seconds=timeout_seconds)
        if not self.path.is_file():
            raise LiveSafetyError('pre-create the durable journal; missing journal DENY')
        self._init_schema()
        # A caller may pass the loop its MCP client already lives on.
        self._owns_loop = loop is None
        self._loop = loop or _LoopThread()

    def run(self, coro: Any) -> Any:
        """Run a coroutine on this broker's loop (create/connect the MCP client here)."""
        return self._loop.run(coro)

    def close(self) -> None:
        if self._owns_loop:
            self._loop.close()

    def rehearse(self, intent: OrderIntent) -> dict[str, Any]:
        """Full entry preflight, read-only: report what WOULD be sent. Never writes."""
        if not isinstance(intent, OrderIntent):
            raise TypeError('OrderIntent required')
        return self.run(self._rehearse(intent))

    async def _rehearse(self, intent: OrderIntent) -> dict[str, Any]:
        contract = intent.contract
        result: dict[str, Any] = {
            'logical_order_id': intent.logical_order_id,
            'contract': f'{contract.underlying} {contract.right.value} {contract.strike} '
                        f'{contract.expiration.isoformat()}',
            'quantity': intent.quantity, 'limit_price': str(intent.limit_price)}
        try:
            account, instrument, available = await self._preflight(intent, rehearsal=True)
            payload = self.reader.future_order_payload(account, instrument, intent)
        except Exception as exc:
            known = isinstance(exc, (LiveSafetyError, MCPContractError, MCPReadError))
            result.update(status='DENIED',
                          reason=(f'{type(exc).__name__}: {exc}' if known
                                  else type(exc).__name__)[:200])
            return result
        payload['account_number'] = f'...{account.account_number[-4:]}'  # never the full number
        result.update(status='WOULD_SEND', buying_power=str(available),
                      would_send={'tool': 'place_option_order', 'arguments': payload})
        return result

    def verify_account(self) -> bool:
        """Read-only startup check: the pinned account exists and is agentic_allowed."""
        self.run(self._account())
        return True

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA synchronous=FULL')
            with conn:
                yield conn
        finally:
            conn.close()  # a leaked handle would keep the journal file locked on Windows

    def _init_schema(self) -> None:
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS live_orders(
              logical_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE,
              signal_id TEXT NOT NULL, side TEXT NOT NULL,
              intent_json TEXT NOT NULL, client_ref TEXT NOT NULL UNIQUE,
              account TEXT NOT NULL, option_id TEXT NOT NULL,
              multiplier INTEGER NOT NULL CHECK(multiplier > 0), created TEXT NOT NULL,
              state TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
              fills_json TEXT NOT NULL DEFAULT '[]', accepted TEXT,
              rejected_reason TEXT, broker_id TEXT UNIQUE,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              write_attempted INTEGER NOT NULL DEFAULT 0,
              UNIQUE(signal_id, side))''')
            db.execute('''CREATE TABLE IF NOT EXISTS live_risk_latches(
              session_date TEXT PRIMARY KEY, triggered_at TEXT NOT NULL)''')

    @staticmethod
    def create_journal(path: Path) -> None:
        """Explicit local setup only. Never deletes or truncates an existing DB."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            raise FileExistsError('will not overwrite existing live journal')
        connection = sqlite3.connect(str(p))
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> _Record:
        fills = tuple(BrokerFill(f['id'], int(f['qty']), Decimal(f['price']),
                                 _parse_time(f['at']))
                      for f in json.loads(row['fills_json']))
        return _Record(_deserialize(row['intent_json']), row['client_ref'],
                       row['account'], row['option_id'], int(row['multiplier']),
                       _parse_time(row['created']),
                       BrokerOrderState(row['state']), int(row['filled']), fills,
                       _parse_time(row['accepted']) if row['accepted'] else None,
                       row['rejected_reason'], row['broker_id'],
                       bool(row['cancel_requested']), bool(row['write_attempted']))

    def _load(self, logical_id: str) -> _Record | None:
        with self._db() as db:
            row = db.execute('SELECT * FROM live_orders WHERE logical_id=?',
                             (logical_id,)).fetchone()
            return self._record(row) if row else None

    def _status(self, record: _Record, *, unknown: bool = False) -> BrokerOrderStatus:
        return BrokerOrderStatus(
            logical_order_id=record.intent.logical_order_id,
            state=BrokerOrderState.UNKNOWN if unknown else record.state,
            filled_quantity=record.filled,
            accepted_at_utc=record.accepted,
            fills=record.fills,
            rejected_reason=None if unknown else record.rejected_reason,
            contract=record.intent.contract, side=record.intent.side,
            requested_quantity=record.intent.quantity,
            limit_price=record.intent.limit_price,
            idempotency_key=record.intent.idempotency_key,
        )

    def _reserve(self, intent: OrderIntent, *, option_id: str,
                 multiplier: int, available: Decimal | None = None) -> tuple[_Record, bool]:
        ref = str(uuid5(_CLIENT_NS, intent.idempotency_key))
        payload = _serialize(intent)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM live_orders WHERE logical_id=? OR idem_key=? '
                             'OR (signal_id=? AND side=?)',
                             (intent.logical_order_id, intent.idempotency_key,
                              _signal(intent), intent.side.value)).fetchone()
            reopen = False
            if row:
                existing = self._record(row)
                if (row['intent_json'] != payload or existing.account != self.account_number):
                    raise LiveSafetyError('conflicting signal/order/idempotency identity')
                if not existing.released:
                    return existing, False
                # Never sent: the slot is free again, but an expired entry is never reopened.
                if (intent.side is OrderSide.BUY_TO_OPEN and
                        _now(self.clock()) - existing.created >= timedelta(minutes=60)):
                    raise LiveSafetyError('expired unsent entry cannot be reopened')
                reopen = True
            if intent.side is OrderSide.BUY_TO_OPEN:
                if available is None:
                    raise LiveSafetyError('buying power reservation missing')
                active = db.execute('SELECT intent_json, multiplier FROM live_orders '
                                    'WHERE side=? AND state IN (?,?,?,?)',
                                    (OrderSide.BUY_TO_OPEN.value,
                                     BrokerOrderState.UNKNOWN.value,
                                     BrokerOrderState.ACCEPTED.value,
                                     BrokerOrderState.PARTIALLY_FILLED.value,
                                     BrokerOrderState.CANCEL_PENDING.value)).fetchall()
                reserved = sum((_deserialize(r['intent_json']).limit_price *
                                Decimal(r['multiplier']) for r in active), Decimal(0))
                if available < reserved + intent.limit_price * Decimal(multiplier):
                    raise LiveSafetyError('atomic buying power reservation denied')
            if reopen:
                db.execute('UPDATE live_orders SET state=?, rejected_reason=NULL '
                           'WHERE logical_id=?', (BrokerOrderState.UNKNOWN.value,
                                                  intent.logical_order_id))
            else:
                db.execute('''INSERT INTO live_orders
                  (logical_id, idem_key, signal_id, side, intent_json, client_ref,
                   account, option_id, multiplier, created, state)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                  (intent.logical_order_id, intent.idempotency_key, _signal(intent),
                   intent.side.value, payload, ref, self.account_number, option_id,
                   multiplier, _now(self.clock()).isoformat(), BrokerOrderState.UNKNOWN.value))
        result = self._load(intent.logical_order_id)
        if result is None:
            raise LiveSafetyError('journal record vanished')
        return result, True

    def _check_loss_latch(self, net_pnl: Decimal, *, persist: bool = True) -> None:
        session = self._session_date()
        breached = net_pnl <= -DAILY_NET_LOSS_LIMIT_USD
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if breached and persist:
                db.execute('INSERT OR IGNORE INTO live_risk_latches '
                           '(session_date, triggered_at) VALUES(?, ?)',
                           (session, _now(self.clock()).isoformat()))
            stopped = db.execute('SELECT 1 FROM live_risk_latches WHERE session_date=?',
                                 (session,)).fetchone() is not None
        if stopped or (breached and not persist):
            raise LiveSafetyError('daily realized+unrealized net loss stop latched')

    def _check_write(self, *, entry: bool, writes_required: bool = True) -> None:
        if writes_required and not self.writes_enabled:
            raise LiveSafetyError('live writes disabled')
        if entry and self.kill_switch():
            raise LiveSafetyError('entry kill switch active')

    async def _account(self) -> MCPAccount:
        raw = await self.reader._call('get_accounts', {})
        rows = raw.get('accounts')
        if not isinstance(rows, list):
            raise LiveSafetyError('account list unavailable')
        matches = [x for x in rows if isinstance(x, Mapping) and
                   x.get('account_number') == self.account_number]
        if len(matches) != 1:
            raise LiveSafetyError('pinned account absent/ambiguous')
        item = matches[0]
        # The pinned account number is the identity; the MCP exposes no account_type.
        if item.get('agentic_allowed') is not True:
            raise LiveSafetyError('pinned account is not agentic_allowed')
        account = MCPAccount(self.account_number, True, str(item.get('option_level', '')))
        if not account.eligible:
            raise LiveSafetyError('account not options-approved')
        return account

    async def _tool_schema(self, name: str, mandatory: set[str]) -> None:
        try:
            advertised = await asyncio.wait_for(self.client.list_tools(),
                                                timeout=self.timeout_seconds)
        except Exception as exc:
            raise LiveSafetyError('MCP tool discovery failed') from exc
        tools = advertised if isinstance(advertised, list) else (
            advertised.get('tools') if isinstance(advertised, Mapping) else None)
        if not isinstance(tools, list):
            raise LiveSafetyError('tool list contract missing')
        matches = [t for t in tools if isinstance(t, Mapping) and t.get('name') == name]
        if len(matches) != 1:
            raise LiveSafetyError(f'tool {name} unavailable or ambiguous')
        schema = matches[0].get('inputSchema', matches[0].get('input_schema'))
        if not isinstance(schema, Mapping) or not isinstance(schema.get('properties'), Mapping):
            raise LiveSafetyError(f'tool {name} schema unavailable')
        if not mandatory <= set(schema['properties']):
            raise LiveSafetyError(f'tool {name} schema incompatible; refuses to guess')

    async def _portfolio(self) -> Decimal:
        """Buying power only; P/L comes from _daily_pnl."""
        value = await self.reader._call('get_portfolio',
                                        {'account_number': self.account_number})
        if value.get('account_number', self.account_number) != self.account_number:
            raise LiveSafetyError('portfolio account not independently confirmed')
        bp = value.get('buying_power')
        if not isinstance(bp, Mapping):
            raise LiveSafetyError('buying power unavailable')
        return _decimal(bp.get('buying_power'))

    async def _realized(self, today: str) -> Decimal:
        """data.total_returns for today's window; null gains must come from zero trades."""
        args = {'account_number': self.account_number, 'start_date': today, 'end_date': today}
        await self._tool_schema('get_realized_pnl', set(args))
        try:
            data = _data(await asyncio.wait_for(self.client.call_tool('get_realized_pnl', args),
                                                timeout=self.timeout_seconds))
        except Exception as exc:
            raise LiveSafetyError('realized P/L unavailable') from exc
        total = _decimal(data.get('total_returns'), signed=True)
        points = data.get('data_points')
        if not isinstance(points, list):
            raise LiveSafetyError('realized P/L data points unavailable')
        for point in points:
            if not isinstance(point, Mapping):
                raise LiveSafetyError('realized P/L data point malformed')
            if point.get('realized_gain') is None:
                # null + zero trades is zero; null + trades (or unknown trade count) is DENY
                if _whole(point.get('number_of_trades')) > 0:
                    raise LiveSafetyError('realized gain null although trades occurred')
            else:
                _decimal(point.get('realized_gain'), signed=True)
        return total

    async def _positions(self) -> list[Mapping[str, Any]]:
        """All nonzero option positions, following `next` until exhausted."""
        await self._tool_schema('get_option_positions', {'account_number', 'nonzero'})
        args: dict[str, Any] = {'account_number': self.account_number, 'nonzero': True}
        rows: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        try:
            for _ in range(10):
                data = await self.reader._call('get_option_positions', args)
                page = data.get('positions')
                if not isinstance(page, list) or not all(isinstance(p, Mapping) for p in page):
                    raise LiveSafetyError('positions response malformed')
                rows.extend(page)
                cursor = _next_cursor(data)
                if cursor is None:
                    return rows
                if cursor in seen:
                    raise LiveSafetyError('repeated positions pagination cursor')
                seen.add(cursor)
                args = dict(args, cursor=cursor)
        except LiveSafetyError:
            raise
        except Exception as exc:
            raise LiveSafetyError('open positions unavailable') from exc
        raise LiveSafetyError('incomplete positions pagination')

    async def _quotes(self, option_ids: list[str]) -> dict[str, dict[str, Decimal | None]]:
        """data.results[].quote (fresh <=30s) plus data.results[].close.price (prior close)."""
        try:
            data = await self.reader._call('get_option_quotes', {'instrument_ids': option_ids})
        except Exception as exc:
            raise LiveSafetyError('option quotes unavailable') from exc
        results = data.get('results')
        if not isinstance(results, list):
            raise LiveSafetyError('option quotes malformed')
        now = _now(self.clock())
        out: dict[str, dict[str, Decimal | None]] = {}
        for row in results:
            quote = row.get('quote') if isinstance(row, Mapping) else None
            if not isinstance(quote, Mapping) or not isinstance(quote.get('instrument_id'), str):
                raise LiveSafetyError('option quote malformed')
            age = (now - _parse_time(quote.get('updated_at'))).total_seconds()
            if not 0 <= age <= 30:
                raise LiveSafetyError('stale option quote')
            close = row.get('close')
            out[quote['instrument_id']] = {
                'bid': _decimal(quote.get('bid_price')), 'ask': _decimal(quote.get('ask_price')),
                'mark': _decimal(quote.get('mark_price')),
                'close': _decimal(close.get('price')) if isinstance(close, Mapping)
                and close.get('price') is not None else None}
        return out

    async def _today_activity(
            self, account: MCPAccount, today: str,
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Classify fills by execution time, not the order's creation time.

        Unverified filled-order evidence, unsupported legs and missing execution
        timestamps deny new entries rather than silently undercounting daily P/L.
        """
        bought: dict[str, Decimal] = {}
        sold: dict[str, Decimal] = {}
        for order in await self.reader.orders(account, limit_pages=10):
            processed = _decimal(order.get('processed_quantity'))
            if processed == 0:
                continue
            legs = order.get('legs')
            if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
                raise LiveSafetyError('filled multi-leg order cannot establish daily risk')
            leg = legs[0]
            option_id = leg.get('option_id')
            if not isinstance(option_id, str) or not option_id:
                raise LiveSafetyError('filled order option identity missing')
            direction = (leg.get('side'), leg.get('position_effect'))
            if direction not in {('buy', 'open'), ('sell', 'close')}:
                raise LiveSafetyError('unsupported filled option order for daily risk')
            executions = leg.get('executions')
            if not isinstance(executions, list):
                raise LiveSafetyError('filled order executions unavailable for daily risk')
            total = Decimal(0)
            for execution in executions:
                if not isinstance(execution, Mapping):
                    raise LiveSafetyError('invalid daily-risk execution')
                quantity = _decimal(execution.get('quantity'))
                if quantity <= 0 or quantity != int(quantity):
                    raise LiveSafetyError('invalid daily-risk fill quantity')
                total += quantity
                if self._ny_date(_parse_time(execution.get('timestamp'))) != today:
                    continue
                target = bought if direction == ('buy', 'open') else sold
                target[option_id] = target.get(option_id, Decimal(0)) + quantity
            if total != processed:
                raise LiveSafetyError('executions do not reconcile to daily-risk order')
        return bought, sold

    async def _daily_pnl(self, account: MCPAccount) -> Decimal:
        """Today's realized total + unrealized on open longs; fails closed on any gap.

        Held from a prior day: (mark - prior close) x multiplier x quantity.
        Opened today: (mark - fill) x multiplier x quantity, where the fill is the
        position's average_price, which is ALREADY per contract (so it is not scaled).
        """
        today = self._session_date()
        realized = await self._realized(today)
        held = [p for p in await self._positions() if _decimal(p.get('quantity')) > 0]
        try:
            bought, sold = await self._today_activity(account, today)
        except LiveSafetyError:
            raise
        except Exception as exc:
            raise LiveSafetyError('today order history unavailable') from exc
        # A prior-day long closed today may report lifetime P/L rather than
        # P/L measured from yesterday's close. Never assume the two are equal.
        if any(sold.get(option_id, Decimal(0)) > bought.get(option_id, Decimal(0))
               for option_id in set(bought) | set(sold)):
            raise LiveSafetyError('overnight close daily P/L attribution unverified')
        if not held:
            return realized
        option_ids = [str(p.get('option_id')) for p in held]
        quotes = await self._quotes(option_ids)
        unrealized = Decimal(0)
        for position, option_id in zip(held, option_ids, strict=True):
            quantity = _decimal(position.get('quantity'))
            multiplier = _decimal(position.get('trade_value_multiplier'))
            quote = quotes.get(option_id)
            if position.get('type') != 'long' or multiplier <= 0 or quote is None:
                raise LiveSafetyError('open position cannot be marked to a current quote')
            mark = quote['mark']
            assert mark is not None
            opened_today = bought.get(option_id, Decimal(0))
            closed_today = sold.get(option_id, Decimal(0))
            prior_inventory = quantity - opened_today + closed_today
            if prior_inventory < 0:
                raise LiveSafetyError('current position and daily orders disagree')
            if prior_inventory > 0 and (opened_today > 0 or closed_today > 0):
                raise LiveSafetyError('position mixes prior-day and today lots; cannot mark')
            if opened_today == 0:
                prior_close = quote['close']
                if prior_close is None:
                    raise LiveSafetyError('prior close unavailable for held position')
                unrealized += (mark - prior_close) * multiplier * quantity
            else:
                average = _decimal(position.get('average_price'))  # per contract already
                unrealized += (mark * multiplier - average) * quantity
        return realized + unrealized

    def _ny_date(self, moment: datetime) -> str:
        try:
            return _now(moment).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        except ZoneInfoNotFoundError as exc:
            raise LiveSafetyError('IANA timezone database is unavailable') from exc

    def _session_date(self) -> str:
        return self._ny_date(self.clock())

    async def _preflight(
            self, intent: OrderIntent, *, rehearsal: bool = False,
    ) -> tuple[MCPAccount, MCPInstrument, Decimal | None]:
        # Rehearsal runs every check except "are writes enabled"; nothing is persisted.
        self._check_write(entry=intent.side is OrderSide.BUY_TO_OPEN,
                          writes_required=not rehearsal)
        account = await self._account()
        instrument = await self.reader.resolve_instrument(intent.contract)
        if instrument.multiplier is None or instrument.minimum_tick is None:
            raise LiveSafetyError('option multiplier or tick unverified')
        if (instrument.minimum_tick <= 0 or
                intent.limit_price % instrument.minimum_tick != 0):
            raise LiveSafetyError('off-tick option price')
        available: Decimal | None = None
        if intent.side is OrderSide.BUY_TO_OPEN:
            if (self.signal_gate is None
                    or str(self.signal_gate(_signal(intent))).upper() != 'ENTRY'):
                raise LiveSafetyError(
                    'entry is not verified as an original ENTRY signal (ADD denied)')
            if intent.quantity != MAX_CONTRACTS_PER_ALERT:
                raise LiveSafetyError('entries must be exactly one long contract')
            available = await self._portfolio()
            self._check_loss_latch(await self._daily_pnl(account), persist=not rehearsal)
            total = intent.limit_price * Decimal(instrument.multiplier)
            if total > PREMIUM_CEILING_USD:
                raise LiveSafetyError(f'entry exceeds ${PREMIUM_CEILING_USD} maximum premium')
            quote = (await self._quotes([instrument.option_id])).get(instrument.option_id)
            if quote is None or quote['ask'] is None or quote['bid'] is None \
                    or quote['ask'] < quote['bid']:
                raise LiveSafetyError('current option quote unavailable or crossed')
            if quote['ask'] <= 0 or quote['ask'] > intent.limit_price * Decimal('1.10'):
                raise LiveSafetyError('ask exceeds 10% original alert tolerance')
            # Conservative reserve against duplicate simultaneous buying power uses.
            with self._db() as db:
                rows = db.execute('SELECT intent_json, multiplier, state FROM live_orders WHERE '
                                  'side=? AND state IN (?,?,?,?)',
                                  (OrderSide.BUY_TO_OPEN.value,
                                   BrokerOrderState.UNKNOWN.value, BrokerOrderState.ACCEPTED.value,
                                   BrokerOrderState.PARTIALLY_FILLED.value,
                                   BrokerOrderState.CANCEL_PENDING.value)).fetchall()
            outstanding = sum((_deserialize(r['intent_json']).limit_price *
                               Decimal(r['multiplier']) for r in rows), Decimal(0))
            if available < total + outstanding:
                raise LiveSafetyError('insufficient verified buying power')
        else:
            # Full exit for this signal, never short and never close a different signal.
            with self._db() as db:
                entries = db.execute('SELECT * FROM live_orders WHERE signal_id=? AND side=?',
                                     (_signal(intent), OrderSide.BUY_TO_OPEN.value)).fetchall()
            if len(entries) != 1:
                raise LiveSafetyError('exit has no unique owned entry')
            entry = self._record(entries[0])
            if entry.intent.contract != intent.contract or entry.option_id != instrument.option_id:
                raise LiveSafetyError('exit contract differs from owned entry')
            # Outstanding entries could still fill AFTER an exit; refuse until terminal.
            entry_status = await self._reconcile(entry)
            if entry_status.state not in _TERMINAL or entry_status.filled_quantity < 1:
                raise LiveSafetyError('entry not conclusively finished/filled')
            if intent.quantity != entry_status.filled_quantity:
                raise LiveSafetyError('exit must liquidate full signal quantity')
            held = [p for p in await self._positions()
                    if p.get('option_id') == instrument.option_id]
            if len(held) != 1 or held[0].get('type') != 'long' or \
                    _decimal(held[0].get('quantity')) < intent.quantity:
                raise LiveSafetyError('broker long position not confirmed')
            # Aggregate positions do not distinguish manually held contracts.
            # Verify broker quantity equals the full journal-owned inventory for
            # this option across all signals, not merely >= this exit amount.
            with self._db() as db:
                ownership = db.execute(
                    'SELECT side,filled FROM live_orders WHERE option_id=?',
                    (instrument.option_id,)).fetchall()
            expected = sum((int(row['filled']) * (
                1 if row['side'] == OrderSide.BUY_TO_OPEN.value else -1)
                for row in ownership), 0)
            if expected <= 0 or _decimal(held[0].get('quantity')) != expected:
                raise LiveSafetyError('position has untracked/manual or uncertain holdings')
        review = await self.reader.review_order(account, instrument, intent)
        if not review.unblocked:
            raise LiveSafetyError('broker option order review not approved')
        await self._tool_schema('place_option_order', {
            'ref_id', 'account_number', 'legs', 'quantity', 'price', 'type',
            'time_in_force', 'market_hours'})
        return account, instrument, available

    def _assert_exact_order(self, item: Mapping[str, Any], record: _Record) -> None:
        intent = record.intent
        # There is no ref_id/account field to compare: the request is account-scoped.
        if item.get('account_number', record.account) != record.account:
            raise LiveSafetyError('broker order account mismatch')
        if not isinstance(item.get('id'), str) or not item['id']:
            raise LiveSafetyError('broker order id missing')
        if item.get('agent_initiated') is not True:
            raise LiveSafetyError('broker order was not agent initiated')
        if (_decimal(item.get('quantity')) != Decimal(intent.quantity) or
                _decimal(item.get('price')) != intent.limit_price or
                item.get('type') != 'limit'):
            raise LiveSafetyError('broker order quantity or limit differs')
        legs = item.get('legs')
        if not isinstance(legs, list) or len(legs) != 1:
            raise LiveSafetyError('broker order has unexpected option legs')
        leg = legs[0]
        if not isinstance(leg, Mapping) or leg.get('option_id') != record.option_id or \
           leg.get('side') != ('buy' if intent.side is OrderSide.BUY_TO_OPEN else 'sell') or \
           leg.get('position_effect') != (
                   'open' if intent.side is OrderSide.BUY_TO_OPEN else 'close'):
            raise LiveSafetyError('broker option leg identity mismatch')

    @staticmethod
    def _normalize_order(item: Mapping[str, Any], record: _Record) -> BrokerOrderStatus:
        """Real get_option_orders shape -> BrokerOrderStatus (no ref_id/fills/accepted_at)."""
        intent = record.intent
        raw_state = item.get('state')
        state = _STATES.get(raw_state) if isinstance(raw_state, str) else None
        if state is None:
            raise LiveSafetyError('unknown broker order state')
        processed = _whole(item.get('processed_quantity'))
        if processed > intent.quantity:
            raise LiveSafetyError('unexpected overfill')
        executions = item['legs'][0].get('executions')
        # The broker can omit executions on an unfilled pending/cancelled order.
        # Zero processed quantity proves that an empty execution list is correct;
        # positive processed quantity without executions must still fail closed.
        if executions is None and processed == 0:
            executions = []
        if not isinstance(executions, list):
            raise LiveSafetyError('order executions missing')
        fills = []
        for execution in executions:
            if not isinstance(execution, Mapping) or not isinstance(execution.get('id'), str):
                raise LiveSafetyError('invalid broker execution')
            quantity = _whole(execution.get('quantity'))
            if quantity < 1:
                raise LiveSafetyError('invalid broker execution quantity')
            fills.append(BrokerFill(execution['id'], quantity, _decimal(execution.get('price')),
                                    _parse_time(execution.get('timestamp'))))
        if sum(f.quantity for f in fills) != processed:
            raise LiveSafetyError('broker executions do not reconcile to processed quantity')
        if (state is BrokerOrderState.FILLED and processed != intent.quantity
                or state is BrokerOrderState.PARTIALLY_FILLED
                and not 0 < processed < intent.quantity
                or state is BrokerOrderState.ACCEPTED and processed != 0):
            raise LiveSafetyError('broker quantity conflicts with order state')
        created = _parse_time(item.get('created_at'))
        return BrokerOrderStatus(
            logical_order_id=intent.logical_order_id, state=state, filled_quantity=processed,
            # No accepted_at exists: created_at is the acceptance-time proxy.
            accepted_at_utc=None if state is BrokerOrderState.REJECTED else created,
            fills=tuple(fills),
            rejected_reason=f'broker state {raw_state}'
            if state is BrokerOrderState.REJECTED else None,
            contract=intent.contract, side=intent.side, requested_quantity=intent.quantity,
            limit_price=intent.limit_price, idempotency_key=intent.idempotency_key)

    def _persist_status(self, logical_id: str, status: BrokerOrderStatus,
                        broker_id: str | None) -> _Record:
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prev = db.execute('SELECT * FROM live_orders WHERE logical_id=?',
                              (logical_id,)).fetchone()
            if prev is None:
                raise LiveSafetyError('journal record missing during reconciliation')
            before = self._record(prev)
            if status.filled_quantity < before.filled:
                raise LiveSafetyError('broker fills regressed')
            if before.broker_id and broker_id and before.broker_id != broker_id:
                raise LiveSafetyError('broker order ID changed')
            old_fills = {x.fill_id: x for x in before.fills}
            for fill in status.fills:
                if fill.fill_id in old_fills and old_fills[fill.fill_id] != fill:
                    raise LiveSafetyError('broker changed an existing fill')
            if before.state in _TERMINAL and status.state != before.state:
                raise LiveSafetyError('broker terminal state changed')
            encoded = json.dumps([{'id': f.fill_id, 'qty': f.quantity,
                                   'price': str(f.price),
                                   'at': _now(f.filled_at_utc).isoformat()}
                                  for f in status.fills], separators=(',', ':'))
            db.execute('''UPDATE live_orders SET state=?, filled=?, fills_json=?,
                accepted=?, rejected_reason=?, broker_id=? WHERE logical_id=?''',
                (status.state.value, status.filled_quantity, encoded,
                 _now(status.accepted_at_utc).isoformat() if status.accepted_at_utc else None,
                 status.rejected_reason, broker_id or before.broker_id, logical_id))
        result = self._load(logical_id)
        if result is None:
            raise LiveSafetyError('journal record vanished')
        return result

    async def _reconcile(self, record: _Record) -> BrokerOrderStatus:
        account = await self._account()
        rows = await self.reader.orders(account, limit_pages=10)
        item = self._match_order(rows, record)
        if item is None:
            if record.state in _TERMINAL:  # incl. released-unsent: journal is authoritative
                return self._status(record)
            # Not found != not placed; no automatic retry, no inferred cancel.
            return self._status(record, unknown=True)
        self._assert_exact_order(item, record)
        broker_id = item.get('id')
        normalized = self._normalize_order(item, record)
        return self._status(self._persist_status(record.intent.logical_order_id,
                                                  normalized, broker_id))

    def _match_order(self, rows: list[dict[str, Any]],
                     record: _Record) -> dict[str, Any] | None:
        """Find the broker order for a record without relying on ref_id.

        Once the broker order id is journaled it is authoritative. Before that, an order
        must match account, option, side, quantity, limit and created time, be
        agent_initiated and not already belong to another journal record. More than one
        candidate is ambiguous: raise, never guess, never resend.
        """
        if record.broker_id:
            by_id = [r for r in rows if r.get('id') == record.broker_id]
            if len(by_id) > 1:
                raise LiveSafetyError('duplicate broker order id')
            return by_id[0] if by_id else None
        if not record.write_attempted:
            return None  # nothing was sent, so there is nothing to claim
        with self._db() as db:
            claimed = {r[0] for r in db.execute(
                'SELECT broker_id FROM live_orders WHERE broker_id IS NOT NULL '
                'AND logical_id!=?', (record.intent.logical_order_id,))}
        low = record.created - timedelta(seconds=_CREATED_SKEW_SECONDS)
        high = _now(self.clock()) + timedelta(seconds=_CREATED_SKEW_SECONDS)
        found = []
        for row in rows:
            if row.get('id') in claimed:
                continue
            try:
                self._assert_exact_order(row, record)
                created = _parse_time(row.get('created_at'))
            except LiveSafetyError:
                continue
            if low <= created <= high:
                found.append(row)
        if len(found) > 1:
            raise LiveSafetyError('ambiguous broker order match')
        return found[0] if found else None

    def _release_unsent(self, logical_id: str, why: str) -> BrokerOrderStatus:
        """Terminal REJECTED for a reservation whose write was never attempted."""
        with self._db() as db:
            db.execute('UPDATE live_orders SET state=?, rejected_reason=? WHERE logical_id=? '
                       'AND state=? AND write_attempted=0 AND broker_id IS NULL',
                       (BrokerOrderState.REJECTED.value, f'NOT_SENT: {why}'[:200],
                        logical_id, BrokerOrderState.UNKNOWN.value))
        record = self._load(logical_id)
        if record is None:
            raise LiveSafetyError('journal record vanished')
        return self._status(record)

    async def _call_write(self, name: str, payload: dict[str, Any]) -> Any:
        if self.rehearsal or not self.writes_enabled:
            raise LiveSafetyError('writes are disabled; refusing to call a write tool')
        if name not in {'place_option_order', 'cancel_option_order'}:
            raise LiveSafetyError('unsupported brokerage write tool')
        if payload.get('account_number') != self.account_number:
            raise LiveSafetyError('broker write does not target pinned account')
        if name == 'place_option_order':
            legs = payload.get('legs')
            if not isinstance(legs, list) or len(legs) != 1:
                raise LiveSafetyError('only single-leg orders permitted')
            leg = legs[0]
            if not isinstance(leg, Mapping):
                raise LiveSafetyError('option leg malformed')
            if leg.get('side') == 'buy' and leg.get('position_effect') == 'open':
                # Check the kill switch again immediately before the MCP call.
                # If paused after the journal write marker, leave UNKNOWN
                # conservatively rather than attempting a duplicate later.
                self._check_write(entry=True)
            elif leg.get('side') != 'sell' or leg.get('position_effect') != 'close':
                raise LiveSafetyError('only long opens and owned closes permitted')
        return await asyncio.wait_for(self.client.call_tool(name, payload),
                                      timeout=self.timeout_seconds)

    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        if not isinstance(intent, OrderIntent):
            raise TypeError('OrderIntent required')
        # Existing journal wins; NEVER send a second place order after any outcome.
        with self._db() as db:
            row = db.execute('SELECT * FROM live_orders WHERE logical_id=? OR idem_key=? '
                             'OR (signal_id=? AND side=?)',
                             (intent.logical_order_id, intent.idempotency_key,
                              _signal(intent), intent.side.value)).fetchone()
        if row:
            existing = self._record(row)
            if row['intent_json'] != _serialize(intent):
                raise LiveSafetyError('idempotency identity reused for another order')
            if not existing.released:
                return self.get_order(existing.intent.logical_order_id)
        return self.run(self._submit_once(intent))

    async def _submit_once(self, intent: OrderIntent) -> BrokerOrderStatus:
        account, instrument, available = await self._preflight(intent)
        record, new = self._reserve(intent, option_id=instrument.option_id,
                                    multiplier=instrument.multiplier, available=available)
        if not new:
            try:
                return await self._reconcile(record)
            except Exception:
                return self._status(record, unknown=True)
        # Every exit before the write below is provably unsent: release the reservation.
        logical = intent.logical_order_id
        try:
            # Orders cannot be matched without a sent write, so an identical order whose
            # outcome is still unknown makes a new one indistinguishable: refuse.
            with self._db() as db:
                twins = db.execute(
                    'SELECT intent_json FROM live_orders WHERE logical_id!=? AND option_id=? '
                    'AND side=? AND state=? AND write_attempted=1 AND broker_id IS NULL',
                    (logical, record.option_id, intent.side.value,
                     BrokerOrderState.UNKNOWN.value)).fetchall()
            if any(_deserialize(r['intent_json']).limit_price == intent.limit_price and
                   _deserialize(r['intent_json']).quantity == intent.quantity for r in twins):
                raise LiveSafetyError('identical order with unknown outcome pending')
            payload = self.reader.future_order_payload(account, instrument, intent)
            self._check_write(entry=intent.side is OrderSide.BUY_TO_OPEN)
            if intent.side is OrderSide.BUY_TO_OPEN:
                buying_power = await self._portfolio()
                self._check_loss_latch(await self._daily_pnl(account))
                if buying_power < intent.limit_price * Decimal(instrument.multiplier):
                    raise LiveSafetyError('insufficient buying power at send')
            self._check_write(entry=intent.side is OrderSide.BUY_TO_OPEN)
        except Exception as exc:
            return self._release_unsent(logical, str(exc) if isinstance(
                exc, LiveSafetyError) else type(exc).__name__)
        # Durable marker BEFORE the write: from here an outcome is uncertain, never released.
        with self._db() as db:
            db.execute('UPDATE live_orders SET write_attempted=1 WHERE logical_id=?', (logical,))
        record = self._load(logical) or record
        try:
            result = await self._call_write('place_option_order', payload)
            # Result is not proof of acceptance; always reconcile authoritative history.
            if isinstance(result, Mapping) and (result.get('isError') or result.get('is_error')):
                raise LiveSafetyError('broker returned ambiguous write error')
        except Exception:
            pass  # Unknown disposition; NEVER resend, even if not found on next read.
        try:
            return await self._reconcile(record)
        except Exception:
            return self._status(record, unknown=True)

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        record = self._load(logical_order_id)
        if record is None:
            return BrokerOrderStatus(logical_order_id, BrokerOrderState.UNKNOWN, 0, None)
        try:
            return self.run(self._reconcile(record))
        except Exception:
            return self._status(record, unknown=True)

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        record = self._load(logical_order_id)
        if record is None:
            return BrokerOrderStatus(logical_order_id, BrokerOrderState.UNKNOWN, 0, None)
        return self.run(self._cancel(record))

    async def _cancel(self, record: _Record) -> BrokerOrderStatus:
        if (record.state is BrokerOrderState.UNKNOWN and not record.write_attempted
                and record.broker_id is None):  # e.g. crash after reserve, before send
            return self._release_unsent(record.intent.logical_order_id, 'cancelled before send')
        try:
            current = await self._reconcile(record)
        except Exception:
            return self._status(record, unknown=True)
        if current.state in _TERMINAL:
            return current
        record = self._load(record.intent.logical_order_id)
        assert record is not None
        if record.broker_id is None:  # outcome unknown: cannot safely target cancellation
            return self._status(record, unknown=True)
        if record.cancel_requested:
            return current
        try:
            self._check_write(entry=False)  # exits/cancellation allowed under kill switch
            await self._tool_schema('cancel_option_order', {'account_number', 'order_id'})
        except Exception:
            return self._status(record, unknown=True)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            result = db.execute('UPDATE live_orders SET cancel_requested=1 '
                                'WHERE logical_id=? AND cancel_requested=0',
                                (record.intent.logical_order_id,))
            if result.rowcount != 1:
                return self._status(self._load(record.intent.logical_order_id), unknown=True)
        with suppress(Exception):  # Cancel outcome unknown; never blindly resend.
            await self._call_write('cancel_option_order',
                                   self.reader.future_cancel_payload(
                                       MCPAccount(record.account, True, 'option_level_2'),
                                       record.broker_id))
        try:
            return await self._reconcile(self._load(record.intent.logical_order_id))
        except Exception:
            return self._status(self._load(record.intent.logical_order_id), unknown=True)

    def refresh_daily_risk(self) -> bool:
        """Poll broker P/L; persist today's stop if hit; True iff stop latched.

        Must be scheduled externally. Missing/invalid snapshots raise and the
        separate entry preflight independently denies trading without P/L.
        """
        return self.run(self._refresh_daily_risk())

    async def _refresh_daily_risk(self) -> bool:
        pnl = await self._daily_pnl(await self._account())
        try:
            self._check_loss_latch(pnl)
        except LiveSafetyError as exc:
            if str(exc) == 'daily realized+unrealized net loss stop latched':
                return True
            raise
        return False

    def review_expired_entries(self) -> tuple[BrokerOrderStatus, ...]:
        """Cancel unfilled entry remainder after 60m from broker acceptance.

        A provably unsent reservation can be released after 60m from reservation,
        but a sent order must have a reconciled broker acceptance timestamp before
        it may be timed out. Unknown acceptance never becomes an inferred cancel.
        """
        threshold = _now(self.clock()) - timedelta(minutes=60)
        with self._db() as db:
            rows = db.execute('SELECT * FROM live_orders WHERE side=? AND created<=? '
                              'AND state IN (?,?,?,?)',
                              (OrderSide.BUY_TO_OPEN.value, threshold.isoformat(),
                               BrokerOrderState.UNKNOWN.value, BrokerOrderState.ACCEPTED.value,
                               BrokerOrderState.PARTIALLY_FILLED.value,
                               BrokerOrderState.CANCEL_PENDING.value)).fetchall()
        due: list[BrokerOrderStatus] = []
        for row in rows:
            record = self._record(row)
            logical = record.intent.logical_order_id
            if not record.write_attempted:
                # Never sent to Robinhood; release the abandoned signal slot.
                due.append(self.cancel_order(logical))
                continue
            # Refresh acceptance and fill state before selecting an order to cancel.
            # A network outage cannot imply acceptance or a terminal cancellation.
            try:
                status = self.get_order(logical)
                refreshed = self._load(logical)
            except Exception:
                continue
            if (status.state not in _TERMINAL and refreshed is not None
                    and refreshed.accepted is not None
                    and refreshed.accepted <= threshold):
                due.append(self.cancel_order(logical))
        return tuple(due)


def _read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f'{Path(db_path).resolve().as_uri()}?mode=ro', uri=True, timeout=5)


def read_kill_state(db_path: Path) -> bool:
    """True when the engine kill switch is engaged; raises if it cannot be read."""
    with closing(_read_only(db_path)) as db:
        row = db.execute('SELECT paused, emergency_stop FROM paper_engine_control '
                         'WHERE singleton=1').fetchone()
    if row is None:
        raise LiveSafetyError('engine control row missing')
    return bool(row[0]) or bool(row[1])


def engine_kill_switch(db_path: Path) -> Callable[[], bool]:
    """The existing engine kill switch (paper_engine_control): paused or emergency stop.

    Fails closed: an unreadable or missing control row counts as killed.
    """
    def killed() -> bool:
        try:
            with closing(_read_only(db_path)) as db:
                row = db.execute('SELECT paused, emergency_stop FROM paper_engine_control '
                                 'WHERE singleton=1').fetchone()
        except sqlite3.Error:
            return True
        return row is None or bool(row[0]) or bool(row[1])
    return killed


def entry_signal_gate(db_path: Path) -> Callable[[str], str]:
    """ENTRY only for a signal whose own alert the parser/lineage recorded as ENTRY.

    ADD / ADDING MORE parse as METADATA_ONLY and never create a signal, so they
    cannot pass. Anything unrecorded or unreadable is DENY.
    """
    def gate(signal_id: str) -> str:
        try:
            with closing(_read_only(db_path)) as db:
                row = db.execute("SELECT 1 FROM signal_parser_decisions WHERE signal_id=? "
                                 "AND intent='ENTRY' AND status='RECORDED'",
                                 (signal_id,)).fetchone()
        except sqlite3.Error:
            return 'DENY'
        return 'ENTRY' if row else 'DENY'
    return gate
