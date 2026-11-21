"""Synthetic-only strict rule tests: no paid Swift text and no broker access."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import StaticInstrumentCatalog, resolve_entry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "swift_phase2_synthetic.json"
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[x["id"] for x in CASES])
def test_synthetic_alert_classification(case):
    parsed = parse_message(case["text"])
    assert parsed.intent.value == case["intent"], case
    if "underlying" in case:
        assert parsed.underlying == case["underlying"]
    if "category" in case:
        assert parsed.category == case["category"]
    if "premium" in case:
        assert parsed.premium == Decimal(case["premium"])
    if "average" in case:
        assert parsed.swift_average == Decimal(case["average"])


@pytest.mark.parametrize("text,category", [
    ("OPTION - SWING\n\nSPY 600.00C 12/18 @ 1.50", "SWING"),
    ("OPTION - LOTTO\n\nQQQ 5C 1/16 @ .45", "LOTTO"),
    ("\U0001F319 OPTION - SCALP \U0001F319\n\nSPY 600P 12/18 @ 2.10", "SCALP"),
])
def test_real_option_header_entry_is_an_entry(text, category):
    parsed = parse_message(text)
    assert parsed.intent is SignalIntent.ENTRY and parsed.category == category
    assert parsed.underlying in {"SPY", "QQQ"} and parsed.premium is not None


@pytest.mark.parametrize("text", [
    "STOCK - SWING\n\nSPY 600.00C 12/18 @ 1.50",              # not an option alert
    "OPTION - WATCHLIST\n\nSPY 600.00C 12/18 @ 1.50",         # unknown category
    "OPTION - SWING\n\nADDING MORE SPY 600.00C 12/18 @ 1.50",  # ADD must never enter
    "OPTION - SWING\n\nSOLD HALF SPY 600.00C 12/18 @ 1.50",    # exit, not entry
])
def test_option_header_does_not_make_other_messages_entries(text):
    assert parse_message(text).intent is not SignalIntent.ENTRY


@pytest.mark.parametrize("text", [
    "OPTION - SWING 500C 12/20 @ 1",            # ticker missing after the header
    "OPTION - SWING SWING 500C 12/20 @ 1",
    "SWING 500C 12/20 @ 1",
    "LOTTO 500C 12/20 @ 1",
])
def test_category_word_is_never_taken_as_the_ticker(text):
    assert parse_message(text).intent is not SignalIntent.ENTRY


@pytest.mark.parametrize("bad", [None, 12, [], {}])
def test_non_string_is_error(bad):
    with pytest.raises(TypeError):
        parse_message(bad)


def test_catalog_verifies_yearless_not_guessing():
    alert = parse_message("SPY 680C 11/13 @ 1.20")
    assert resolve_entry(alert, None, as_of=date(2026, 11, 12)) == (
        None, "INSTRUMENT_CATALOG_REQUIRED"
    )
    contract = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
    catalog = StaticInstrumentCatalog([contract])
    assert resolve_entry(alert, catalog, as_of=date(2026, 11, 12)) == (contract, None)


def test_ambiguity_two_expiry_years():
    alert = parse_message("SPY 680C 11/13 @ 1.20")
    contracts = [
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(y, 11, 13))
        for y in (2026, 2027)
    ]
    assert resolve_entry(alert, StaticInstrumentCatalog(contracts), as_of=date(2026, 11, 12)) == (
        None, "AMBIGUOUS_EXPIRY_YEAR"
    )


def test_explicit_year_does_not_override_catalog():
    alert = parse_message("SPY 680C 11/13/2026 @ 1.20")
    catalog = StaticInstrumentCatalog([
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2027, 11, 13))
    ])
    assert resolve_entry(alert, catalog, as_of=date(2026, 11, 12)) == (
        None, "CONTRACT_NOT_FOUND"
    )


def test_expired_contract_rejected():
    alert = parse_message("SPY 680C 11/13/2026 @ 1.20")
    catalog = StaticInstrumentCatalog([
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
    ])
    assert resolve_entry(alert, catalog, as_of=date(2026, 11, 14)) == (
        None, "CONTRACT_NOT_FOUND"
    )


def test_same_strike_put_is_not_call():
    alert = parse_message("SPY 680P 11/13/2026 @ 1.20")
    catalog = StaticInstrumentCatalog([
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
    ])
    assert resolve_entry(alert, catalog, as_of=date(2026, 11, 12))[0] is None


def test_unbounded_text_rejected_without_regex_backtracking():
    assert parse_message("SPY " * 1000).intent == SignalIntent.REVIEW_REQUIRED
