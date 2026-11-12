"""Real SQLite integration; only synthetic Telegram event records."""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog

CHANNEL = -1001234000001  # fabricated test channel (never a paid/private channel ID)
SENT = "2026-11-12T15:30:00.000000Z"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "safe.sqlite3"
    apply_migrations(path)
    return path


@pytest.fixture
def catalog():
    return StaticInstrumentCatalog([
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13)),
        OptionContract("SPY", OptionRight.PUT, Decimal("680"), date(2026, 11, 13)),
        OptionContract("SPY", OptionRight.CALL, Decimal("681"), date(2026, 11, 13)),
        OptionContract("QQQ", OptionRight.CALL, Decimal("500"), date(2026, 11, 13)),
    ])


def seed(
    db, mid, body, *, reply=None, kind="new", eligible=True,
    sent=SENT, edit_at=None, source="live", channel=CHANNEL,
):
    with sqlite3.connect(db) as con:
        rev = con.execute(
            "SELECT COALESCE(MAX(revision),-1)+1 FROM telegram_message_revisions "
            "WHERE channel_id=? AND message_id=?", (str(channel), mid),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
            "received_at_utc,sent_at_utc,body) VALUES(?,?,?,?,?,?)",
            (str(channel), str(mid), rev, sent, sent, body),
        )
        rawid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,"
            "revision,event_kind,origin,telegram_sent_at_utc,telegram_edited_at_utc,"
            "received_at_utc,reply_to_message_id,content_sha256,eligible_for_future_processing) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rawid, str(channel), mid, rev, kind, source, sent, edit_at, sent,
                reply, f"fixture-{channel}-{mid}-{rev}", int(eligible),
            ),
        )
        rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO telegram_messages(channel_id,message_id,latest_revision_id,"
            "latest_effective_at_utc,first_seen_at_utc) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id,message_id) DO UPDATE SET "
            "latest_revision_id=excluded.latest_revision_id,"
            "latest_effective_at_utc=excluded.latest_effective_at_utc",
            (str(channel), mid, rid, edit_at or sent, sent),
        )
    return rid


def row(db, sql, args=()):
    with sqlite3.connect(db) as con:
        return con.execute(sql, args).fetchone()


