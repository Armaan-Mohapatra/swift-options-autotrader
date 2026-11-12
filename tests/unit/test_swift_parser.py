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
