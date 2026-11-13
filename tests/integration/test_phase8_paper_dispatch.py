"""No authenticated data, external orders, or user-account access in this suite."""
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
    MarketEvidenceUnavailable, MarketSnapshot, PaperDispatcher, VerifiedQuote,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.telegram.events import TelegramMessage

CHANNEL = -1001234567000
START = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Evidence:
    def __init__(self):
        self.down = False
        self.ask = Decimal("1.15")
        self.bid = Decimal("1.10")
        self.size = 2
        self.counter = 0
        self.quoted = True

    async def snapshot(self, entries, active, now):
        if self.down:
            raise MarketEvidenceUnavailable("Simulated network disconnected")
        self.counter += 1
        quote = VerifiedQuote(
            f"synthetic-{self.counter}", CONTRACT, self.bid, self.ask,
            self.size, self.size, now, "SYNTHETIC_TEST", "local-test-only",
        )
        check = Preflight(True, True, True, Decimal("10000"), Decimal("0.01"), CONTRACT)
        return MarketSnapshot(
            {CONTRACT: check}, {CONTRACT: quote} if self.quoted else {},
            "SYNTHETIC_TEST", now,
        )


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / "paper.sqlite3"
    apply_migrations(path)
    clock = Clock()
    feed = Evidence()
    store = TelegramStore(path, CHANNEL)
    limits = RiskCeilings(
        contracts_per_signal=2, premium_per_signal_usd=Decimal("1000"),
        total_exposure_usd=Decimal("3000"), open_signal_count=5,
    )
    worker = PaperDispatcher(path, CHANNEL, feed, limits=limits, clock=clock)
    return path, clock, feed, store, worker


def message(rig, text, mid, *, reply=None):
    _, clock, _, store, _ = rig
    return store.ingest(
        TelegramMessage(CHANNEL, mid, clock.now, clock.now, text,
                        reply_to_message_id=reply),
        session_started_at_utc=START - timedelta(seconds=1),
    )


def cycle(rig):
    return asyncio.run(rig[4].run_once())


@pytest.mark.parametrize("prefix", ["SPY", "SPY LOTTO"])
def test_original_price_quote_based_fill_and_full_exit(prefix, rig):
    path, clock, feed, _, _ = rig
    message(rig, f"{prefix} 680C 11/13 @ 1.20", 101)
    first = cycle(rig)
    assert first.entry_orders == 1 and first.simulated_fills == 0
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    clock.now += timedelta(seconds=2)
    message(rig, "SPY NEW AVG .95", 102, reply=101)
    assert cycle(rig).entry_orders == 0
    clock.now += timedelta(seconds=2)
    message(rig, "SPY SOLD HALF", 103, reply=101)
    feed.bid, feed.ask = Decimal("1.36"), Decimal("1.40")
    assert cycle(rig).exit_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 2
    report = PaperAnalytics(
        path, costs=CostModel(fee_per_contract_side_usd=Decimal("0.65"))
    ).snapshot(as_of=clock.now)
    assert Decimal(report["realized_gross_usd"]) == Decimal("42")
    assert Decimal(report["realized_net_usd"]) == Decimal("39.40")
    with sqlite3.connect(path) as db:
        entry = db.execute(
            "SELECT limit_price FROM order_intents WHERE side='BUY_TO_OPEN'"
        ).fetchone()[0]
        average = db.execute(
            "SELECT swift_reported_avg FROM option_signals"
        ).fetchone()[0]
    assert entry == "1.20" and Decimal(average) == Decimal("0.95")


def test_outage_preserves_unparsed_source_and_resumes(rig):
    path, _, feed, _, _ = rig
    message(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.down = True
    assert cycle(rig).status == "PAUSED_DATA_UNAVAILABLE"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM signal_parser_decisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
    feed.down = False
    assert cycle(rig).entry_orders == 1


def test_cancel_deadline_survives_read_only_provider_failure(rig):
    path, clock, feed, _, _ = rig
    message(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.bid, feed.ask = Decimal("1.39"), Decimal("1.40")
    assert cycle(rig).entry_orders == 1
    feed.down = True
    clock.now += timedelta(seconds=3599)
    assert cycle(rig).cancellations == 0
    clock.now += timedelta(seconds=1)
    assert cycle(rig).cancellations == 1
    with sqlite3.connect(path) as db:
        status = db.execute(
            "SELECT state FROM order_intents WHERE side='BUY_TO_OPEN'"
        ).fetchone()[0]
    assert status in {"CANCELED", "CANCEL_PENDING"}


def test_missing_quote_never_fills_and_original_limit_remains(rig):
    path, clock, feed, _, _ = rig
    message(rig, "SPY 680C 11/13 @ 1.20", 101)
    feed.quoted = False
    assert cycle(rig).entry_orders == 0
    feed.quoted = True
    feed.bid, feed.ask = Decimal("1.45"), Decimal("1.50")
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=2)
    assert cycle(rig).simulated_fills == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0


@pytest.mark.parametrize("text", ["BUY 10 SHARES SPY", "BTC UP", "SPY MARKET COMMENTARY"])
def test_nonoption_ignored(rig, text):
    message(rig, text, 101)
    assert cycle(rig).entry_orders == 0


def test_add_and_duplicate_do_not_buy_again(rig):
    _, clock, _, _, _ = rig
    message(rig, "SPY 680C 11/13 @ 1.20", 101)
    assert cycle(rig).entry_orders == 1
    clock.now += timedelta(seconds=1)
    message(rig, "SPY ADDING MORE", 102, reply=101)
    assert cycle(rig).entry_orders == 0
    message(rig, "SPY 680C 11/13 @ 1.20", 101)
    assert cycle(rig).entry_orders == 0
