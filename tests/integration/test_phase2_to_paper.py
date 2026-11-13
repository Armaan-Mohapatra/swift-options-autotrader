"""True parser -> durable signal -> paper order pipeline, entirely synthetic."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from swift_autotrader.broker.paper import PaperBroker, iso
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitQuote
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
CHANNEL = "-1001234567000"  # Synthetic example, never a paid channel ID.
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))


def deliver(path: Path, *, message_id: int, text: str, reply: int | None = None,
            eligible: bool = True) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
            "received_at_utc,sent_at_utc,body) VALUES(?,?,0,?,?,?)",
            (CHANNEL, str(message_id), iso(NOW), iso(NOW), text),
        )
        rid = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,"
            "revision,event_kind,origin,telegram_sent_at_utc,received_at_utc,"
            "reply_to_message_id,content_sha256,eligible_for_future_processing) "
            "VALUES(?,?,?,0,'new','live',?,?,?,?,?)",
            (rid, CHANNEL, message_id, iso(NOW), iso(NOW), reply,
             f"synthetic-{message_id}", int(eligible)),
        )
        rev = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO telegram_messages(channel_id,message_id,latest_revision_id,"
            "latest_effective_at_utc,first_seen_at_utc) VALUES(?,?,?,?,?)",
            (CHANNEL, message_id, rev, iso(NOW), iso(NOW)),
        )


def get(path: Path, sql: str):
    with sqlite3.connect(path) as db:
        return db.execute(sql).fetchone()


def test_lotto_add_average_and_half_exit_end_to_end(tmp_path):
    path = tmp_path / "e2e.sqlite3"
    apply_migrations(path)
    matcher = SignalMatcher(path, int(CHANNEL), catalog=StaticInstrumentCatalog([CONTRACT]))
    broker = PaperBroker(path, clock=lambda: NOW)
    engine = PaperOrderEngine(
        path, broker,
        preflight=PaperPreflight({CONTRACT: Preflight(
            True, True, True, Decimal("1000"), Decimal("0.01"), CONTRACT,
        )}),
        limits=RiskCeilings(contracts_per_signal=2, premium_per_signal_usd=Decimal("500"),
                            total_exposure_usd=Decimal("1000")),
        clock=lambda: NOW,
    )
    deliver(path, message_id=1, text="SPY LOTTO 680C 11/13 @ 1.20")
    results = matcher.process_pending()
    assert results[0].status == "RECORDED"
    assert engine.run_cycle().entry_submissions == 1
    sid = results[0].signal_id
    assert sid
    broker.manual_fill(sid + ":entry", fill_id="opened", quantity=1,
                       price=Decimal("1.15"), at=NOW)
    deliver(path, message_id=2, reply=1, text="SPY adding more, new avg .95")
    assert matcher.process_pending()[0].status == "RECORDED"
    assert engine.run_cycle().entry_submissions == 0
    row = get(path, "SELECT original_limit,swift_reported_avg,confirmed_avg_fill_price "
              "FROM option_signals")
    assert row == ("1.20", "0.95", "1.15")
    deliver(path, message_id=3, reply=1, text="SPY SOLD HALF")
    assert matcher.process_pending()[0].status == "RECORDED"
    assert engine.run_cycle(quotes={CONTRACT: ExitQuote(
        CONTRACT, Decimal("0.90"), Decimal("0.01"), NOW,
    )}).close_submissions == 1
    assert get(path, "SELECT requested_quantity FROM order_intents "
               "WHERE side='SELL_TO_CLOSE'")[0] == 1
    broker.manual_fill(sid + ":close", fill_id="closed", quantity=1,
                       price=Decimal("0.91"), at=NOW)
    engine.run_cycle()
    assert get(path, "SELECT state FROM option_signals")[0] == "CLOSED"
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 2


def test_non_option_never_reaches_paper_engine(tmp_path):
    path = tmp_path / "nonoption.sqlite3"
    apply_migrations(path)
    matcher = SignalMatcher(path, int(CHANNEL), catalog=StaticInstrumentCatalog([CONTRACT]))
    deliver(path, message_id=10, text="BUY 20 SHARES SPY")
    result = matcher.process_pending()[0]
    assert result.status == "IGNORED"
    engine = PaperOrderEngine(
        path, PaperBroker(path, clock=lambda: NOW), preflight=PaperPreflight(),
        clock=lambda: NOW,
    )
    assert engine.run_cycle().entry_submissions == 0
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 0
