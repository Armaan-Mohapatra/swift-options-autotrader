"""Synthetic-only Phase 3 end-to-end contract tests; no Telegram or live broker."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import BrokerOrderState
from swift_autotrader.broker.paper import PaperBroker, iso
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitQuote
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
OTHER = OptionContract("SPY", OptionRight.PUT, Decimal("675"), date(2026, 11, 13))


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def synthetic_signal(
    db_path: Path, *, mid: int = 1, contract: OptionContract = CONTRACT,
    category: str = "STANDARD", price: str = "1.20", sent: datetime = NOW,
    eligible: bool = True, reviewed: bool = False,
) -> str:
    channel = "-1001234567000"  # Invented ID, not a real private channel.
    sid = hashlib.sha256(f"swift:{channel}:{mid}".encode()).hexdigest()[:32]
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
            "received_at_utc,sent_at_utc,body) VALUES(?,?,0,?,?,?)",
            (channel, str(mid), iso(sent), iso(sent), "SYNTHETIC OPTIONS ALERT"),
        )
        raw_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,"
            "revision,event_kind,origin,telegram_sent_at_utc,received_at_utc,"
            "content_sha256,eligible_for_future_processing)"
            "VALUES(?,?,?,0,'new','live',?,?,?,?)",
            (raw_id, channel, mid, iso(sent), iso(sent), f"hash-{mid}", int(eligible)),
        )
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO option_signals(signal_id,source_event_id,underlying,option_right,"
            "strike,expiration_date,original_limit,created_at_utc,state) "
            "VALUES(?,?,?,?,?,?,?,?,'SEEN')",
            (sid, raw_id, contract.underlying, contract.right.value,
             str(contract.strike), contract.expiration.isoformat(), price, iso(sent)),
        )
        db.execute(
            "INSERT INTO signal_metadata(signal_id,channel_id,source_message_id,category,"
            "first_revision_id) VALUES(?,?,?,?,?)", (sid, channel, mid, category, rid),
        )
        db.execute(
            "INSERT INTO signal_parser_decisions(revision_id,channel_id,message_id,"
            "intent,status,reason,signal_id,processed_at_utc) "
            "VALUES(?, ?, ?, 'ENTRY', ?, ?, ?, ?)",
            (rid, channel, mid, "REVIEW_REQUIRED" if reviewed else "RECORDED",
             "INSTRUMENT_CATALOG_REQUIRED" if reviewed else
             "ENTRY_VALIDATED_NOT_EXECUTED", sid, iso(sent)),
        )
    return sid


def close_signal(db_path: Path, sid: str, *, mid: int = 999) -> None:
    channel = "-1001234567000"
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
            "received_at_utc,sent_at_utc,body) VALUES(?,?,0,?,?,?)",
            (channel, str(mid), iso(NOW), iso(NOW), "SYNTHETIC SOLD HALF"),
        )
        raw = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,"
            "revision,event_kind,origin,telegram_sent_at_utc,received_at_utc,"
            "content_sha256,eligible_for_future_processing) "
            "VALUES(?,?,?,0,'new','live',?,?,?,1)",
            (raw, channel, mid, iso(NOW), iso(NOW), f"closehash-{mid}"),
        )
        rev = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO signal_close_requests(signal_id,first_revision_id,requested_at_utc) "
            "VALUES(?,?,?)", (sid, rev, iso(NOW)),
        )
        db.execute("UPDATE option_signals SET state='CLOSE_REQUESTED' WHERE signal_id=?", (sid,))


@pytest.fixture()
def rig(tmp_path: Path):
    db_path = tmp_path / "phase3.sqlite3"
    assert len(apply_migrations(db_path)) == 5
    clock = Clock()
    broker = PaperBroker(db_path, clock=clock)
    limits = RiskCeilings(
        contracts_per_signal=3,
        premium_per_signal_usd=Decimal("1000"),
        total_exposure_usd=Decimal("3000"),
        open_signal_count=5,
    )
    checks = {
        c: Preflight(
            True, True, True, Decimal("10000"), Decimal("0.01"), c,
        ) for c in (CONTRACT, OTHER)
    }
    engine = PaperOrderEngine(
        db_path, broker, preflight=PaperPreflight(checks), limits=limits, clock=clock,
    )
    return db_path, clock, broker, engine


def get(db_path: Path, query: str, args: tuple = ()):
    with connect(db_path) as db:
        return db.execute(query, args).fetchone()


def entry_id(sid: str) -> str:
    return sid + ":entry"


def close_id(sid: str) -> str:
    return sid + ":close"


def quote(contract: OptionContract = CONTRACT, *, at: datetime = NOW,
          price: str = "0.85") -> dict[OptionContract, ExitQuote]:
    return {contract: ExitQuote(contract, Decimal(price), Decimal("0.01"), at)}


def test_exact_original_limit_lotto_and_no_chase(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path, category="LOTTO", price="1.20")
    assert engine.run_cycle().entry_submissions == 1
    order = get(path, "SELECT * FROM order_intents WHERE signal_id=?", (sid,))
    assert order["limit_price"] == "1.20"
    assert order["requested_quantity"] == 3
    assert order["state"] == "ACCEPTED"
    assert broker.get_order(entry_id(sid)).state == BrokerOrderState.ACCEPTED
    assert engine.run_cycle().entry_submissions == 0
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 1


def test_accepted_clock_drives_deadline_not_source_time(rig):
    path, clock, _, engine = rig
    sid = synthetic_signal(path, sent=NOW - timedelta(seconds=60))
    engine.run_cycle()
    row = get(path, "SELECT accepted_at_utc,expires_at_utc FROM order_intents "
              "WHERE logical_order_id=?", (entry_id(sid),))
    assert row["accepted_at_utc"] == iso(NOW)
    assert row["expires_at_utc"] == iso(NOW + timedelta(seconds=3600))
    clock.advance(3599)
    assert engine.run_cycle().cancellation_requests == 0
    clock.advance(1)
    assert engine.run_cycle().cancellation_requests == 1
    assert get(path, "SELECT state FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "CANCELED"
    clock.advance(86400)
    assert engine.run_cycle().entry_submissions == 0


def test_partial_fill_expiry_preserves_two_of_three(rig):
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=2,
                       price=Decimal("1.10"), at=NOW)
    engine.run_cycle()
    clock.advance(3600)
    engine.run_cycle()
    row = get(path, "SELECT state,filled_quantity FROM order_intents WHERE logical_order_id=?",
              (entry_id(sid),))
    assert (row["state"], row["filled_quantity"]) == ("CANCELED", 2)
    assert get(path, "SELECT confirmed_avg_fill_price FROM option_signals "
               "WHERE signal_id=?", (sid,))[0] == "1.10"


def test_cancel_fill_race_until_ack(rig):
    path, clock, _, engine = rig
    broker = PaperBroker(path, clock=clock, cancel_pending=True)
    engine = PaperOrderEngine(
        path, broker, preflight=engine.preflight, limits=engine.limits, clock=clock,
    )
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.17"))
    clock.advance(3600)
    engine.run_cycle()
    assert broker.get_order(entry_id(sid)).state == BrokerOrderState.CANCEL_PENDING
    broker.manual_fill(entry_id(sid), fill_id="f2", quantity=1, price=Decimal("1.19"))
    broker.confirm_cancel(entry_id(sid))
    engine.run_cycle()
    assert get(path, "SELECT filled_quantity FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == 2
    assert get(path, "SELECT COUNT(*) FROM order_fills")[0] == 2


def test_exit_waits_for_cancel_confirmation_and_closes_all(rig):
    path, clock, _, engine = rig
    broker = PaperBroker(path, clock=clock, cancel_pending=True)
    engine = PaperOrderEngine(path, broker, preflight=engine.preflight,
                              limits=engine.limits, clock=clock)
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.10"))
    close_signal(path, sid)
    assert engine.run_cycle(quotes=quote()).close_submissions == 0
    assert broker.get_order(entry_id(sid)).state is BrokerOrderState.CANCEL_PENDING
    broker.manual_fill(entry_id(sid), fill_id="f2", quantity=1, price=Decimal("1.15"))
    broker.confirm_cancel(entry_id(sid))
    assert engine.run_cycle(quotes=quote()).close_submissions == 1
    stc = broker.get_order(close_id(sid))
    assert stc.state == BrokerOrderState.ACCEPTED
    assert get(path, "SELECT requested_quantity,limit_price FROM order_intents "
               "WHERE logical_order_id=?", (close_id(sid),))[0] == 2
    broker.manual_fill(close_id(sid), fill_id="s1", quantity=2, price=Decimal("0.90"))
    engine.run_cycle()
    assert get(path, "SELECT state FROM option_signals WHERE signal_id=?", (sid,))[0] == "CLOSED"
    assert engine.run_cycle(quotes=quote()).close_submissions == 0
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 2


def test_full_fill_exit_close_100_percent_not_half(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="full", quantity=3, price=Decimal("1.05"))
    engine.run_cycle()
    close_signal(path, sid)
    engine.run_cycle(quotes=quote())
    assert get(path, "SELECT requested_quantity FROM order_intents "
               "WHERE logical_order_id=?", (close_id(sid),))[0] == 3


def test_close_before_entry_prevents_open(rig):
    path, _, _, engine = rig
    sid = synthetic_signal(path)
    close_signal(path, sid)
    engine.run_cycle()
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 0
    assert get(path, "SELECT state FROM option_signals WHERE signal_id=?", (sid,))[0] == "CLOSED"


def test_swift_avg_never_overwrites_fill_basis_or_limit(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.00"))
    broker.manual_fill(entry_id(sid), fill_id="f2", quantity=1, price=Decimal("1.10"))
    engine.run_cycle()
    with connect(path) as db:
        db.execute("UPDATE option_signals SET swift_reported_avg='0.65' WHERE signal_id=?",
                   (sid,))
    engine.run_cycle()
    row = get(path, "SELECT original_limit,swift_reported_avg,confirmed_avg_fill_price "
              "FROM option_signals WHERE signal_id=?", (sid,))
    assert tuple(row) == ("1.20", "0.65", "1.05")


def test_zero_buy_if_catalog_not_verified(rig):
    path, _, _, engine = rig
    synthetic_signal(path, reviewed=True)
    engine.run_cycle()
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 0


@pytest.mark.parametrize("override,reason", [
    ({"instrument_verified": False}, "INSTRUMENT_NOT_VERIFIED"),
    ({"options_permission": False}, "OPTIONS_PERMISSION_REQUIRED"),
    ({"market_session_open": False}, "MARKET_SESSION_CLOSED"),
    ({"available_buying_power_usd": Decimal("1")}, "INSUFFICIENT_BUYING_POWER"),
    ({"premium_tick": Decimal("0")}, "TICK_RULE_UNVERIFIED"),
])
def test_preflight_fail_closed(rig, override, reason):
    path, _, _, engine = rig
    sid = synthetic_signal(path)
    check = replace(engine.preflight.check(CONTRACT), **override)
    engine.preflight = PaperPreflight({CONTRACT: check})
    assert engine.run_cycle().entry_submissions == 0
    assert get(path, "SELECT reason FROM paper_engine_decisions WHERE signal_id=?",
               (sid,))[0] == reason
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 0


def test_invalid_tick_never_rounded(rig):
    path, _, _, engine = rig
    sid = synthetic_signal(path, price="1.23")
    engine.preflight = PaperPreflight({CONTRACT: replace(
        engine.preflight.check(CONTRACT), premium_tick=Decimal("0.05"))})
    engine.run_cycle()
    assert get(path, "SELECT reason FROM paper_engine_decisions WHERE signal_id=?",
               (sid,))[0] == "INVALID_PREMIUM_TICK"


def test_signal_cap_and_total_exposure(rig):
    path, _, _, engine = rig
    first = synthetic_signal(path, mid=1)
    second = synthetic_signal(path, mid=2, contract=OTHER)
    engine.limits = replace(engine.limits, open_signal_count=1)
    engine.run_cycle()
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 1
    assert get(path, "SELECT reason FROM paper_engine_decisions WHERE signal_id=?",
               (second,))[0] == "OPEN_SIGNAL_CAP_EXCEEDED"
    assert get(path, "SELECT outcome FROM paper_engine_decisions WHERE signal_id=?",
               (first,))[0] == "INTENT_RECORDED"


def test_duplicate_fill_id_no_double_count(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.20"), at=NOW)
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.20"), at=NOW)
    engine.run_cycle()
    assert get(path, "SELECT COUNT(*) FROM order_fills")[0] == 1
    assert get(path, "SELECT filled_quantity FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == 1
    with pytest.raises(ValueError, match="conflicting"):
        broker.manual_fill(entry_id(sid), fill_id="f1", quantity=2,
                           price=Decimal("1.20"), at=NOW)


def test_restart_does_not_duplicate_orders_or_fills(rig):
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.18"))
    engine.run_cycle()
    restarted = PaperOrderEngine(
        path, PaperBroker(path, clock=clock), preflight=engine.preflight,
        limits=engine.limits, clock=clock,
    )
    restarted.run_cycle()
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 1
    assert get(path, "SELECT COUNT(*) FROM order_fills")[0] == 1


def test_crash_after_persisted_intent_never_retries_unknown(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    with connect(path) as db:
        signal = db.execute("SELECT * FROM option_signals WHERE signal_id=?", (sid,)).fetchone()
    engine._intent(signal, OrderSide.BUY_TO_OPEN, 3, Decimal("1.20"), NOW)
    engine.run_cycle()
    assert broker.get_order(entry_id(sid)).state == BrokerOrderState.UNKNOWN
    assert get(path, "SELECT code FROM paper_engine_alerts WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "UNKNOWN_BROKER_ORDER"
    assert get(path, "SELECT COUNT(*) FROM paper_broker_orders")[0] == 0


def test_broker_duplicate_submission_same_key_returns_same_order(rig):
    path, _, broker, engine = rig
    synthetic_signal(path)
    contract = CONTRACT
    sid = "dummy"
    intent = OrderIntent(sid + ":entry", "stable-key", contract,
                         OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"))
    one = broker.submit_order(intent)
    two = broker.submit_order(intent)
    assert one == two
    assert get(path, "SELECT COUNT(*) FROM paper_broker_orders")[0] == 1
    with pytest.raises(ValueError, match="idempotency"):
        broker.submit_order(replace(intent, limit_price=Decimal("1.30")))


def test_rejected_paper_order_stays_rejected(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.force_reject(entry_id(sid))
    engine.run_cycle()
    assert get(path, "SELECT state FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "REJECTED"
    assert engine.run_cycle().entry_submissions == 0


def test_stale_and_recovered_entries_do_not_execute(rig):
    path, clock, _, engine = rig
    sid = synthetic_signal(path, sent=clock.now - timedelta(seconds=180))
    synthetic_signal(path, mid=2, contract=OTHER, eligible=False)
    engine.run_cycle()
    assert get(path, "SELECT reason FROM paper_engine_decisions WHERE signal_id=?",
               (sid,))[0] == "STALE_ENTRY_NO_REPLAY"
    assert get(path, "SELECT COUNT(*) FROM order_intents")[0] == 0


def test_same_ticker_different_contract_no_wrong_close(rig):
    path, _, broker, engine = rig
    one = synthetic_signal(path, mid=1)
    two = synthetic_signal(path, mid=2, contract=OTHER)
    engine.run_cycle()
    broker.manual_fill(entry_id(one), fill_id="a", quantity=1, price=Decimal("1.18"))
    broker.manual_fill(entry_id(two), fill_id="b", quantity=1, price=Decimal("1.10"))
    close_signal(path, one)
    engine.run_cycle(quotes=quote(OTHER))
    assert get(path, "SELECT COUNT(*) FROM order_intents "
               "WHERE signal_id=? AND side='SELL_TO_CLOSE'", (two,))[0] == 0
    assert get(path, "SELECT COUNT(*) FROM order_intents "
               "WHERE signal_id=? AND side='SELL_TO_CLOSE'", (one,))[0] == 0
    assert "EXIT_QUOTE_MISSING" in engine.health()["open_alerts"]
    engine.run_cycle(quotes=quote())
    assert get(path, "SELECT requested_quantity FROM order_intents WHERE logical_order_id=?",
               (close_id(one),))[0] == 1


def test_exit_without_quote_does_not_false_close(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=3, price=Decimal("1.10"))
    close_signal(path, sid)
    engine.run_cycle()
    assert get(path, "SELECT state FROM option_signals "
               "WHERE signal_id=?", (sid,))[0] == "ALERT_REQUIRED"
    assert get(path, "SELECT COUNT(*) FROM order_intents WHERE side='SELL_TO_CLOSE'")[0] == 0


def test_stale_exit_quote_never_submitted(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=3, price=Decimal("1.10"))
    close_signal(path, sid)
    engine.run_cycle(quotes=quote(at=NOW - timedelta(seconds=31)))
    assert get(path, "SELECT COUNT(*) FROM order_intents WHERE side='SELL_TO_CLOSE'")[0] == 0
    assert "EXIT_QUOTE_STALE" in engine.health()["open_alerts"]


def test_partial_exit_does_not_claim_closed(rig):
    path, _, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=3, price=Decimal("1.08"))
    close_signal(path, sid)
    engine.run_cycle(quotes=quote())
    broker.manual_fill(close_id(sid), fill_id="sell1", quantity=1, price=Decimal("0.85"))
    engine.run_cycle()
    assert get(path, "SELECT state FROM option_signals WHERE signal_id=?", (sid,))[0] != "CLOSED"
    broker.cancel_order(close_id(sid))
    engine.run_cycle()
    assert get(path, "SELECT state FROM option_signals "
               "WHERE signal_id=?", (sid,))[0] == "ALERT_REQUIRED"
    assert "UNFILLED_EXIT" in engine.health()["open_alerts"]
    assert get(path, "SELECT COUNT(*) FROM order_intents WHERE side='SELL_TO_CLOSE'")[0] == 1


def test_pause_and_emergency_stop_preserve_position(rig):
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.set_paused(True)
    assert engine.run_cycle().entry_submissions == 0
    engine.set_paused(False)
    engine.run_cycle()
    broker.manual_fill(entry_id(sid), fill_id="f1", quantity=1, price=Decimal("1.10"))
    engine.emergency_stop()
    assert engine.health()["emergency_stop"] is True
    assert get(path, "SELECT state,filled_quantity FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "CANCELED"
    assert engine.run_cycle().entry_submissions == 0
    with pytest.raises(PermissionError):
        engine.reset_emergency_stop()
    engine.reset_emergency_stop(approved=True)
    assert engine.health()["paused"] is True


def test_reconciliation_uses_only_broker_confirmed_fills(rig):
    path, _, _, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    assert get(path, "SELECT confirmed_avg_fill_price FROM option_signals WHERE signal_id=?",
               (sid,))[0] is None
    assert get(path, "SELECT filled_quantity FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == 0


def test_broker_order_mismatch_rejected(rig):
    path, _, broker, engine = rig
    synthetic_signal(path)
    engine.run_cycle()
    with pytest.raises(ValueError, match="limit"):
        broker.manual_fill(broker.get_order(
            get(path, "SELECT logical_order_id FROM order_intents")[0]
        ).logical_order_id, fill_id="f1", quantity=1, price=Decimal("1.21"))


def test_synthetic_broker_not_network_adapter(rig):
    path, _, _, engine = rig
    class FakeBroker:
        db_path = path
    with pytest.raises(TypeError, match="ONLY"):
        PaperOrderEngine(path, FakeBroker(), preflight=engine.preflight)


def test_initial_risk_ceiling_blocks_by_default(tmp_path):
    path = tmp_path / "defaults.sqlite3"
    apply_migrations(path)
    broker = PaperBroker(path, clock=lambda: NOW)
    sid = synthetic_signal(path)
    engine = PaperOrderEngine(
        path, broker, preflight=PaperPreflight({CONTRACT: Preflight(
            True, True, True, Decimal("1000"), Decimal("0.01"), CONTRACT,
        )}), clock=lambda: NOW,
    )
    engine.run_cycle()
    assert get(path, "SELECT reason FROM paper_engine_decisions WHERE signal_id=?",
               (sid,))[0] == "SIGNAL_PREMIUM_CAP_EXCEEDED"


def test_minute_clock_is_timezone_aware(rig):
    path, _, _, engine = rig
    with pytest.raises(ValueError, match="timezone"):
        engine.clock = lambda: datetime(2026, 11, 12)
        engine.run_cycle()


def test_explicit_bid_ask_crossing_and_no_price_chase(rig):
    from swift_autotrader.broker.paper import PaperMarketQuote
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    # Price far above Swift's 1.20: order remains at 1.20, does not chase.
    high = PaperMarketQuote("qhigh", CONTRACT, Decimal("2.00"), Decimal("2.10"),
                            10, 10, clock.now)
    assert broker.observe_quote(high) == 0
    assert broker.get_order(entry_id(sid)).filled_quantity == 0
    # Once ask is at or below resting limit, only actual displayed depth fills.
    good = PaperMarketQuote("qgood", CONTRACT, Decimal("1.00"), Decimal("1.10"),
                            1, 2, clock.now)
    assert broker.observe_quote(good) == 2
    assert broker.observe_quote(good) == 0  # delivery idempotency
    engine.run_cycle()
    assert broker.get_order(entry_id(sid)).filled_quantity == 2
    assert get(path, "SELECT confirmed_avg_fill_price FROM option_signals WHERE signal_id=?",
               (sid,))[0] == "1.10"
    with pytest.raises(ValueError, match="quote ID"):
        broker.observe_quote(replace(good, ask=Decimal("1.05")))


def test_quote_expiration_and_bad_spread_rejected(rig):
    from swift_autotrader.broker.paper import PaperMarketQuote
    path, clock, broker, engine = rig
    synthetic_signal(path)
    engine.run_cycle()
    with pytest.raises(ValueError, match="stale"):
        broker.observe_quote(PaperMarketQuote(
            "stale", CONTRACT, Decimal("1.00"), Decimal("1.10"),
            1, 1, clock.now - timedelta(seconds=16),
        ))
    with pytest.raises(ValueError, match="invalid"):
        broker.observe_quote(PaperMarketQuote(
            "crossed", CONTRACT, Decimal("1.11"), Decimal("1.10"),
            1, 1, clock.now,
        ))


def test_quote_is_contract_specific(rig):
    from swift_autotrader.broker.paper import PaperMarketQuote
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    assert broker.observe_quote(PaperMarketQuote(
        "wrong", OTHER, Decimal("1.0"), Decimal("1.05"), 100, 100, clock.now,
    )) == 0
    assert broker.get_order(entry_id(sid)).filled_quantity == 0


def test_early_market_close_cancels_without_reopening(rig):
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    assert broker.close_session() == 1
    engine.run_cycle()
    assert broker.get_order(entry_id(sid)).state == BrokerOrderState.CANCELED
    clock.advance(86400)
    assert engine.run_cycle().entry_submissions == 0


def test_broker_confirmation_lost_after_submission_recovers_from_journal(rig):
    from unittest.mock import patch
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    original = broker.submit_order

    def submit_then_disconnect(intent):
        original(intent)
        raise ConnectionError("synthetic interruption after broker acceptance")

    with (
        patch.object(broker, "submit_order", side_effect=submit_then_disconnect),
        pytest.raises(ConnectionError),
    ):
        engine.run_cycle()
    assert get(path, "SELECT state FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "INTENT_RECORDED"
    new_engine = PaperOrderEngine(
        path, PaperBroker(path, clock=clock), preflight=engine.preflight,
        limits=engine.limits, clock=clock,
    )
    assert new_engine.run_cycle().entry_submissions == 0
    assert broker.get_order(entry_id(sid)).state == BrokerOrderState.ACCEPTED
    assert get(path, "SELECT state FROM order_intents WHERE logical_order_id=?",
               (entry_id(sid),))[0] == "ACCEPTED"
    assert get(path, "SELECT COUNT(*) FROM paper_broker_orders")[0] == 1


def test_quote_does_not_fill_until_broker_accepts(rig):
    from swift_autotrader.broker.paper import PaperMarketQuote
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    with pytest.raises(ValueError, match="stale"):
        broker.observe_quote(PaperMarketQuote(
            "pre", CONTRACT, Decimal("1.0"), Decimal("1.1"), 1, 1,
            clock.now - timedelta(seconds=5),
        ), max_age_seconds=1)
    assert broker.get_order(entry_id(sid)).filled_quantity == 0


def test_single_host_lock_prevents_overlapping_cycles(rig):
    from swift_autotrader.orders.engine import _cycle_lock
    path, _, _, engine = rig
    with _cycle_lock(path), pytest.raises(RuntimeError, match="already running"):
        engine.run_cycle()
    for _ in range(3):
        engine.run_cycle()
    assert path.with_suffix(path.suffix + ".cycle.lock").stat().st_size == 1


def test_terminal_order_status_regression_requires_review(rig):
    from unittest.mock import patch
    path, clock, broker, engine = rig
    sid = synthetic_signal(path)
    engine.run_cycle()
    broker.cancel_order(entry_id(sid))
    engine.run_cycle()
    impossible = replace(broker.get_order(entry_id(sid)), state=BrokerOrderState.ACCEPTED)
    with (
        patch.object(broker, "get_order", return_value=impossible),
        pytest.raises(Exception, match="regression"),
    ):
        engine.run_cycle()
    assert "RECONCILIATION_REQUIRED" in engine.health()["open_alerts"]
