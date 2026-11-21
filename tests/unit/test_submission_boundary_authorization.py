"""Final authorization boundary: scope must still hold at place_option_order.

Synthetic brokerage only. No real authorization is generated and no real
write endpoint is contacted.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from swift_autotrader.broker.base import OrderIntent, OrderSide
from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodMCPBroker,
)
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.orders.acceptance_scope import AcceptanceScope
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP
from tests.unit.test_supervised_lifecycle import OpenMarketCalendar, managed_live_env  # noqa: F401

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 12, 18))


class HookedMCP(StatefulFakeMCP):
    """Runs a side effect when a named tool is called (simulates latency/edits)."""

    def __init__(self, *a, hooks=None, review_override=None, **kw):
        super().__init__(*a, **kw)
        self.hooks = hooks or {}
        self.review_override = review_override
        self.seen: dict[str, int] = {}

    async def call_tool(self, name, arguments):
        # Each tool runs once for the market snapshot, then again on the
        # submission path. Fire hooks/overrides on the submission-path call.
        self.seen[name] = self.seen.get(name, 0) + 1
        submission_path = self.seen[name] >= 2
        if name in self.hooks and submission_path:
            self.hooks[name]()
        if (name == "review_option_order" and self.review_override is not None
                and submission_path):
            self.calls.append((name, arguments))
            return self.review_override
        return await super().call_tool(name, arguments)


def _setup(settings, live_db, *, expires_in=90, hooks=None, review_override=None):
    store = TelegramStore(live_db, settings.telegram_channel_id)
    store.ingest(
        TelegramMessage(
            channel_id=settings.telegram_channel_id, message_id=1001,
            sent_at_utc=NOW, received_at_utc=NOW, body="BTO SPY 680C 12/18 @ 1.20",
            kind=EventKind.NEW, origin=EventOrigin.LIVE,
        ),
        session_started_at_utc=NOW - timedelta(minutes=5),
    )
    with sqlite3.connect(live_db) as conn:
        rev = conn.execute(
            "SELECT revision_id FROM telegram_message_revisions WHERE message_id=1001"
        ).fetchone()[0]
    scope = AcceptanceScope(
        settings.robinhood_mcp_account, settings.telegram_channel_id, 1001, rev,
        CONTRACT, 1, Decimal("1.25"), NOW + timedelta(seconds=expires_in),
    )
    clock = FakeClock(NOW)
    fake = HookedMCP(
        account_number=settings.robinhood_mcp_account, buying_power=Decimal("10000"),
        clock=clock, hooks={k: (lambda f=f: f(clock, live_db)) for k, f in (hooks or {}).items()},
        review_override=review_override,
    )
    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True,
                         options_level="option_level_2")
    mcp = RobinhoodMCPBroker(fake, approved_account_number=settings.robinhood_mcp_account)
    dispatcher = LiveDispatcher(
        live_db, settings.telegram_channel_id, mcp, account,
        MCPReadOnlyOptionData(mcp, calendar=OpenMarketCalendar(), clock=clock), settings,
        clock=clock, single_entry=True, acceptance_scope=scope,
    )
    return dispatcher, fake


def _expire(clock, _db):
    clock.advance(120)


def _supersede(_clock, db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_messages SET latest_revision_id=latest_revision_id+1 "
                     "WHERE message_id=1001")


def _placements(fake):
    return sum(name == "place_option_order" for name, _ in fake.calls)


def _intent_state(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT state FROM order_intents").fetchall()


def test_control_valid_scope_places_exactly_once(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db)
    assert asyncio.run(dispatcher.run_once()).entry_orders_submitted == 1
    assert _placements(fake) == 1


@pytest.mark.parametrize("tool", ["get_option_instruments", "review_option_order"])
def test_expiry_after_intent_blocks_placement(managed_live_env, tool):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db, hooks={tool: _expire})
    asyncio.run(dispatcher.run_once())  # refused unsent; the cycle itself completes
    assert _placements(fake) == 0
    assert _intent_state(live_db) == [("REJECTED",)]
    # A later cycle must never re-attempt the refused entry.
    asyncio.run(dispatcher.run_once())
    assert _placements(fake) == 0


def test_superseded_revision_during_review_blocks_placement(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db, hooks={"review_option_order": _supersede})
    asyncio.run(dispatcher.run_once())  # refused unsent; the cycle itself completes
    assert _placements(fake) == 0
    assert _intent_state(live_db) == [("REJECTED",)]


def test_provider_required_approval_is_definitive_non_submission(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db, review_override={"structuredContent": {"data": {
        "order_checks": [], "approval_required": True}}})
    asyncio.run(dispatcher.run_once())  # refused unsent; the cycle itself completes
    assert _placements(fake) == 0
    assert _intent_state(live_db) == [("REJECTED",)]


def test_malformed_review_is_definitive_non_submission(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db, review_override={"structuredContent": {"data": {}}})
    asyncio.run(dispatcher.run_once())  # refused unsent; the cycle itself completes
    assert _placements(fake) == 0
    assert _intent_state(live_db) == [("REJECTED",)]


def test_guard_exception_fails_closed(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, fake = _setup(settings, live_db)

    def boom(_intent):
        raise sqlite3.OperationalError("database is locked")
    dispatcher.live_broker.submission_guard = boom
    asyncio.run(dispatcher.run_once())  # refused unsent; the cycle itself completes
    assert _placements(fake) == 0
    assert _intent_state(live_db) == [("REJECTED",)]


def test_guard_rejects_changed_terms_and_duplicates_but_never_blocks_exits(managed_live_env):
    _, settings, _, live_db = managed_live_env
    dispatcher, _fake = _setup(settings, live_db)
    assert asyncio.run(dispatcher.run_once()).entry_orders_submitted == 1
    with sqlite3.connect(live_db) as conn:
        oid, key, limit = conn.execute(
            "SELECT logical_order_id,idempotency_key,limit_price FROM order_intents").fetchone()
    guard = dispatcher._authorize_submission
    entry = OrderIntent(oid, key, CONTRACT, OrderSide.BUY_TO_OPEN, 1, Decimal(limit))
    # Already submitted (broker id/state advanced): a second placement is refused.
    assert guard(entry) is False
    # Unknown/changed logical order: refused.
    assert guard(OrderIntent("other:entry", key, CONTRACT, OrderSide.BUY_TO_OPEN, 1,
                             Decimal(limit))) is False
    # Exits remain permitted even after the entry authorization has expired.
    dispatcher.clock.advance(3600)
    exit_intent = OrderIntent(oid.replace("entry", "close"), key + "x", CONTRACT,
                              OrderSide.SELL_TO_CLOSE, 1, Decimal("1.00"))
    assert guard(exit_intent) is True
