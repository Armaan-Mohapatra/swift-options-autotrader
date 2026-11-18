"""Synthetic end-to-end refusal containment; no live transport or credentials."""
import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import MCPAccount, RobinhoodMCPBroker
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.orders.exposure_state import unresolved_exit_signal_ids
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP
from tests.unit.test_supervised_lifecycle import OpenMarketCalendar, managed_live_env  # noqa: F401


def test_refused_exit_blocks_new_entries_and_survives_dispatcher_reconstruction(managed_live_env):
    _, settings, _, live_db = managed_live_env
    clock = FakeClock(datetime(2026, 11, 16, 15, 0, tzinfo=UTC))
    store = TelegramStore(live_db, settings.telegram_channel_id)
    fake = StatefulFakeMCP(account_number=settings.robinhood_mcp_account,
                           buying_power=Decimal("10000.00"), clock=clock)
    account = MCPAccount(settings.robinhood_mcp_account, agentic_allowed=True,
                         options_level="option_level_2")

    def dispatcher():
        mcp = RobinhoodMCPBroker(fake, approved_account_number=settings.robinhood_mcp_account)
        return LiveDispatcher(live_db, settings.telegram_channel_id, mcp, account,
                              MCPReadOnlyOptionData(mcp, calendar=OpenMarketCalendar()),
                              settings, clock=clock)

    def ingest(mid, body, reply=None):
        store.ingest(TelegramMessage(
            channel_id=settings.telegram_channel_id, message_id=mid,
            sent_at_utc=clock.now(), received_at_utc=clock.now(), body=body,
            kind=EventKind.NEW, origin=EventOrigin.LIVE, reply_to_message_id=reply,
        ), session_started_at_utc=clock.now() - timedelta(minutes=5))

    ingest(901, "BTO SPY 680C 11/20 @ 1.20")
    first = dispatcher()
    assert asyncio.run(first.run_once()).entry_orders_submitted == 1
    fake.fill_order(next(iter(fake.orders)), filled_quantity=1, fill_price="1.20")
    asyncio.run(first.run_once())
    clock.advance(30)
    fake.set_quote("opt-spy-680c", "1.80", "1.85", updated_at=clock.now())
    fake.set_quote("opt-qqq-500c", "2.00", "2.02", updated_at=clock.now())
    ingest(902, "SPY ALL OUT", reply=901)
    ingest(903, "BTO QQQ 500C 11/20 @ 2.00")
    original = fake.call_tool

    async def review_refusal(name, arguments):
        if name == "review_option_order" and arguments["legs"][0]["side"] == "sell":
            return {"structuredContent": {"data": {
                "order_checks": [], "approval_required": True,
            }}}
        return await original(name, arguments)

    fake.call_tool = review_refusal
    denied = asyncio.run(first.run_once())
    assert denied.close_orders_submitted == denied.entry_orders_submitted == 0
    assert denied.unresolved_exit_count == 1
    with sqlite3.connect(live_db) as db:
        sid, state = db.execute(
            "SELECT signal_id,state FROM order_intents WHERE side='SELL_TO_CLOSE'"
        ).fetchone()
        assert state == "REJECTED"
        assert unresolved_exit_signal_ids(db) == {sid}
        assert db.execute("SELECT state FROM option_signals WHERE signal_id=?", (sid,)).fetchone()[0] == "ALERT_REQUIRED"
        assert db.execute("SELECT count(*) FROM paper_engine_alerts WHERE code='UNFILLED_EXIT' AND resolved=0").fetchone()[0] == 1
    assert len(fake.orders) == 1
    # Fresh adapter/dispatcher, same durable database; no ID injection.
    second = dispatcher()
    again = asyncio.run(second.run_once())
    assert again.unresolved_exit_count == 1
    assert again.entry_orders_submitted == again.close_orders_submitted == 0
    assert len(fake.orders) == 1
