"""Fake-MCP-only regression tests. No network and no brokerage credentials.

Run from the repository root with: pytest -q tests/unit/test_live_broker.py
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swift_autotrader.broker import live_broker
from swift_autotrader.broker.base import BrokerOrderState
from swift_autotrader.broker.live_broker import (
    LiveBroker,
    LiveSafetyError,
    engine_kill_switch,
    entry_signal_gate,
)
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract, OptionRight


@dataclass
class Clock:
    current: datetime = datetime(2026, 11, 17, 17, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class FakeMCPClient:
    """Strict async in-memory broker using the real Robinhood MCP response shapes.

    NEVER calls Robinhood or any network.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.account = 'MCP-ONLY-123'
        self.agentic_allowed = True
        self.options_level = 'option_level_2'
        self.buying_power = '5000'
        self.realized = '-100'
        self.unrealized = '-200'
        self.mark = '2.00'
        self.ask = '2.01'
        self.bid = '1.99'
        self.multiplier: int | None = 100
        self.tick: str | None = '0.01'
        self.instrument_id = 'option-instrument-123'
        self.orders: list[dict[str, Any]] = []
        self.position = 1
        self.review_alerts: list[Any] = []
        self.approval_required = False
        self.place_count = 0
        self.cancel_count = 0
        self.disconnect_place: str | None = None  # None, before, after
        self.read_failure = False
        self.read_failure_after_place = False  # order history outage that starts at a write
        self.remove_ref_field = False
        self.call_log: list[str] = []
        self.loops: set[int] = set()
        self.realized_failure = False
        self.realized_null_trades: int | None = None  # realized_gain null with N trades
        self.omit_total_returns = False
        self.stale_quote = False
        self.omit_close = False
        self.page_positions = False
        self.next_at_top = False  # `next` beside `data` instead of inside it
        self.extra_positions: list[dict[str, Any]] = []
        # Paginated order history: older broker orders listed before ours.
        self.history: list[dict[str, Any]] = []
        self.orders_page_size: int | None = None
        self.repeat_cursor = False
        self.fail_order_page: int | None = None  # page index whose read is interrupted
        self.order_pages_read = 0

    def _stamp(self) -> str:
        return self.clock().isoformat()

    def position_row(self, option_id: str) -> dict[str, Any]:
        # Held from a prior day: (mark - prior close) x 100 x 1 == self.unrealized.
        # average_price is per contract: (mark x 100 - average) x 1 == self.unrealized too.
        return {'option_id': option_id, 'type': 'long', 'quantity': f'{self.position}.0000',
                'average_price': str(Decimal(self.mark) * 100 - Decimal(self.unrealized)),
                'trade_value_multiplier': '100.0000'}

    async def list_tools(self) -> list[dict[str, Any]]:
        schemas = {
            'get_accounts': set(),
            'get_portfolio': {'account_number'},
            'get_realized_pnl': {'account_number', 'start_date', 'end_date'},
            'get_option_chains': {'underlying_symbol'},
            'get_option_instruments': {'chain_id', 'expiration_dates', 'strike_price',
                                       'type', 'state'},
            'get_option_quotes': {'instrument_ids'},
            'get_option_orders': {'account_number'},
            'get_option_positions': {'account_number', 'nonzero'},
            'review_option_order': {'account_number', 'legs', 'quantity'},
            'place_option_order': {'account_number', 'ref_id', 'legs', 'quantity',
                                   'price', 'type', 'time_in_force', 'market_hours'},
            'cancel_option_order': {'account_number', 'order_id'},
        }
        if self.remove_ref_field:
            schemas['place_option_order'].remove('ref_id')
        return [{'name': n, 'inputSchema': {'type': 'object', 'properties':
                {key: {'type': 'string'} for key in props}}} for n, props in schemas.items()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.call_log.append(name)
        self.loops.add(id(asyncio.get_running_loop()))
        if name == 'get_option_orders' and (
                self.read_failure or (self.read_failure_after_place and self.place_count)):
            raise ConnectionError('synthetic read interruption')
        if name == 'get_accounts':
            return {'accounts': [
                {'account_number': self.account, 'agentic_allowed': self.agentic_allowed,
                 'option_level': self.options_level},
                {'account_number': 'INDIVIDUAL-1', 'agentic_allowed': False,
                 'option_level': 'option_level_3'}]}
        if name == 'get_portfolio':
            assert arguments['account_number'] == self.account
            return {'account_number': self.account,
                    'buying_power': {'buying_power': self.buying_power}}
        if name == 'get_realized_pnl':
            assert arguments['account_number'] == self.account
            today = (self.clock() - timedelta(hours=5)).date().isoformat()  # New York, EST
            assert arguments['start_date'] == arguments['end_date'] == today
            if self.realized_failure:
                raise ConnectionError('synthetic realized P/L outage')
            point = {'realized_gain': self.realized, 'number_of_trades': 1}
            if self.realized_null_trades is not None:
                point = {'realized_gain': None, 'number_of_trades': self.realized_null_trades}
            data: dict[str, Any] = {'total_returns': self.realized, 'data_points': [point]}
            if self.omit_total_returns:
                del data['total_returns']
            return {'data': data}
        if name == 'get_option_chains':
            assert arguments['underlying_symbol'] == 'AAPL'
            return {'chains': [{'id': 'chain-AAPL', 'underlying_symbol': 'AAPL',
                                'expiration_dates': ['2027-02-19']}]}
        if name == 'get_option_instruments':
            return {'instruments': [
                {'id': self.instrument_id, 'chain_id': 'chain-AAPL',
                 'underlying_symbol': 'AAPL', 'chain_symbol': 'AAPL',
                 'strike_price': '150', 'expiration_date': '2027-02-19',
                 'type': 'call', 'tradability': 'tradable', 'state': 'active',
                 'minimum_tick': self.tick, 'multiplier': self.multiplier}]}
        if name == 'get_option_quotes':
            updated = self.clock() - timedelta(seconds=60 if self.stale_quote else 0)
            results = []
            for option_id in arguments['instrument_ids']:
                result: dict[str, Any] = {
                    'quote': {'instrument_id': option_id, 'bid_price': self.bid,
                              'ask_price': self.ask, 'mark_price': self.mark,
                              'updated_at': updated.isoformat()},
                    'close': {'price': str(Decimal(self.mark) - Decimal(self.unrealized) / 100)}}
                if self.omit_close:
                    del result['close']
                results.append(result)
            return {'data': {'results': results}}
        if name == 'review_option_order':
            return {'order_checks': self.review_alerts,
                    'approval_required': self.approval_required}
        if name == 'get_option_orders':
            assert arguments['account_number'] == self.account
            rows = [dict(x) for x in self.history + self.orders]
            if not self.orders_page_size:
                return {'data': {'orders': rows, 'next': None}}
            start = int(arguments.get('cursor', '0'))
            page = start // self.orders_page_size
            self.order_pages_read += 1
            if self.fail_order_page is not None and page == self.fail_order_page:
                raise ConnectionError('synthetic outage in the middle of history pagination')
            end = start + self.orders_page_size
            cursor = 1 if self.repeat_cursor else end
            more = end < len(rows)
            return {'data': {'orders': rows[start:end],
                             'next': f'https://api.example/o/?cursor={cursor}' if more else None}}
        if name == 'get_option_positions':
            assert arguments['account_number'] == self.account
            assert arguments['nonzero'] is True
            rows = [self.position_row(self.instrument_id)] if self.position else []
            if self.page_positions and 'cursor' not in arguments:
                url = 'https://api.example/p/?cursor=p2'
                if self.next_at_top:
                    return {'data': {'positions': rows}, 'next': url}
                return {'data': {'positions': rows, 'next': url}}
            if self.page_positions:
                rows = list(self.extra_positions)
            return {'data': {'positions': rows, 'next': None}}
        if name == 'place_option_order':
            assert arguments['account_number'] == self.account
            assert arguments['type'] == 'limit'
            assert arguments['time_in_force'] == 'gfd'
            assert arguments['market_hours'] == 'regular_hours'
            self.place_count += 1
            if self.disconnect_place == 'before':
                raise ConnectionError('simulated pre-commit socket loss')
            order = {
                'id': f'broker-order-{self.place_count}', 'state': 'confirmed',
                'type': 'limit', 'quantity': f"{Decimal(arguments['quantity']):.4f}",
                'price': f"{Decimal(arguments['price']):.4f}",
                'processed_quantity': '0.0000', 'placed_agent': 'agent',
                'agent_initiated': True, 'created_at': self._stamp(),
                'updated_at': self._stamp(),
                'legs': [dict(leg, executions=[]) for leg in arguments['legs']],
            }
            self.orders.append(order)
            if self.disconnect_place == 'after':
                raise ConnectionError('simulated post-commit socket loss')
            return {'data': {'order': order}}
        if name == 'cancel_option_order':
            assert arguments['account_number'] == self.account
            self.cancel_count += 1
            matched = [x for x in self.orders if x['id'] == arguments['order_id']]
            assert len(matched) == 1
            matched[0]['state'] = 'cancelled'
            return {'data': {'order': matched[0]}}
        raise AssertionError(f'unexpected MCP tool {name}')


@pytest.fixture
def scenario(tmp_path: Path) -> tuple[LiveBroker, FakeMCPClient, Clock]:
    clock = Clock()
    fake = FakeMCPClient(clock)
    path = tmp_path / 'live.sqlite'
    LiveBroker.create_journal(path)
    broker = LiveBroker(path, fake, account_number=fake.account,
                        writes_enabled=True, kill_switch=lambda: False,
                        signal_gate=lambda _signal: 'ENTRY', clock=clock)
    yield broker, fake, clock
    broker.close()


def entry(signal: str = 'signal-1', *, price: str = '2.00',
          key: str | None = None) -> OrderIntent:
    return OrderIntent(f'{signal}:entry', key or f'{signal}-entry-idem',
                       OptionContract('AAPL', OptionRight.CALL,
                                      Decimal('150'), date(2027, 2, 19)),
                       OrderSide.BUY_TO_OPEN, 1, Decimal(price))


def exit_intent(signal: str = 'signal-1') -> OrderIntent:
    return OrderIntent(f'{signal}:exit', f'{signal}-exit-idem',
                       entry(signal).contract, OrderSide.SELL_TO_CLOSE,
                       1, Decimal('1.95'))


def mark_filled(fake: FakeMCPClient, clock: Clock, index: int = 0) -> None:
    record = fake.orders[index]
    record.update(state='filled', processed_quantity=record['quantity'])
    record['legs'][0]['executions'] = [{'id': 'fill-1', 'quantity': record['quantity'],
                                        'price': record['price'],
                                        'timestamp': clock().isoformat()}]


def broker_order(fake: FakeMCPClient, *, executions=None, **change) -> dict[str, Any]:
    """An order in the real get_option_orders shape (no ref_id/filled_quantity/fills)."""
    stamp = fake.clock().isoformat()
    order = {'id': 'foreign', 'state': 'confirmed', 'type': 'limit', 'quantity': '1.0000',
             'price': '2.0000', 'processed_quantity': '0.0000', 'placed_agent': 'agent',
             'agent_initiated': True, 'created_at': stamp, 'updated_at': stamp,
             'legs': [{'option_id': fake.instrument_id, 'side': 'buy',
                       'position_effect': 'open', 'ratio_quantity': 1,
                       'executions': executions or []}]}
    order.update(change)
    return order


def test_entry_one_contract_exact_limit_and_persisted_idempotency(scenario):
    broker, fake, _ = scenario
    item = entry()
    assert broker.submit_order(item).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1
    order = fake.orders[0]
    assert Decimal(order['quantity']) == 1
    assert Decimal(order['price']) == Decimal('2.00')
    assert 'ref_id' not in order and 'filled_quantity' not in order  # real shape
    assert len(order['legs']) == 1
    assert order['legs'][0]['position_effect'] == 'open'
    assert broker.submit_order(item).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1
    # Restart with same journal never replays placement.
    restarted = LiveBroker(broker.path, fake, account_number=fake.account,
                           writes_enabled=True, kill_switch=lambda: False,
                           signal_gate=lambda _signal: 'ENTRY', clock=broker.clock)
    assert restarted.submit_order(item).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1


@pytest.mark.parametrize('disconnect', ['before', 'after'])
def test_disconnect_never_triggers_duplicate_submission(scenario, disconnect):
    broker, fake, _ = scenario
    fake.disconnect_place = disconnect
    first = broker.submit_order(entry())
    assert first.state is (BrokerOrderState.UNKNOWN if disconnect == 'before'
                           else BrokerOrderState.ACCEPTED)
    fake.disconnect_place = None
    assert broker.submit_order(entry()).state is first.state
    assert fake.place_count == 1
    if disconnect == 'after':
        assert len(fake.orders) == 1
    else:
        assert not fake.orders


def test_unknown_read_does_not_claim_order_absent_or_retry(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.read_failure = True
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert broker.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 1


def test_signal_uniqueness_prevents_averaging_or_changed_client_id(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    with pytest.raises(LiveSafetyError):
        broker.submit_order(entry(key='different-key'))
    assert fake.place_count == 1


@pytest.mark.parametrize('realized,unrealized', [('-550', '-200'), ('-200', '-700')])
def test_daily_loss_counts_realized_and_unrealized(scenario, realized, unrealized):
    broker, fake, _ = scenario
    fake.realized = realized
    fake.unrealized = unrealized
    with pytest.raises(LiveSafetyError, match='daily'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_max_premium_buying_power_and_slippage_guards(scenario):
    broker, fake, _ = scenario
    with pytest.raises(LiveSafetyError, match='999'):
        broker.submit_order(entry(price='10.00'))
    fake.buying_power = '100'
    with pytest.raises(LiveSafetyError, match='buying power'):
        broker.submit_order(entry())
    fake.buying_power = '5000'
    fake.ask = '2.21'
    with pytest.raises(LiveSafetyError, match='10%'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_mcp_account_pin_rejects_individual_or_unauthorized(scenario):
    broker, fake, clock = scenario
    # No account_type exists: the pinned number plus agentic_allowed is the identity.
    wrong = LiveBroker(broker.path, fake, account_number='INDIVIDUAL-1', writes_enabled=True,
                       kill_switch=lambda: False, signal_gate=lambda _signal: 'ENTRY',
                       clock=clock)
    with pytest.raises(LiveSafetyError, match='agentic_allowed'):
        wrong.submit_order(entry())
    wrong.close()
    missing = LiveBroker(broker.path, fake, account_number='NOT-THERE', writes_enabled=True,
                         kill_switch=lambda: False, signal_gate=lambda _signal: 'ENTRY',
                         clock=clock)
    with pytest.raises(LiveSafetyError, match='absent'):
        missing.submit_order(entry())
    missing.close()
    fake.agentic_allowed = False
    with pytest.raises(LiveSafetyError, match='agentic_allowed'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_add_and_unverified_signals_ignored_even_with_valid_order(scenario):
    broker, fake, _ = scenario
    broker.signal_gate = lambda _: 'ADD'
    with pytest.raises(LiveSafetyError, match='ADD denied'):
        broker.submit_order(entry())
    broker.signal_gate = None
    with pytest.raises(LiveSafetyError, match='ADD denied'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_kill_switch_blocks_entry_but_allows_owned_exit(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry())
    mark_filled(fake, clock)
    killed = LiveBroker(broker.path, fake, account_number=fake.account,
                        writes_enabled=True, kill_switch=lambda: True,
                        signal_gate=lambda _signal: 'ENTRY', clock=clock)
    with pytest.raises(LiveSafetyError, match='kill switch'):
        killed.submit_order(entry('signal-2'))
    result = killed.submit_order(exit_intent())
    assert result.state is BrokerOrderState.ACCEPTED
    assert fake.orders[1]['legs'][0]['position_effect'] == 'close'
    assert fake.place_count == 2


def test_exit_never_adopts_manual_contract_inventory(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry())
    mark_filled(fake, clock)
    fake.position = 2  # one Swift fill, one external/manual contract
    with pytest.raises(LiveSafetyError, match='untracked/manual'):
        broker.submit_order(exit_intent())
    assert fake.place_count == 1


def test_full_exit_requires_confirmed_ownership(scenario):
    broker, fake, clock = scenario
    with pytest.raises(LiveSafetyError, match='owned'):
        broker.submit_order(exit_intent())
    broker.submit_order(entry())
    with pytest.raises(LiveSafetyError, match='entry not conclusively'):
        broker.submit_order(exit_intent())
    mark_filled(fake, clock)
    fake.position = 0
    with pytest.raises(LiveSafetyError, match='long position'):
        broker.submit_order(exit_intent())
    assert fake.place_count == 1


def test_cancel_after_60_minutes_once_and_preserve_fills(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry())
    clock.advance(59)
    assert broker.review_expired_entries() == ()
    clock.advance(2)
    result = broker.review_expired_entries()
    assert len(result) == 1
    assert result[0].state is BrokerOrderState.CANCELED
    assert fake.cancel_count == 1
    assert broker.review_expired_entries() == ()
    assert fake.cancel_count == 1


def test_pending_order_with_no_execution_field_reconciles(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.orders[0]['legs'][0].pop('executions')
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.ACCEPTED
    fake.orders[0].update(state='cancelled', processed_quantity='0.0000')
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.CANCELED
    assert fake.place_count == 1


def test_filled_order_without_executions_stays_unknown(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.orders[0].update(state='filled', processed_quantity='1.0000')
    fake.orders[0]['legs'][0].pop('executions')
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 1


def test_timeout_uses_broker_acceptance_not_local_reservation(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry())
    # Broker acceptance occurs ten minutes after our durable reservation.
    fake.orders[0]['created_at'] = (clock() + timedelta(minutes=10)).isoformat()
    clock.advance(10)
    assert broker.get_order('signal-1:entry').accepted_at_utc == clock()
    clock.advance(51)  # reservation is 61m old; acceptance only 51m old
    assert broker.review_expired_entries() == ()
    assert fake.cancel_count == 0
    clock.advance(10)
    assert len(broker.review_expired_entries()) == 1
    assert fake.cancel_count == 1


def test_rejection_is_terminal_and_not_resubmitted(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.orders[0]['state'] = 'rejected'
    status = broker.get_order('signal-1:entry')
    assert status.state is BrokerOrderState.REJECTED
    assert status.rejected_reason == 'broker state rejected'
    assert broker.submit_order(entry()).state is BrokerOrderState.REJECTED
    assert fake.place_count == 1


def test_partial_fills_and_cancel_race_on_preexisting_multiquantity_order(scenario):
    broker, fake, clock = scenario
    # A pre-existing/legacy order can have quantity >1; new entries remain qty=1.
    legacy = OrderIntent('legacy:entry', 'legacy-order-key', entry().contract,
                         OrderSide.BUY_TO_OPEN, 2, Decimal('2.00'))
    record, new = broker._reserve(legacy, option_id=fake.instrument_id, multiplier=100,
                                  available=Decimal('1000'))
    assert new
    with sqlite3.connect(broker.path) as con:  # as if its write had been sent
        con.execute('UPDATE live_orders SET write_attempted=1 WHERE logical_id=?',
                    ('legacy:entry',))
    fake.orders.append(broker_order(
        fake, id='legacy-order-broker', quantity='2.0000', price='2.0000',
        state='partially_filled', processed_quantity='1.0000',
        executions=[{'id': 'legacy-fill-1', 'quantity': '1.0000', 'price': '1.9900',
                     'timestamp': clock().isoformat()}]))
    first = broker.get_order('legacy:entry')
    assert first.state is BrokerOrderState.PARTIALLY_FILLED
    assert first.filled_quantity == 1 and len(first.fills) == 1
    clock.advance(61)
    result = broker.review_expired_entries()
    assert len(result) == 1
    assert result[0].state is BrokerOrderState.CANCELED
    assert result[0].filled_quantity == 1
    assert fake.cancel_count == 1


def test_inconsistent_partial_fill_fail_closed(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.orders[0].update(state='partially_filled', processed_quantity='1.0000')
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 1


def test_no_default_live_writes_and_no_credential_leak(tmp_path, capsys):
    fake_clock = Clock()
    fake = FakeMCPClient(fake_clock)
    path = tmp_path / 'journal.db'
    LiveBroker.create_journal(path)
    guarded = LiveBroker(path, fake, account_number=fake.account, clock=fake_clock)
    with pytest.raises(LiveSafetyError, match='disabled'):
        guarded.submit_order(entry())
    assert fake.place_count == 0
    captured = capsys.readouterr()
    assert 'secret-token-DO-NOT-PRINT' not in captured.out + captured.err


def test_write_schema_must_expose_client_ref(scenario):
    broker, fake, _ = scenario
    fake.remove_ref_field = True
    with pytest.raises(LiveSafetyError, match='schema'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_cancel_unknown_order_does_not_guess_broker_id(scenario):
    broker, fake, _ = scenario
    fake.disconnect_place = 'before'
    broker.submit_order(entry())
    status = broker.cancel_order('signal-1:entry')
    assert status.state is BrokerOrderState.UNKNOWN
    assert fake.cancel_count == 0


def test_atomic_reservation_prevents_parallel_budget_overcommit(scenario):
    broker, fake, _ = scenario
    broker._reserve(entry('first'), option_id=fake.instrument_id, multiplier=100,
                    available=Decimal('250'))
    with pytest.raises(LiveSafetyError, match='atomic buying power'):
        broker._reserve(entry('second'), option_id=fake.instrument_id, multiplier=100,
                        available=Decimal('250'))
    assert fake.place_count == 0


def test_no_arbitrary_open_trade_count_cap(scenario):
    broker, fake, _ = scenario
    for n in range(5):
        assert broker.submit_order(entry(f'valid-{n}')).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 5


def test_daily_loss_stop_is_latched_despite_recovery_and_restart(scenario):
    broker, fake, clock = scenario
    fake.realized, fake.unrealized = '-700', '-50'
    with pytest.raises(LiveSafetyError, match='latched'):
        broker.submit_order(entry())
    fake.realized, fake.unrealized = '25', '25'
    restarted = LiveBroker(broker.path, fake, account_number=fake.account,
                           writes_enabled=True, kill_switch=lambda: False,
                           signal_gate=lambda _: 'ENTRY', clock=clock)
    with pytest.raises(LiveSafetyError, match='latched'):
        restarted.submit_order(entry())
    # The following New York calendar date starts a new daily accounting session.
    clock.advance(24 * 60)
    assert restarted.submit_order(entry()).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1


def test_daily_risk_monitor_latches_breach_without_new_entry(scenario):
    broker, fake, _ = scenario
    assert broker.refresh_daily_risk() is False
    fake.realized, fake.unrealized = '-400', '-351'
    assert broker.refresh_daily_risk() is True
    fake.realized, fake.unrealized = '-10', '10'
    assert broker.refresh_daily_risk() is True
    with pytest.raises(LiveSafetyError, match='latched'):
        broker.submit_order(entry())
    assert fake.place_count == 0


class FlipKill:
    """Kill switch that reads off for the first N calls, then on."""

    def __init__(self, off_reads: int) -> None:
        self.left = off_reads

    def __call__(self) -> bool:
        self.left -= 1
        return self.left < 0


def test_kill_switch_rechecked_at_transport_boundary(scenario):
    broker, fake, _ = scenario
    broker.kill_switch = FlipKill(3)  # after journal write_attempted is saved
    status = broker.submit_order(entry())
    assert status.state is BrokerOrderState.UNKNOWN
    assert broker._load('signal-1:entry').write_attempted
    assert fake.place_count == 0
    assert broker.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 0


def test_unsent_entry_is_terminal_and_releases_buying_power_and_slot(scenario):
    broker, fake, _ = scenario
    broker.kill_switch = FlipKill(1)  # off at preflight, on just before the write
    fake.buying_power = '250'
    status = broker.submit_order(entry('first'))
    assert status.state is BrokerOrderState.REJECTED
    assert status.rejected_reason.startswith('NOT_SENT')
    assert fake.place_count == 0
    assert broker.get_order('first:entry').state is BrokerOrderState.REJECTED
    broker.kill_switch = lambda: False
    # Buying power released: a 250 budget still fits another $200 entry.
    assert broker.submit_order(entry('second')).state is BrokerOrderState.ACCEPTED
    # Signal slot released: the same entry can now be sent, exactly once.
    fake.buying_power = '5000'
    assert broker.submit_order(entry('first')).state is BrokerOrderState.ACCEPTED
    assert broker.submit_order(entry('first')).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 2


def test_reserved_but_never_sent_entry_is_released_and_never_reopened_after_expiry(scenario):
    broker, fake, clock = scenario
    broker._reserve(entry(), option_id=fake.instrument_id, multiplier=100,
                    available=Decimal('1000'))  # simulates a crash before the send
    clock.advance(61)
    (result,) = broker.review_expired_entries()
    assert result.state is BrokerOrderState.REJECTED
    assert result.rejected_reason.startswith('NOT_SENT')
    assert fake.cancel_count == 0 and fake.place_count == 0
    with pytest.raises(LiveSafetyError, match='expired'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_one_long_lived_loop_serves_every_call(scenario):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    broker.get_order('signal-1:entry')
    broker.refresh_daily_risk()
    assert len(fake.loops) == 1 and broker._loop.thread.is_alive()

    async def inside_a_loop():
        with pytest.raises(LiveSafetyError, match='event loop'):
            broker.refresh_daily_risk()
    asyncio.run(inside_a_loop())


def test_limits_are_read_from_risk_limits(scenario, monkeypatch):
    from swift_autotrader import risk_limits
    assert Decimal('750') == risk_limits.DAILY_NET_LOSS_LIMIT_USD
    assert Decimal('999') == risk_limits.PREMIUM_CEILING_USD
    broker, fake, _ = scenario
    monkeypatch.setattr(live_broker, 'PREMIUM_CEILING_USD', Decimal('100'))
    with pytest.raises(LiveSafetyError, match='100'):
        broker.submit_order(entry())
    monkeypatch.setattr(live_broker, 'PREMIUM_CEILING_USD', Decimal('999'))
    # The latch uses the configured ceiling (MAX_DAILY_LOSS_USD), bounded by risk_limits.
    assert broker.loss_limit == risk_limits.DAILY_NET_LOSS_LIMIT_USD
    tighter = LiveBroker(broker.path, fake, account_number=fake.account, writes_enabled=True,
                         kill_switch=lambda: False, signal_gate=lambda _signal: 'ENTRY',
                         clock=broker.clock, loss_limit=Decimal('250'))
    with pytest.raises(LiveSafetyError, match='latched'):  # fake P/L is -300
        tighter.submit_order(entry())
    tighter.close()
    for bad in (Decimal('750.01'), Decimal('0'), Decimal('-1'), Decimal('NaN')):
        with pytest.raises(ValueError, match='approved range'):
            LiveBroker(broker.path, fake, account_number=fake.account, loss_limit=bad)
    assert fake.place_count == 0


def test_signal_gate_follows_real_entry_classification(tmp_path):
    db = tmp_path / 'swift.db'
    apply_migrations(db)
    assert parse_message('SPY 680C 11/13 @ 1.20').intent is SignalIntent.ENTRY
    add = parse_message('ADDING MORE SPY 680C 11/13 @ 1.00')
    assert add.intent is not SignalIntent.ENTRY
    with sqlite3.connect(db) as con:
        con.execute('PRAGMA foreign_keys=OFF')
        for rev, sid, intent in [(1, 'sig-entry', SignalIntent.ENTRY),
                                 (2, 'sig-entry', add.intent),
                                 (3, 'sig-add-only', add.intent)]:
            con.execute('INSERT INTO signal_parser_decisions(revision_id,channel_id,message_id,'
                        "intent,status,signal_id,processed_at_utc) VALUES(?,?,?,?,'RECORDED',?,"
                        "'2026-11-17T17:00:00Z')", (rev, 'c', rev, intent.value, sid))
    gate = entry_signal_gate(db)
    assert gate('sig-entry') == 'ENTRY'
    assert gate('sig-add-only') == 'DENY' and gate('unknown') == 'DENY'
    assert entry_signal_gate(tmp_path / 'missing.db')('sig-entry') == 'DENY'


def test_kill_switch_is_the_engine_control_row(tmp_path):
    db = tmp_path / 'swift.db'
    apply_migrations(db)
    killed = engine_kill_switch(db)
    assert killed() is False
    for column in ('paused', 'emergency_stop'):
        with sqlite3.connect(db) as con:
            con.execute(f'UPDATE paper_engine_control SET {column}=1')
        assert killed() is True
        with sqlite3.connect(db) as con:
            con.execute(f'UPDATE paper_engine_control SET {column}=0')
    assert engine_kill_switch(tmp_path / 'missing.db')() is True


def test_order_matched_without_ref_id_and_distinct_signals_do_not_collide(scenario):
    broker, fake, _ = scenario
    for name in ('a', 'b'):  # identical contract, price and quantity
        assert broker.submit_order(entry(name)).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 2
    assert all('ref_id' not in o for o in fake.orders)
    ids = {broker._load(f'{n}:entry').broker_id for n in ('a', 'b')}
    assert ids == {'broker-order-1', 'broker-order-2'}


def test_ambiguous_candidates_stay_unknown_and_never_resend(scenario):
    broker, fake, _ = scenario
    fake.disconnect_place = 'after'  # order exists; the read-back also fails
    fake.read_failure_after_place = True
    assert broker.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    fake.read_failure_after_place = False
    fake.orders.append(dict(fake.orders[0], id='broker-order-dup'))  # second candidate
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert broker._load('signal-1:entry').broker_id is None  # claimed nothing
    del fake.orders[1]  # a single candidate resolves normally, still without a resend
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.ACCEPTED
    assert broker.submit_order(entry()).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1


@pytest.mark.parametrize('change', [
    {'agent_initiated': False},
    {'created_at': '2026-11-17T12:00:00+00:00'},   # long before our write
    {'price': '2.0100'}, {'quantity': '2.0000'}, {'account_number': 'OTHER'},
])
def test_non_matching_orders_are_not_claimed(scenario, change):
    broker, fake, _ = scenario
    fake.disconnect_place = 'before'  # nothing placed by us
    assert broker.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    fake.orders.append(broker_order(fake, **change))
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 1


def test_unknown_outcome_blocks_identical_new_order(scenario):
    broker, fake, _ = scenario
    fake.disconnect_place = 'before'
    assert broker.submit_order(entry('first')).state is BrokerOrderState.UNKNOWN
    fake.disconnect_place = None
    status = broker.submit_order(entry('second'))  # identical order, first still unknown
    assert status.state is BrokerOrderState.REJECTED
    assert 'unknown outcome' in status.rejected_reason
    assert fake.place_count == 1


@pytest.mark.parametrize('raw,expected', [
    ('queued', BrokerOrderState.ACCEPTED), ('unconfirmed', BrokerOrderState.ACCEPTED),
    ('confirmed', BrokerOrderState.ACCEPTED), ('cancelled', BrokerOrderState.CANCELED),
    ('rejected', BrokerOrderState.REJECTED), ('failed', BrokerOrderState.REJECTED),
    ('voided', BrokerOrderState.REJECTED),
    ('canceled', BrokerOrderState.UNKNOWN),  # one-l spelling is not a real state: fail closed
])
def test_real_order_states_map(scenario, raw, expected):
    broker, fake, _ = scenario
    broker.submit_order(entry())
    fake.orders[0]['state'] = raw
    assert broker.get_order('signal-1:entry').state is expected


def test_filled_order_executions_become_fills(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry())
    mark_filled(fake, clock)
    status = broker.get_order('signal-1:entry')
    assert status.state is BrokerOrderState.FILLED and status.filled_quantity == 1
    assert [(f.fill_id, f.quantity, f.price) for f in status.fills] == [
        ('fill-1', 1, Decimal('2.00'))]
    assert status.accepted_at_utc == clock()  # created_at stands in for acceptance


def test_overnight_close_denies_unverified_daily_realized_basis(scenario):
    broker, fake, clock = scenario
    fake.position = 0
    fake.orders.append(broker_order(
        fake, state='filled', processed_quantity='1.0000',
        legs=[{'option_id': fake.instrument_id, 'side': 'sell',
               'position_effect': 'close', 'executions': [
                   {'id': 'overnight-close', 'quantity': '1.0000', 'price': '2.00',
                    'timestamp': clock().isoformat()}]}]))
    with pytest.raises(LiveSafetyError, match='overnight close'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_missing_fill_history_denies_daily_risk_even_when_flat(scenario):
    broker, fake, _ = scenario
    fake.position = 0
    fake.orders.append(broker_order(fake, state='filled',
                                    processed_quantity='1.0000', executions=[]))
    with pytest.raises(LiveSafetyError, match='executions do not reconcile'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_daily_risk_uses_execution_date_not_order_creation_date(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry('today'))
    mark_filled(fake, clock)
    fake.orders[0]['created_at'] = (clock() - timedelta(days=1)).isoformat()
    fake.omit_close = True
    assert broker.refresh_daily_risk() is False


def _spread(fake: FakeMCPClient, **change) -> dict[str, Any]:
    leg = {'option_id': fake.instrument_id, 'side': 'buy', 'position_effect': 'open',
           'ratio_quantity': 1, 'executions': []}
    return broker_order(fake, state='filled', processed_quantity='1.0000',
                        legs=[leg, dict(leg, side='sell')], **change)


def test_old_manual_multileg_fill_does_not_block_daily_risk(scenario):
    broker, fake, clock = scenario
    old = (clock() - timedelta(days=20)).isoformat()
    fake.orders.append(_spread(fake, created_at=old, updated_at=old))
    assert broker.submit_order(entry()).state is BrokerOrderState.ACCEPTED


@pytest.mark.parametrize('stamps', [{}, {'updated_at': None}])
def test_multileg_fill_with_activity_today_or_unknown_age_still_denies(scenario, stamps):
    broker, fake, clock = scenario
    old = (clock() - timedelta(days=20)).isoformat()
    order = _spread(fake, created_at=old, updated_at=old) if stamps else _spread(fake)
    order.update(stamps)
    fake.orders.append(order)
    with pytest.raises(LiveSafetyError, match='multi-leg'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_realized_null_gain_needs_zero_trades(scenario):
    broker, fake, _ = scenario
    fake.realized, fake.realized_null_trades = '0', 0  # null gain, no trades: zero
    assert broker.submit_order(entry('a')).state is BrokerOrderState.ACCEPTED
    fake.realized_null_trades = 3  # null gain but trades happened: deny
    with pytest.raises(LiveSafetyError, match='null although trades'):
        broker.submit_order(entry('b'))
    fake.realized_null_trades = None
    fake.omit_total_returns = True
    with pytest.raises(LiveSafetyError, match='missing'):
        broker.submit_order(entry('b'))
    assert fake.place_count == 1


def test_realized_outage_fails_closed(scenario):
    broker, fake, _ = scenario
    fake.realized_failure = True
    with pytest.raises(LiveSafetyError, match='realized'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_unrealized_prior_day_hold_marks_to_prior_close(scenario):
    broker, fake, _ = scenario
    fake.unrealized = '-650'  # (mark - prior close) x 100 x 1; with realized -100 => -750
    with pytest.raises(LiveSafetyError, match='latched'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_prior_day_hold_without_prior_close_is_denied(scenario):
    broker, fake, _ = scenario
    fake.omit_close = True
    with pytest.raises(LiveSafetyError, match='prior close'):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_position_opened_today_marks_to_per_contract_average_price(scenario):
    broker, fake, clock = scenario
    assert broker.submit_order(entry('a')).state is BrokerOrderState.ACCEPTED
    mark_filled(fake, clock)  # bought today: no prior close needed, average_price is used
    fake.omit_close = True
    assert broker.submit_order(entry('b')).state is BrokerOrderState.ACCEPTED
    fake.unrealized = '-700'  # (mark x 100 - average_price) x 1 == -700; with -100 => latched
    with pytest.raises(LiveSafetyError, match='latched'):
        broker.submit_order(entry('c'))
    assert fake.place_count == 2


def test_position_mixing_prior_and_today_lots_is_denied(scenario):
    broker, fake, clock = scenario
    broker.submit_order(entry('a'))
    mark_filled(fake, clock)
    fake.position = 2  # one lot from today plus one from an earlier day
    with pytest.raises(LiveSafetyError, match='mixes'):
        broker.submit_order(entry('b'))
    assert fake.place_count == 1


def test_stale_quote_for_open_position_is_denied(scenario):
    broker, fake, _ = scenario
    fake.stale_quote = True
    with pytest.raises(LiveSafetyError, match='stale'):
        broker.submit_order(entry())
    assert fake.place_count == 0


@pytest.mark.parametrize('top', [False, True])
def test_positions_follow_next_pages(scenario, top):
    broker, fake, _ = scenario
    fake.next_at_top = top
    fake.unrealized = '-350'  # one position: -100 + -350 = -450, allowed
    assert broker.submit_order(entry('a')).state is BrokerOrderState.ACCEPTED
    fake.page_positions = True  # a second page holds another losing position: -800
    fake.extra_positions = [fake.position_row('option-on-page-2')]
    with pytest.raises(LiveSafetyError, match='latched'):
        broker.submit_order(entry('b'))
    assert fake.place_count == 1


def test_buying_power_comes_only_from_get_portfolio(scenario):
    broker, fake, _ = scenario
    portfolio = asyncio.run(fake.call_tool('get_portfolio', {'account_number': fake.account}))
    assert set(portfolio) == {'account_number', 'buying_power'}  # no P/L fields relied on
    fake.buying_power = '100'
    with pytest.raises(LiveSafetyError, match='buying power'):
        broker.submit_order(entry())


# --- Order-history pagination: full coverage proof, never a 10-page assumption ----------

def _history_filler(fake: FakeMCPClient, count: int) -> list[dict[str, Any]]:
    """Older, unfilled foreign orders that pad the history across many pages."""
    old = (fake.clock() - timedelta(days=3)).isoformat()
    return [broker_order(fake, id=f'old-{i}', state='cancelled', created_at=old,
                         updated_at=old) for i in range(count)]


def _overnight_sell_filled_today(fake: FakeMCPClient) -> dict[str, Any]:
    """Created yesterday (resting overnight), executed today: a prior-day position close."""
    yesterday = (fake.clock() - timedelta(days=1)).isoformat()
    order = broker_order(fake, id='overnight-sell', state='filled', created_at=yesterday,
                         processed_quantity='1.0000',
                         executions=[{'id': 'overnight-fill', 'quantity': '1.0000',
                                      'price': '1.9000',
                                      'timestamp': fake.clock().isoformat()}])
    order['legs'][0].update(side='sell', position_effect='close')
    return order


def test_order_history_beyond_ten_pages_is_read_to_exhaustion(scenario):
    """An overnight order executed today on page 14 is found by execution time and counted."""
    broker, fake, _ = scenario
    fake.orders_page_size = 3
    fake.history = _history_filler(fake, 40) + [_overnight_sell_filled_today(fake)]
    with pytest.raises(LiveSafetyError, match='overnight close'):
        broker.submit_order(entry())
    assert fake.order_pages_read >= 14
    assert fake.place_count == 0
    # Without the far-page overnight execution, the same deep history permits the entry.
    fake.history = _history_filler(fake, 40)
    assert broker.submit_order(entry()).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1


def test_overnight_close_clears_on_the_next_new_york_date(scenario):
    """Denied on the sale's day, per call and never latched; allowed once the NY date rolls."""
    broker, fake, clock = scenario
    fake.position = 0
    fake.history = [_overnight_sell_filled_today(fake)]
    with pytest.raises(LiveSafetyError, match='overnight close'):
        broker.submit_order(entry())
    clock.advance(24 * 60)
    assert broker.submit_order(entry()).state is BrokerOrderState.ACCEPTED
    assert fake.place_count == 1


def test_order_history_that_cannot_be_exhausted_fails_closed(scenario):
    """More pages than the coverage cap is unverifiable: deny entries and risk refresh."""
    broker, fake, _ = scenario
    broker.submit_order(entry())  # an existing order whose status must be re-verified
    fake.orders_page_size = 1
    fake.history = _history_filler(fake, live_broker._ORDER_HISTORY_PAGES + 5)
    with pytest.raises(live_broker.LivePreflightRefusal, match='order history'):
        broker.submit_order(entry('signal-2'))
    with pytest.raises(LiveSafetyError, match='order history'):
        broker.refresh_daily_risk()
    # Reconciliation without full history is UNKNOWN, never "absent", never resent.
    assert broker.get_order('signal-1:entry').state is BrokerOrderState.UNKNOWN
    assert fake.place_count == 1


def test_repeated_history_cursor_fails_closed(scenario):
    broker, fake, _ = scenario
    fake.orders_page_size = 2
    fake.repeat_cursor = True
    fake.history = _history_filler(fake, 10)
    with pytest.raises(live_broker.LivePreflightRefusal):
        broker.submit_order(entry())
    assert fake.place_count == 0


def test_history_outage_mid_pagination_fails_closed(scenario):
    broker, fake, _ = scenario
    fake.orders_page_size = 2
    fake.fail_order_page = 12
    fake.history = _history_filler(fake, 40)
    with pytest.raises(live_broker.LivePreflightRefusal, match='order history'):
        broker.submit_order(entry())
    assert fake.place_count == 0


@pytest.mark.parametrize(('realized', 'unrealized', 'allowed'), [
    ('-549.99', '-200', True),    # net -749.99: inside the $750 latch
    ('-550', '-200', False),      # net -750.00: latched exactly at the ceiling
    ('100', '-850', False),       # a realized gain cannot hide an unrealized loss
    ('-780', '+20', False),       # net -760: an unrealized gain cannot hide a realized loss
])
def test_750_latch_uses_realized_plus_unrealized_at_the_exact_boundary(
        scenario, realized, unrealized, allowed):
    broker, fake, _ = scenario
    fake.realized, fake.unrealized = realized, unrealized.lstrip('+')
    if allowed:
        assert broker.submit_order(entry()).state is BrokerOrderState.ACCEPTED
        assert broker.refresh_daily_risk() is False
    else:
        with pytest.raises(LiveSafetyError, match='latched'):
            broker.submit_order(entry())
        assert fake.place_count == 0
        # The latch persists for the New York day, across a recovery and a restart.
        fake.realized, fake.unrealized = '0', '0'
        restarted = LiveBroker(broker.path, fake, account_number=fake.account,
                               writes_enabled=True, kill_switch=lambda: False,
                               signal_gate=lambda _signal: 'ENTRY', clock=broker.clock)
        with pytest.raises(LiveSafetyError, match='latched'):
            restarted.submit_order(entry('signal-2'))
        restarted.close()
        assert fake.place_count == 0


def test_full_exit_after_partial_fill_sells_exactly_the_filled_quantity(scenario):
    """A partially filled (then cancelled) entry exits its full OWNED filled quantity only."""
    broker, fake, clock = scenario
    legacy = OrderIntent('legacy:entry', 'legacy-order-key', entry().contract,
                         OrderSide.BUY_TO_OPEN, 2, Decimal('2.00'))
    broker._reserve(legacy, option_id=fake.instrument_id, multiplier=100,
                    available=Decimal('1000'))
    with sqlite3.connect(broker.path) as con:
        con.execute('UPDATE live_orders SET write_attempted=1 WHERE logical_id=?',
                    ('legacy:entry',))
    fake.orders.append(broker_order(
        fake, id='legacy-order-broker', quantity='2.0000', price='2.0000',
        state='cancelled', processed_quantity='1.0000',
        executions=[{'id': 'legacy-fill-1', 'quantity': '1.0000', 'price': '1.9900',
                     'timestamp': clock().isoformat()}]))
    status = broker.get_order('legacy:entry')
    assert status.state is BrokerOrderState.CANCELED and status.filled_quantity == 1
    fake.position = 1
    over = OrderIntent('legacy:exit', 'legacy-exit-2', legacy.contract,
                       OrderSide.SELL_TO_CLOSE, 2, Decimal('1.95'))
    with pytest.raises(LiveSafetyError, match='full signal quantity'):
        broker.submit_order(over)
    exact = OrderIntent('legacy:exit', 'legacy-exit-1', legacy.contract,
                        OrderSide.SELL_TO_CLOSE, 1, Decimal('1.95'))
    assert broker.submit_order(exact).state is BrokerOrderState.ACCEPTED
    sell = fake.orders[-1]
    assert Decimal(sell['quantity']) == 1
    assert sell['legs'][0]['side'] == 'sell' and sell['legs'][0]['position_effect'] == 'close'
