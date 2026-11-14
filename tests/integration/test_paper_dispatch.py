"""Full paper pipeline with only synthetic market evidence and Telegram events."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from swift_autotrader.analytics.metrics import CostModel, PaperAnalytics
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.orders.risk import Preflight, RiskCeilings
from swift_autotrader.paper_dispatch import (
    MarketEvidenceUnavailable,
    MarketSnapshot,
    PaperDispatcher,
    VerifiedQuote,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage

CHANNEL = -1001234567000
START = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self):
        return self.now


class Feed:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.down = False
        self.quoted = True
        self.ask = Decimal("1.15")
        self.bid = Decimal("1.10")
        self.ask_size = 2
        self.bid_size = 2
        self.reference = 0
        self.market_open = True

    async def snapshot(self, entries, active, now):
        if self.down:
            raise MarketEvidenceUnavailable("Read-only feed disconnected")
        self.reference += 1
        quote = VerifiedQuote(
            f"mock-q-{self.reference}", CONTRACT, self.bid, self.ask,
            self.bid_size, self.ask_size, now, "SYNTHETIC_TEST", "fixture-quote",
        )
        check = Preflight(
            True, True, self.market_open, Decimal("10000"), Decimal("0.01"), CONTRACT,
        )
        return MarketSnapshot(
            {CONTRACT: check}, {CONTRACT: quote} if self.quoted else {},
            "SYNTHETIC_TEST", now,
        )


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / "paper.sqlite3"
    apply_migrations(path)
    clock = Clock()
    feed = Feed(clock)
    store = TelegramStore(path, CHANNEL)
    limits = RiskCeilings(
        contracts_per_signal=2, premium_per_signal_usd=Decimal("1000"),
        total_exposure_usd=Decimal("3000"), open_signal_count=5,
    )
    worker = PaperDispatcher(path, CHANNEL, feed, limits=limits, clock=clock)
    return path, clock, feed, store, worker


def ingest(rig, msg, mid, *, reply=None, kind=EventKind.NEW, origin=EventOrigin.LIVE):
    _, clock, _, store, _ = rig
    return store.ingest(
        TelegramMessage(
            CHANNEL, mid, clock.now, clock.now, msg, kind=kind,
            origin=origin, edited_at_utc=clock.now if kind == EventKind.EDIT else None,
            reply_to_message_id=reply,
        ),
        session_started_at_utc=START - timedelta(seconds=1),
    )


def cycle(rig):
    return asyncio.run(rig[4].run_once())


def test_full_telegram_to_paper_to_analytics_lotto_and_average(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, "SPY LOTTO 680C 11/13 @ 1.20", 101)
    first = cycle(rig)
    assert first.entry_orders == 1 and first.simulated_fills == 0
    clock.now += timedelta(seconds=2)
    second = cycle(rig)
    assert second.simulated_fills == 2
    clock.now += timedelta(seconds=2)
    ingest(rig, "SPY NEW AVG .95", 102, reply=101)
    assert cycle(rig).entry_orders == 0
    clock.now += timedelta(seconds=2)
    ingest(rig, "SPY SOLD HALF", 103, reply=101)
    feed.bid = Decimal("1.36")
    feed.ask = Decimal("1.40")
    assert cycle(rig).exit_orders == 1
    clock.now += timedelta(seconds=2)
    end = cycle(rig)
    assert end.simulated_fills == 2
    report = PaperAnalytics(
        path, costs=CostModel(fee_per_contract_side_usd=Decimal("0.65")),
    ).snapshot(as_of=clock.now)
    assert Decimal(report["realized_gross_usd"]) == Decimal("42.00")
    assert Decimal(report["realized_net_usd"]) == Decimal("39.40")
    with sqlite3.connect(path) as db:
        entry = db.execute(
            "SELECT limit_price FROM order_intents WHERE side='BUY_TO_OPEN'"
        ).fetchone()[0]
        avg = db.execute("SELECT swift_reported_avg FROM option_signals").fetchone()[0]
    assert entry == "1.20" and Decimal(avg) == Decimal("0.95")


def test_provider_outage_preserves_unparsed_source(rig):
    path, _, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.down = True
    outcome = cycle(rig)
    assert outcome.status == "PAUSED_DATA_UNAVAILABLE" and outcome.entry_orders == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM signal_parser_decisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
    feed.down = False
    assert cycle(rig).entry_orders == 1


def test_no_quote_preserves_source_and_never_assumes_fill(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.quoted = False
    assert cycle(rig).entry_orders == 0
    feed.quoted = True
    feed.ask = Decimal("1.50")
    feed.bid = Decimal("1.45")
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0


def test_exact_3600_cancel_still_runs_when_provider_disconnected(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.ask = Decimal("1.40")
    feed.bid = Decimal("1.39")
    assert cycle(rig).entry_orders == 1
    feed.down = True
    clock.now += timedelta(seconds=3599)
    assert cycle(rig).cancellations == 0
    clock.now += timedelta(seconds=1)
    assert cycle(rig).cancellations == 1
    with sqlite3.connect(path) as db:
        state = db.execute(
            "SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'"
        ).fetchone()[0]
    assert state in {"CANCELED", "CANCEL_PENDING"}


@pytest.mark.parametrize("msg", ["BUY 50 SHARES SPY", "BTC GOING UP", "SPY MACRO COMMENTARY"])
def test_non_option_never_orders(rig, msg):
    ingest(rig, msg, 101)
    assert cycle(rig).entry_orders == 0


def test_add_and_duplicate_never_open_more(rig):
    _, clock, _, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=1)
    ingest(rig, "SPY ADDING MORE", 102, reply=101)
    assert cycle(rig).entry_orders == 0
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    assert cycle(rig).entry_orders == 0


def test_conflicting_quote_missing_provenance_halts(rig):
    _, _, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.quoted = False
    assert cycle(rig).status == "PAUSED_DATA_UNAVAILABLE"


def test_missing_verified_catalog_refuses_pending_entry(rig):
    path, _, _, _, _ = rig
    ingest(rig, "QQQ 480C 11/13 @ 1.20", 101)
    assert cycle(rig).status == "PAUSED_DATA_UNAVAILABLE"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0


def test_recovered_telegram_entry_never_requests_market_contract(rig):
    _, _, _, _, worker = rig
    outcome = ingest(rig, "SPY 680C 11/13 @ 1.20", 101, origin=EventOrigin.RECOVERED)
    assert not outcome.eligible_for_future_processing
    entries, _active = worker._pending_and_active()
    assert entries == []
    assert cycle(rig).entry_orders == 0

# Phase 8 secondary-audit regression cases, authored before implementation fixes.
def test_unverifiable_pending_entry_does_not_block_unrelated_full_exit(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, 'SPY 680C 11/13 @ 1.20', 101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    ingest(rig, 'QQQ 480C 11/13 @ 1.20', 102)
    ingest(rig, 'SPY SOLD HALF', 103, reply=101)
    feed.bid, feed.ask = Decimal('1.30'), Decimal('1.35')
    response = cycle(rig)
    assert response.exit_orders == 1
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM signal_close_requests").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM signal_parser_decisions p JOIN telegram_message_revisions r ON p.revision_id=r.revision_id WHERE r.message_id=102").fetchone()[0] == 0


def test_canceled_zero_fill_not_in_active_quote_set(rig):
    path, clock, feed, _, worker = rig
    ingest(rig, 'SPY 680C 11/13 @ 1.20', 101)
    feed.ask, feed.bid = Decimal('1.50'), Decimal('1.45')
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=3600)
    assert cycle(rig).cancellations == 1
    assert worker._pending_and_active()[1] == []
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0


def test_stale_unverified_source_never_blocks_fresh_eligible_entry(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, 'QQQ 480C 11/13 @ 1.20', 101)
    assert cycle(rig).status == 'PAUSED_DATA_UNAVAILABLE'
    clock.now += timedelta(seconds=121)
    ingest(rig, 'SPY 680C 11/13 @ 1.20', 102)
    result = cycle(rig)
    assert result.entry_orders == 1
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0] == 1


def test_thirteen_pending_entries_do_not_hit_provider_cap_forever(rig):
    path, clock, feed, _, _ = rig
    original_snapshot = feed.snapshot
    async def capped(entries, active, now):
        if len(entries) > 12:
            raise MarketEvidenceUnavailable('Too many pending entries')
        return await original_snapshot(entries, active, now)
    feed.snapshot = capped
    for idx in range(13):
        ingest(rig, 'SPY 680C 11/13 @ 1.20', 101 + idx)
    first = cycle(rig)
    assert first.status == 'PAPER_ONLY'
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT COUNT(*) FROM signal_parser_decisions').fetchone()[0] == 13


def test_market_data_failure_preserves_checkpoint_without_duplicate_buy(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, 'SPY 680C 11/13 @ 1.20', 101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=1)
    ingest(rig, 'QQQ 480C 11/13 @ 1.20', 102)
    feed.down = True
    for _ in range(2):
        assert cycle(rig).entry_orders == 0
    feed.down = False
    assert cycle(rig).entry_orders == 0
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()[0] == 1


def test_exit_without_quote_persists_unresolved_exposure_alert(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, 'SPY 680C 11/13 @ 1.20', 101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    ingest(rig, 'QQQ 480C 11/13 @ 1.20', 102)
    ingest(rig, 'SPY ALL OUT', 103, reply=101)
    feed.quoted = False
    result = cycle(rig)
    assert result.exit_orders == 0
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM signal_close_requests").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM paper_engine_alerts WHERE code='EXIT_QUOTE_MISSING' AND resolved=0").fetchone()[0] > 0 or con.execute("SELECT COUNT(*) FROM paper_engine_alerts WHERE code='EXIT_PRICE_UNKNOWN' AND resolved=0").fetchone()[0] > 0
        assert con.execute("SELECT COUNT(*) FROM order_intents WHERE side='SELL_TO_CLOSE'").fetchone()[0] == 0


def test_large_burst_is_bounded_and_drains_over_multiple_cycles(rig):
    """More than one provider batch is handled without overloading a poll."""
    path, _clock, feed, _, _worker = rig
    seen_batch_sizes = []
    original_snapshot = feed.snapshot

    async def bounded_snapshot(entries, active, now):
        seen_batch_sizes.append(len(entries))
        assert len(entries) <= 12
        return await original_snapshot(entries, active, now)

    feed.snapshot = bounded_snapshot
    for index in range(61):
        ingest(rig, "SPY 680C 11/13 @ 1.20", 1500 + index)
    first = cycle(rig)
    assert first.parsed_events == 48
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM signal_parser_decisions").fetchone()[0] == 48
    second = cycle(rig)
    assert second.parsed_events == 13
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM signal_parser_decisions").fetchone()[0] == 61
    assert seen_batch_sizes and max(seen_batch_sizes) <= 12


def test_due_cancellation_occurs_before_network_provider_request(rig):
    """The 60-minute watchdog must run even if the next quote request stalls."""
    path, clock, feed, _, _worker = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 1101)
    feed.ask = Decimal("1.60")
    feed.bid = Decimal("1.55")
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=3600)

    async def check_cancel_before_data(*args):
        with sqlite3.connect(path) as db:
            status = db.execute(
                "SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'"
            ).fetchone()[0]
        assert status in {"CANCELED", "CANCEL_PENDING"}
        raise MarketEvidenceUnavailable("Intentionally offline after deadline")

    calls = 0

    async def check_no_unnecessary_network(*args):
        nonlocal calls
        calls += 1
        return await check_cancel_before_data(*args)

    feed.snapshot = check_no_unnecessary_network
    result = cycle(rig)
    assert result.status == "PAPER_ONLY"
    assert calls == 0, "No active contracts remain after cancellation"

# Independent chat-audit regressions against the PR #22 paper dispatcher.
def test_pre_network_watchdog_does_not_create_false_exit_quote_alert(rig):
    from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog
    path, clock, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 1101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    ingest(rig, "SPY ALL OUT", 1102, reply=1101)
    SignalMatcher(path, CHANNEL, catalog=StaticInstrumentCatalog([CONTRACT])).process_pending()
    feed.bid, feed.ask = Decimal("1.35"), Decimal("1.40")
    assert cycle(rig).exit_orders == 1
    with sqlite3.connect(path) as db:
        false_alerts = db.execute(
            "SELECT COUNT(*) FROM paper_engine_alerts WHERE code='EXIT_QUOTE_MISSING' AND resolved=0"
        ).fetchone()[0]
    assert false_alerts == 0, "A verified fresh quote was available during the cycle"


def test_crash_after_persisted_parser_decision_recovers_paper_entry(rig):
    from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog
    path, _, _, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 1101)
    decided = SignalMatcher(path, CHANNEL, catalog=StaticInstrumentCatalog([CONTRACT])).process_pending()
    assert len(decided) == 1 and decided[0].status == "RECORDED"
    first = cycle(rig)
    assert first.entry_orders == 1, "Durable source signals need fresh evidence and replay"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM order_intents WHERE side='BUY_TO_OPEN'"
        ).fetchone()[0] == 1


def test_verified_recovered_entry_does_not_block_unverified_other_signal(rig):
    from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog
    path, _, _, _, _ = rig
    other = OptionContract("QQQ", OptionRight.CALL, Decimal("480"), date(2026, 11, 13))
    ingest(rig, "SPY 680C 11/13 @ 1.20", 1201)
    ingest(rig, "QQQ 480C 11/13 @ 1.20", 1202)
    result = SignalMatcher(
        path, CHANNEL, catalog=StaticInstrumentCatalog([CONTRACT, other])
    ).process_pending()
    assert len(result) == 2 and all(decision.status == "RECORDED" for decision in result)
    assert cycle(rig).entry_orders == 1
    with sqlite3.connect(path) as db:
        names = db.execute(
            "SELECT s.underlying FROM order_intents o "
            "JOIN option_signals s ON o.signal_id=s.signal_id"
        ).fetchall()
        unrelated_decisions = db.execute(
            "SELECT COUNT(*) FROM paper_engine_decisions d "
            "JOIN option_signals s ON d.signal_id=s.signal_id WHERE s.underlying='QQQ'"
        ).fetchone()[0]
    assert names == [("SPY",)]
    assert unrelated_decisions == 0, "QQQ must remain unblocked without market evidence"


def test_missing_quote_alert_resolves_only_after_confirmed_full_exit(rig):
    path, clock, feed, _, _ = rig
    ingest(rig, "SPY 680C 11/13 @ 1.20", 1301)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    ingest(rig, "SPY ALL OUT", 1302, reply=1301)
    feed.quoted = False
    assert cycle(rig).exit_orders == 0
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM paper_engine_alerts "
            "WHERE code='EXIT_QUOTE_MISSING' AND resolved=0"
        ).fetchone()[0] >= 1
    feed.quoted = True
    feed.bid, feed.ask = Decimal("1.36"), Decimal("1.40")
    assert cycle(rig).exit_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM paper_engine_alerts "
            "WHERE code='EXIT_QUOTE_MISSING' AND resolved=0"
        ).fetchone()[0] == 0

def test_large_outage_has_bounded_provider_calls_and_preserves_source(rig):
    path, _, feed, _, _ = rig
    for index in range(48):
        ingest(rig, "SPY 680C 11/13 @ 1.20", 2000 + index)
    calls = 0

    async def failing_feed(entries, active, now):
        nonlocal calls
        calls += 1
        raise MarketEvidenceUnavailable("Simulated unavailable read-only provider")

    feed.snapshot = failing_feed
    result = cycle(rig)
    assert result.entry_orders == 0
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM signal_parser_decisions"
        ).fetchone()[0] == 0
    assert calls <= 24, f"A single cycle called an unavailable provider {calls} times"
