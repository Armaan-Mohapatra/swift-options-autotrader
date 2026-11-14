"""No credentials, no MCP network calls and no brokerage writes."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from swift_autotrader.broker.mcp_paper_data import (
    ExchangeCalendarsSession,
    MCPReadOnlyOptionData,
    MarketEvidenceUnavailable,
)
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    MCPInstrument,
    MCPQuote,
    OrderReview,
    ReadOnlyDiagnostic,
    RobinhoodMCPBroker,
)
from swift_autotrader.parser.rules import parse_message
from swift_autotrader.signals.identity import OptionContract, OptionRight

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
CALL = OptionContract('SPY', OptionRight.CALL, Decimal('680'), date(2026, 11, 13))
OTHER = OptionContract('SPY', OptionRight.CALL, Decimal('680'), date(2026, 11, 20))


class FakeCalendar:
    def __init__(self, open_=True):
        self.open = open_

    def is_regular_session_open(self, at_utc):
        assert at_utc == NOW
        return self.open


class FakeReadBroker(RobinhoodMCPBroker):
    def __init__(self):
        super().__init__(None)
        self.calls: list[str] = []
        self.reviewed: list[tuple[OptionContract, Decimal]] = []
        self.tick = Decimal('0.01')
        self.multiplier = 100
        self.depth = 6
        self.allowed = True
        self.dates = [CALL.expiration.isoformat(), OTHER.expiration.isoformat()]
        self.instrument_id_prefix = 'synthetic-option-id'
        self.review_allowed = True
        self.schema_ok = True

    async def discover(self):
        self.calls.append('discover')
        return ReadOnlyDiagnostic(
            'READ_ONLY_SCHEMA_OK' if self.schema_ok else 'READ_ONLY_SCHEMA_INCOMPLETE',
            tuple(), tuple(), tuple(),
        )

    async def _call(self, name, args):
        self.calls.append(name)
        if name != 'get_option_chains':
            raise AssertionError('only read-only option-chain discovery expected')
        assert args == {'underlying_symbol': 'SPY'}
        return {'chains': [{'underlying_symbol': 'SPY', 'expiration_dates': self.dates}]}

    async def external_account(self):
        self.calls.append('external_account')
        return MCPAccount('SYNTHETIC-ACCOUNT', True, 'level_2')

    async def buying_power(self, account):
        self.calls.append('buying_power')
        assert account.eligible
        return Decimal('2000')

    async def resolve_instrument(self, contract):
        self.calls.append('resolve_instrument')
        return MCPInstrument(
            self.instrument_id_prefix + contract.expiration.isoformat(),
            contract, self.allowed, 'synthetic-chain', self.tick, self.multiplier,
        )

    async def quote(self, instrument, *, now=None, max_age_seconds=30):
        self.calls.append('quote')
        assert now == NOW and max_age_seconds == 15
        return MCPQuote(
            instrument.option_id, Decimal('1.10'), Decimal('1.15'), NOW,
            self.depth, self.depth,
        )

    async def review_order(self, account, instrument, intent):
        self.calls.append('review_option_order')
        assert intent.contract == instrument.contract
        self.reviewed.append((intent.contract, intent.limit_price))
        return OrderReview(True, (), not self.review_allowed)


def run(broker, entries=(), active=(), calendar=None):
    provider = MCPReadOnlyOptionData(
        broker, calendar=calendar or FakeCalendar(), contracts_per_signal=2,
    )
    return asyncio.run(provider.snapshot(entries, active, NOW))


def test_exact_contract_verified_quote_and_order_review_with_no_write():
    broker = FakeReadBroker()
    snapshot = run(broker, [parse_message('SPY LOTTO 680C 11/13 @ 1.20')])
    assert snapshot.source == 'MCP_READONLY'
    assert list(snapshot.instruments) == [CALL]
    assert snapshot.instruments[CALL].premium_tick == Decimal('0.01')
    assert snapshot.quotes[CALL].ask_size == 6
    assert broker.reviewed == [(CALL, Decimal('1.20'))]
    assert not ({'place_option_order', 'cancel_option_order'} & set(broker.calls))


def test_exact_expiration_review_does_not_touch_same_strike_other_date():
    broker = FakeReadBroker()
    snapshot = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')], [OTHER])
    assert set(snapshot.quotes) == {CALL, OTHER}
    assert broker.reviewed == [(CALL, Decimal('1.20'))]


@pytest.mark.parametrize('field,value', [
    ('tick', None), ('tick', Decimal('0')), ('multiplier', None),
    ('multiplier', 10), ('depth', None), ('allowed', False),
])
def test_missing_required_verified_field_never_fabricated(field, value):
    broker = FakeReadBroker()
    setattr(broker, field, value)
    with pytest.raises(MarketEvidenceUnavailable):
        run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    assert not ({'place_option_order', 'cancel_option_order'} & set(broker.calls))


def test_account_read_only_review_requiring_approval_blocks():
    broker = FakeReadBroker()
    broker.review_allowed = False
    with pytest.raises(MarketEvidenceUnavailable, match='review blocked'):
        run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])


def test_duplicate_expiration_year_is_ambiguous_and_fails_closed():
    broker = FakeReadBroker()
    broker.dates = ['2026-11-13', '2027-11-13']
    with pytest.raises(MarketEvidenceUnavailable, match='Exact expiration'):
        run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])


def test_off_session_returns_explicit_false_not_success():
    broker = FakeReadBroker()
    snapshot = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')], calendar=FakeCalendar(False))
    assert not snapshot.instruments[CALL].market_session_open


def test_unverified_read_tool_schema_stops_before_account_reads():
    broker = FakeReadBroker()
    broker.schema_ok = False
    with pytest.raises(MarketEvidenceUnavailable, match='incompatible'):
        run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    assert broker.calls == ['discover']


def test_expiration_without_market_data_does_not_infer_historical_year():
    broker = FakeReadBroker()
    broker.dates = []
    with pytest.raises(MarketEvidenceUnavailable, match='Exact expiration'):
        run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])


def test_calendar_dependency_absence_must_fail_closed():
    real_import = __import__

    def without_exchange_calendar(name, *args, **kwargs):
        if name == 'exchange_calendars':
            raise ImportError('test: deliberately absent')
        return real_import(name, *args, **kwargs)

    with (
        patch('builtins.__import__', side_effect=without_exchange_calendar),
        pytest.raises(MarketEvidenceUnavailable, match='calendar|session'),
    ):
        ExchangeCalendarsSession().is_regular_session_open(NOW)


def test_bounded_review_rejects_excessive_candidate_set_before_mcp_calls():
    broker = FakeReadBroker()
    entries = [parse_message("SPY 680C 11/13 @ 1.20")] * 13
    with pytest.raises(MarketEvidenceUnavailable, match="Too many contracts"):
        run(broker, entries)
    assert broker.calls == []

def test_same_option_timestamp_with_changed_quote_content_has_distinct_evidence():
    broker = FakeReadBroker()
    def custom_quote(bid, ask):
        async def quote(instrument, *, now=None, max_age_seconds=30):
            return MCPQuote(instrument.option_id, bid, ask, NOW, 6, 6)
        return quote
    broker.quote = custom_quote(Decimal('1.10'), Decimal('1.15'))
    one = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    broker.quote = custom_quote(Decimal('1.11'), Decimal('1.16'))
    two = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    assert one.quotes[CALL].quote_id != two.quotes[CALL].quote_id
    assert one.quotes[CALL].evidence_reference != two.quotes[CALL].evidence_reference


def test_same_option_identical_quote_content_is_idempotent():
    broker = FakeReadBroker()
    a = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    b = run(broker, [parse_message('SPY 680C 11/13 @ 1.20')])
    assert a.quotes[CALL].quote_id == b.quotes[CALL].quote_id
