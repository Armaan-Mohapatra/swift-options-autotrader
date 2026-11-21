"""Every owner-listed exit phrase must close its reply-linked signal.

Regression: a bare reply such as "SCALED OUT" was read as ticker "SCALED",
failed the reply-contract check and left the position open. Synthetic data only.
"""
from __future__ import annotations

import pytest

from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.lineage import SignalMatcher
from tests.integration.test_signal_matching import CHANNEL, catalog, db, row, seed  # noqa: F401

EXIT_PHRASES = [
    "SOLD HALF", "SOLD 1/3", "TRIM", "TRIMMED", "SCALED OUT", "RUNNERS OUT",
    "ALL OUT", "CLOSED ALL", "FULL EXIT", "OUT OF THIS", "SOLD SOME",
    "Nice, ALL OUT 🔥", "GREAT ALL OUT", "I SOLD HALF here", "Finally all out!",
    "scaled out 💰💰", "Took profits, trimmed.",
]


@pytest.mark.parametrize("phrase", EXIT_PHRASES)
def test_bare_exit_phrase_names_no_ticker(phrase):
    alert = parse_message(phrase)
    assert alert.intent is SignalIntent.CLOSE_100_PERCENT
    assert alert.underlying is None


@pytest.mark.parametrize("phrase", EXIT_PHRASES)
def test_reply_linked_exit_phrase_requests_full_close(db, catalog, phrase):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "QQQ 500C 11/13 @ 2.00")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending(limit=10)
    seed(db, 12, phrase, reply=10)
    decisions = matcher.process_pending(limit=10)
    assert [d.status for d in decisions] == ["RECORDED"], decisions
    closes = row(db, "SELECT count(*) FROM signal_close_requests")[0]
    assert closes == 1
    states = dict(
        (r[0], r[1]) for r in __import__("sqlite3").connect(db).execute(
            "SELECT underlying, state FROM option_signals")
    )
    assert states["QQQ"] != "CLOSE_REQUESTED"
    assert states["SPY"] == "CLOSE_REQUESTED"


def test_unreplied_bare_exit_with_two_open_signals_is_review(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "QQQ 500C 11/13 @ 2.00")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending(limit=10)
    seed(db, 12, "ALL OUT")
    decisions = matcher.process_pending(limit=10)
    assert decisions[0].status == "REVIEW_REQUIRED"
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 0


def test_reply_naming_a_different_ticker_is_review_not_close(db, catalog):  # noqa: F811
    seed(db, 10, "SPY 680C 11/13 @ 1.20")
    seed(db, 11, "QQQ 500C 11/13 @ 2.00")
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending(limit=10)
    seed(db, 12, "QQQ ALL OUT", reply=10)
    decisions = matcher.process_pending(limit=10)
    assert decisions[0].status == "REVIEW_REQUIRED"
    assert row(db, "SELECT count(*) FROM signal_close_requests")[0] == 0


@pytest.mark.parametrize("text,symbol,category,premium", [
    ("LOTTO: TSLA 450P 12/18 @ .45", "TSLA", "LOTTO", ".45"),
    ("LOTTO - TSLA 450P 12/18 @ 0.45", "TSLA", "LOTTO", "0.45"),
    ("BTO LOTTO SPY 680C 12/18 @ 1.20", "SPY", "LOTTO", "1.20"),
    ("$NVDA 150c 12/18 @ $1.20", "NVDA", "STANDARD", "1.20"),
    ("SPY 680C 12/18 @ $1.20 🚀🚀", "SPY", "STANDARD", "1.20"),
    ("🔥 SPY 680C 12/18 @ 1.20", "SPY", "STANDARD", "1.20"),
])
def test_decorated_entries_parse_to_the_same_exact_terms(text, symbol, category, premium):
    from decimal import Decimal
    alert = parse_message(text)
    assert alert.intent is SignalIntent.ENTRY
    assert (alert.underlying, alert.category) == (symbol, category)
    assert alert.premium == Decimal(premium)


@pytest.mark.parametrize("text", [
    "SPY 680C 12/18 @ 1🚀20", "SPY 680C 12/18 @ 1,20", "SPY 680C 12/18 @ 1.20-1.30",
    "SPY 680C 12/18 @ 1.2O", "STO SPY 680P 12/18 @ 1.20",
    "SPY 680C/685C 12/18 spread @ 1.20", "SPY 680C 12/18 @ -1.20", "SPY 680C 0DTE @ 1.20",
])
def test_malformed_or_unsupported_entries_never_become_orders(text):
    assert parse_message(text).intent is SignalIntent.REVIEW_REQUIRED