def test_entry_and_lotto_distinct_original_signal(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY LOTTO 681C 11/13 @ .80")
    decisions = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert [d.status for d in decisions] == ["RECORDED", "RECORDED"]
    assert decisions[0].signal_id != decisions[1].signal_id
    assert row(db, "SELECT category FROM signal_metadata WHERE source_message_id=2")[0] == "LOTTO"
    assert row(db, "SELECT original_limit FROM option_signals WHERE signal_id=?", (
        decisions[0].signal_id,
    ))[0] == "1.20"


def test_add_never_creates_new_entry_or_overwrites_limit(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY adding more, new avg .95", reply=1)
    outputs = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert outputs[1].reason == "SWIFT_AVERAGE_METADATA_ONLY"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 1
    assert row(db, "SELECT original_limit,swift_reported_avg,confirmed_avg_fill_price "
        "FROM option_signals")[0:3] == ("1.20", "0.95", None)
    assert row(db, "SELECT COUNT(*) FROM order_intents")[0] == 0


@pytest.mark.parametrize("phrase", [
    "SOLD HALF", "SOLD 1/3", "TRIM", "SCALED OUT", "RUNNERS OUT", "ALL OUT",
    "SOLD ALL", "SOLD 75%", "FULL EXIT",
])
def test_all_exit_variants_request_full_close_not_completed(db, catalog, phrase):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, f"SPY {phrase}", reply=1)
    results = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert results[1].intent == "CLOSE_100_PERCENT"
    assert results[1].reason == "CLOSE_100_PERCENT_REQUEST_ONLY"
    assert row(db, "SELECT state FROM option_signals")[0] == "CLOSE_REQUESTED"
    assert row(db, "SELECT COUNT(*) FROM order_intents")[0] == 0
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 1


def test_second_close_is_idempotent(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY SOLD HALF", reply=1)
    seed(db, 3, "SPY ALL OUT", reply=2)
    decisions = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert decisions[2].reason == "DUPLICATE_CLOSE_REQUEST"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 1


def test_same_symbol_different_strike_no_unqualified_exit(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY 681C 11/13 @ 1.00")
    seed(db, 3, "SPY SOLD HALF")
    result = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()[-1]
    assert result.reason == "AMBIGUOUS_MATCH"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 0
    assert row(db, "SELECT COUNT(*) FROM signal_review_queue")[0] == 1


def test_explicit_contract_and_reply_resolves_unambiguously(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY 681C 11/13 @ 1.00")
    seed(db, 3, "SPY 681C 11/13 SOLD HALF")
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[2].signal_id == r[1].signal_id
    assert row(db, "SELECT state FROM option_signals WHERE signal_id=?", (
        r[0].signal_id,
    ))[0] == "SEEN"


def test_same_contract_two_entry_origins_need_reply(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY 680C 11/13 @ 1.10")
    seed(db, 3, "SPY 680C 11/13 SOLD HALF")
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[2].reason == "AMBIGUOUS_MATCH"


def test_contradictory_reply_contract_requires_review(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY 681C 11/13 @ 1.00")
    seed(db, 3, "SPY 681C 11/13 SOLD HALF", reply=1)
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[2].reason == "REPLY_CONTRACT_MISMATCH"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 0


def test_reply_chain_to_average_then_close(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY NEW AVG .95", reply=1)
    seed(db, 3, "ALL OUT", reply=2)
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[2].signal_id == r[0].signal_id
    assert r[2].status == "RECORDED"


def test_unknown_reply_is_reviewed_not_matched_by_ticker(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY SOLD HALF", reply=9999)
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[1].reason == "UNKNOWN_REPLY_LINEAGE"


def test_old_entry_not_promoted(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20", eligible=False, source="recovered")
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()[0]
    assert r.status == "SKIPPED"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 0


def test_old_exit_not_promoted(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY SOLD HALF", eligible=False)
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[1].status == "SKIPPED"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 0


def test_duplicate_restart_does_not_repeat_decision(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    assert len(matcher.process_pending()) == 1
    assert matcher.process_pending() == []
    assert SignalMatcher(db, CHANNEL, catalog=catalog).process_pending() == []
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 1


def test_entry_edit_does_not_create_second_signal(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 1, "SPY 681C 11/13 @ 1.20", kind="edit", eligible=False,
         edit_at="2026-11-12T15:40:00.000000Z")
    decision = matcher.process_pending()[0]
    assert decision.reason == "ENTRY_EDIT_REVIEW"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 1
    assert row(db, "SELECT strike FROM option_signals")[0] == "680"


def test_identical_entry_edit_is_noop(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 1, "SPY 680C 11/13 @ 1.20", kind="edit", eligible=False,
         edit_at="2026-11-12T15:40:00.000000Z")
    assert matcher.process_pending()[0].reason == "UNCHANGED_ENTRY_EDIT"


def test_edit_without_seen_original_review(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20", kind="edit", eligible=False,
         edit_at="2026-11-12T15:40:00.000000Z")
    assert SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()[0].reason == (
        "EDIT_WITHOUT_LINKED_ORIGINAL"
    )


def test_avg_edit_only_changes_swift_avg(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY NEW AVG .95", reply=1)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 2, "SPY NEW AVG .85", reply=1, kind="edit", eligible=False,
         edit_at="2026-11-12T15:40:00.000000Z")
    assert matcher.process_pending()[0].reason == "SWIFT_AVERAGE_METADATA_ONLY"
    assert row(db, "SELECT swift_reported_avg,original_limit,confirmed_avg_fill_price "
        "FROM option_signals") == ("0.85", "1.20", None)


def test_exit_edit_from_add_is_rejected(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY ADD", reply=1)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 2, "SPY ALL OUT", reply=1, kind="edit", eligible=False,
         edit_at="2026-11-12T15:45:00.000000Z")
    assert matcher.process_pending()[0].reason == "EDIT_CHANGED_INTENT"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 0


def test_out_of_order_original_and_edit_no_backfilled_entry(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 1, "SPY 681C 11/13 @ 1.20", kind="edit", eligible=False,
         edit_at="2026-11-12T15:40:00.000000Z")
    decisions = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert decisions[0].reason == "SUPERSEDED_REVISION"
    assert decisions[1].reason == "EDIT_WITHOUT_LINKED_ORIGINAL"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 0


def test_stale_average_cannot_overwrite_newer_average(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY NEW AVG .80", reply=1, sent="2026-11-12T15:45:00.000000Z")
    seed(db, 3, "SPY NEW AVG 1.20", reply=1, sent="2026-11-12T15:35:00.000000Z")
    r = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()
    assert r[-1].reason == "STALE_AVERAGE"
    assert row(db, "SELECT swift_reported_avg FROM option_signals")[0] == "0.80"


def test_no_catalog_prevents_entry(db):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    r = SignalMatcher(db, CHANNEL).process_pending()
    assert r[0].reason == "INSTRUMENT_CATALOG_REQUIRED"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 0


def test_wrong_channel_is_never_processed(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20", channel=-1009999999999)
    assert SignalMatcher(db, CHANNEL, catalog=catalog).process_pending() == []


def test_review_queue_reasons_are_durable(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY 681C 11/13 @ 1.00")
    seed(db, 3, "SPY SOLD HALF")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    assert row(db, "SELECT reason,status FROM signal_review_queue") == (
        "AMBIGUOUS_MATCH", "OPEN"
    )
    assert matcher.statistics()["REVIEW_REQUIRED"] == 1
    assert matcher.statistics()["PENDING"] == 0


def test_batch_limit_restart_drains_without_dupes(db, catalog):
    for mid in range(1, 21):
        seed(db, mid, "BUY 20 SHARES SPY")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    assert len(matcher.process_pending(limit=3)) == 3
    assert len(SignalMatcher(db, CHANNEL, catalog=catalog).process_pending(limit=17)) == 17
    assert matcher.statistics()["PENDING"] == 0
    assert row(db, "SELECT COUNT(*) FROM signal_parser_decisions")[0] == 20


def test_invalid_limits_do_not_access_db(db, catalog):
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    with pytest.raises(ValueError):
        matcher.process_pending(limit=0)


def test_reported_average_never_overwrites_nonempty_fill_basis(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    signal = matcher.process_pending()[0].signal_id
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE option_signals SET confirmed_avg_fill_price='1.17' WHERE signal_id=?",
            (signal,),
        )
    seed(db, 2, "SPY NEW AVG .95", reply=1)
    matcher.process_pending()
    assert row(
        db,
        "SELECT swift_reported_avg,confirmed_avg_fill_price,original_limit "
        "FROM option_signals WHERE signal_id=?", (signal,),
    ) == ("0.95", "1.17", "1.20")


def test_edited_closed_alert_does_not_duplicate_exit(db, catalog):
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    seed(db, 2, "SPY SOLD HALF", reply=1)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 2, "SPY SOLD ALL", reply=1, kind="edit", eligible=False,
         edit_at="2026-11-12T15:45:00.000000Z")
    result = matcher.process_pending()[0]
    assert result.reason == "EXIT_EDIT_NO_NEW_ACTION"
    assert row(db, "SELECT COUNT(*) FROM signal_close_requests")[0] == 1


def test_missing_date_keeps_entry_out_of_signal_table(db, catalog):
    seed(db, 1, "BUY SPY 680C @ 1.20")
    result = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()[0]
    assert result.status == "REVIEW_REQUIRED"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 0


def test_new_option_alert_does_not_invent_second_contract_year(db):
    contract26 = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
    contract27 = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2027, 11, 13))
    seed(db, 1, "SPY 680C 11/13 @ 1.20")
    catalog = StaticInstrumentCatalog([contract26, contract27])
    result = SignalMatcher(db, CHANNEL, catalog=catalog).process_pending()[0]
    assert result.reason == "AMBIGUOUS_EXPIRY_YEAR"
    assert row(db, "SELECT COUNT(*) FROM option_signals")[0] == 0
