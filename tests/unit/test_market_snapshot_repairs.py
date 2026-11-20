"""Regression tests for the USO entry that never traded (MARKET_DATA_UNAVAILABLE).

Root cause: Robinhood publishes a two-tier, price-dependent option tick
(min_ticks: 0.01 below $3.00, 0.05 at or above). resolve_instrument kept no tick
for that shape, so every such contract failed the snapshot tick gate. The tick is
now chosen for the order price, and every failure maps to an explicit code.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    MCPContractError,
    MCPInstrument,
    MCPQuote,
    MCPReadError,
    RobinhoodMCPBroker,
)
from swift_autotrader.live_dispatch import _market_failure_code
from swift_autotrader.paper_dispatch import MarketEvidenceUnavailable
from swift_autotrader.parser.rules import parse_message
from swift_autotrader.signals.identity import OptionContract, OptionRight

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("USO", OptionRight.PUT, Decimal("130"), date(2027, 1, 15))
TWO_TIER = (Decimal("0.01"), Decimal("0.05"), Decimal("3.00"))  # below, above, cutoff
ENTRY_6 = "OPTION - SWING\n\nUSO 130P 1/15 @ 6.00"
ENTRY_2_5 = "OPTION - SWING\n\nUSO 130P 1/15 @ 2.50"


class FakeBroker(RobinhoodMCPBroker):
    """Read-only broker double; overrides only the network-facing methods."""

    def __init__(self, *, tick_schedule=TWO_TIER, minimum_tick=None, bid_size=23, ask_size=161):
        RobinhoodMCPBroker.__init__(self, None)
        self.tick_schedule = tick_schedule
        self.minimum_tick = minimum_tick
        self.bid_size = bid_size
        self.ask_size = ask_size

    async def discover(self):
        return SimpleNamespace(status="READ_ONLY_SCHEMA_OK")

    async def external_account(self):
        return MCPAccount("acct", True, "option_level_2")

    async def buying_power(self, account):
        return Decimal("10000")

    async def _call(self, name, arguments):
        if name == "get_option_chains":
            return {"chains": [{"id": "chain-1", "underlying_symbol": "USO",
                                "expiration_dates": ["2027-01-15"]}]}
        if name == "get_option_instruments":
            return {"instruments": [{
                "id": "opt-uso-130p", "chain_id": "chain-1", "chain_symbol": "USO",
                "strike_price": "130.0000", "expiration_date": "2027-01-15", "type": "put",
                "state": "active", "tradability": "tradable",
                "minimum_tick": self.minimum_tick,
                "min_ticks": None if self.tick_schedule is None else {
                    "below_tick": str(self.tick_schedule[0]),
                    "above_tick": str(self.tick_schedule[1]),
                    "cutoff_price": str(self.tick_schedule[2]),
                },
                "multiplier": None, "trade_value_multiplier": "100.0000",
            }]}
        raise AssertionError(f"unexpected read {name}")

    async def resolve_instrument(self, contract):
        return MCPInstrument("opt-uso-130p", contract, True, "chain-1",
                             minimum_tick=self.minimum_tick, multiplier=100,
                             underlying_type="equity", tick_schedule=self.tick_schedule)

    async def quote(self, instrument, *, now=None, max_age_seconds=30, clock=None):
        return MCPQuote(instrument.option_id, Decimal("6.00"), Decimal("6.30"), NOW,
                        self.bid_size, self.ask_size)

    async def review_order(self, account, instrument, intent):
        return SimpleNamespace(unblocked=True)


class OpenCalendar:
    def is_regular_session_open(self, at):
        return True


def snapshot_for(broker, text):
    provider = MCPReadOnlyOptionData(broker, calendar=OpenCalendar())
    return asyncio.run(provider.snapshot([parse_message(text)], [], NOW))


def test_two_tier_tick_is_chosen_for_the_entry_limit():
    snap = snapshot_for(FakeBroker(), ENTRY_6)
    assert snap.instruments[CONTRACT].premium_tick == Decimal("0.05")
    assert CONTRACT in snap.quotes


def test_below_cutoff_entry_uses_the_fine_tick():
    snap = snapshot_for(FakeBroker(), ENTRY_2_5)
    assert snap.instruments[CONTRACT].premium_tick == Decimal("0.01")


def test_unknown_tick_still_fails_closed():
    broker = FakeBroker(tick_schedule=None, minimum_tick=None)
    with pytest.raises(MarketEvidenceUnavailable, match="Tick, tradability"):
        snapshot_for(broker, ENTRY_6)


def test_missing_displayed_depth_fails_closed():
    with pytest.raises(MarketEvidenceUnavailable, match="depth unavailable"):
        snapshot_for(FakeBroker(bid_size=None), ENTRY_6)


def test_real_resolver_keeps_two_tier_schedule_and_multiplier():
    broker = FakeBroker()
    inst = asyncio.run(RobinhoodMCPBroker.resolve_instrument(broker, CONTRACT))
    assert inst.minimum_tick is None
    assert inst.tick_schedule == TWO_TIER
    assert inst.multiplier == 100
    assert inst.tradable is True


def test_constant_tick_is_unchanged_and_has_no_schedule():
    broker = FakeBroker(tick_schedule=None, minimum_tick=Decimal("0.01"))
    inst = asyncio.run(RobinhoodMCPBroker.resolve_instrument(broker, CONTRACT))
    assert inst.minimum_tick == Decimal("0.01")
    assert inst.tick_schedule is None


@pytest.mark.parametrize(("exc", "code"), [
    (asyncio.TimeoutError(), "MARKET_DATA_TIMEOUT"),
    (MCPContractError("x"), "MARKET_CONTRACT_UNVERIFIED"),
    (MCPReadError("x"), "MARKET_BROKER_READ_FAILED"),
    (MarketEvidenceUnavailable("x"), "MARKET_DATA_UNAVAILABLE"),
])
def test_each_failure_class_maps_to_an_explicit_code(exc, code):
    assert _market_failure_code(exc) == code


def _quote_broker(updated_at: datetime):
    stamp = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    class QuoteOnly(RobinhoodMCPBroker):
        def __init__(self):
            RobinhoodMCPBroker.__init__(self, None)

        async def _call(self, name, arguments):
            assert name == "get_option_quotes"
            return {"results": [{"quote": {
                "instrument_id": "opt-1", "bid_price": "6.00", "ask_price": "6.30",
                "bid_size": 3, "ask_size": 4, "updated_at": stamp}}]}

    return QuoteOnly()


def test_quote_observed_after_snapshot_start_is_fresh_at_response():
    from datetime import timedelta
    start = NOW
    response = NOW + timedelta(seconds=10)
    inst = MCPInstrument("opt-1", CONTRACT, True, "chain-1", minimum_tick=Decimal("0.05"), multiplier=100)
    broker = _quote_broker(start + timedelta(seconds=2))  # quoted after the snapshot started
    quote = asyncio.run(RobinhoodMCPBroker.quote(broker, inst, max_age_seconds=15, clock=lambda: response))
    assert quote.observed_at_utc == start + timedelta(seconds=2)


def test_quote_stale_at_response_is_still_rejected():
    from swift_autotrader.broker.robinhood_mcp import MCPContractError
    from datetime import timedelta
    response = NOW + timedelta(seconds=10)
    inst = MCPInstrument("opt-1", CONTRACT, True, "chain-1", minimum_tick=Decimal("0.05"), multiplier=100)
    broker = _quote_broker(NOW - timedelta(seconds=14))  # 24 s old when it arrived
    with pytest.raises(MCPContractError, match="stale or crossed"):
        asyncio.run(RobinhoodMCPBroker.quote(broker, inst, max_age_seconds=15, clock=lambda: response))
