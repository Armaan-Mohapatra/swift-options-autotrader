"""Fail-closed bare SOLD lineage checks using synthetic SQLite state only.

There is no broker client, credential access or real order placement here.
The parser can request a close; the execution engine must still reconcile
actual broker ownership and fill quantity before any external action.
"""
from __future__ import annotations

import sqlite3

import pytest

from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.lineage import SignalMatcher
from tests.integration.test_signal_matching import CHANNEL, catalog, db, row, seed  # noqa: F401


def _filled(db, sid: str, *, suffix: str = "", sold: int = 0) -> None:
    """Record an explicitly synthetic, bot-owned filled buy and optional sell."""
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO order_intents "
            "(logical_order_id,signal_id,idempotency_key,side,"
            "requested_quantity,filled_quantity,limit_price,state) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"buy-{sid}{suffix}", sid, f"key-buy-{sid}{suffix}",
             "BUY_TO_OPEN", 1, 1, "1.20", "FILLED"),
        )
        if sold:
            con.execute(
                "INSERT INTO order_intents "
                "(logical_order_id,signal_id,idempotency_key,side,"
                "requested_quantity,filled_quantity,limit_price,state) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f"sell-{sid}{suffix}", sid, f"key-sell-{sid}{suffix}",
                 "SELL_TO_CLOSE", 1, sold, "2.50", "FILLED"),
            )


@pytest.mark.parametrize("message", [
    "SPY SOLD",
    "SPY SOLD @ $2.50 (-15%)",
    "$SPY SOLD @ 2.50",
    "QQQ SOLD @ 1.20 (+9.5%)",
])
def test_strict_bare_sold_is_an_ownership_gated_close(message):
    result = parse_message(message)
    assert result.intent is SignalIntent.CLOSE_100_PERCENT
    assert result.underlying in {"SPY", "QQQ"}
    assert result.reason == "BARE_SOLD_OWNERSHIP_REQUIRED"


@pytest.mark.parametrize("message", [
    "SOLD @ 2.50",
    "SWING SOLD @ 2.50",
    "OPTION - SWING SOLD @ 2.50",
    "SPY SOLD earlier today",
    "SPY SOLD HALF",
    "SPY SOLD and reentered",
])
def test_bare_sold_does_not_invent_ticker_or_match_commentary(message):
    result = parse_message(message)
    assert result.reason != "BARE_SOLD_OWNERSHIP_REQUIRED"
    if message != "SPY SOLD HALF":
        assert result.intent is not SignalIntent.CLOSE_100_PERCENT


def test_unique_bot_owned_fill_requests_close(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "SPY 681C 11/13 @ 1.10")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    first, second = matcher.process_pending()
    _filled(db, first.signal_id)
    seed(db, 12, "SPY SOLD @ $2.50 (-15%)")
    result = matcher.process_pending()[0]
    assert result.status == "RECORDED"
    assert result.signal_id == first.signal_id
    assert result.reason == "CLOSE_100_PERCENT_REQUEST_ONLY"
    assert row(db, "SELECT state FROM option_signals WHERE signal_id=?",
               (second.signal_id,))[0] == "SEEN"
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 1


def test_two_bot_owned_contracts_require_review(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "SPY 681C 11/13 @ 1.10")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    for decision in matcher.process_pending():
        _filled(db, decision.signal_id)
    seed(db, 12, "SPY SOLD @ 2.50")
    result = matcher.process_pending()[0]
    assert (result.status, result.reason) == ("REVIEW_REQUIRED", "AMBIGUOUS_MATCH")
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 0
    assert row(db, "SELECT reason FROM signal_review_queue")[0] == "AMBIGUOUS_MATCH"


def test_no_filled_bot_position_refuses_bare_sold(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()
    seed(db, 11, "SPY SOLD @ 2.50")
    result = matcher.process_pending()[0]
    assert (result.status, result.reason) == (
        "REVIEW_REQUIRED", "NO_MATCHING_BOT_OWNED_POSITION")
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 0


def test_closed_bot_position_cannot_be_sold_again(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    sid = matcher.process_pending()[0].signal_id
    _filled(db, sid, sold=1)
    seed(db, 11, "SPY SOLD")
    result = matcher.process_pending()[0]
    assert result.status == "REVIEW_REQUIRED"
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 0


def test_reply_resolves_one_of_two_bot_owned_signals(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "SPY 681C 11/13 @ 1.10")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    first, second = matcher.process_pending()
    _filled(db, first.signal_id)
    _filled(db, second.signal_id)
    seed(db, 12, "SPY SOLD", reply=11)
    result = matcher.process_pending()[0]
    assert result.status == "RECORDED"
    assert result.signal_id == second.signal_id
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 1


def test_repeated_bare_sold_does_not_make_a_second_close(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    sid = matcher.process_pending()[0].signal_id
    _filled(db, sid)
    seed(db, 11, "SPY SOLD")
    seed(db, 12, "SPY SOLD @ 2.50 (-15%)")
    first, second = matcher.process_pending()
    assert first.reason == "CLOSE_100_PERCENT_REQUEST_ONLY"
    assert second.reason == "DUPLICATE_CLOSE_REQUEST"
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 1
