from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.policy import deadline_from_acceptance, deadline_reached
from swift_autotrader.parser.rules import (
    ACCEPTED_OPTION_LABELS,
    FULL_CLOSE_MARKERS,
    NEW_AVERAGE_MARKERS,
    NO_ADD_MARKERS,
    SignalIntent,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight


def test_60_minute_acceptance_deadline_utc():
    accepted = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
    assert deadline_from_acceptance(accepted) == accepted + timedelta(seconds=3600)
    assert not deadline_reached(accepted, accepted + timedelta(minutes=59, seconds=59))
    assert deadline_reached(accepted, accepted + timedelta(minutes=60))


def test_reject_naive_time():
    with pytest.raises(ValueError):
        deadline_from_acceptance(datetime(2026, 11, 13))
    with pytest.raises(ValueError):
        deadline_reached(datetime.now(UTC), datetime(2026, 11, 13))


def test_core_semantics_are_captured_as_non_executable_policy():
    assert "LOTTO" in ACCEPTED_OPTION_LABELS
    assert "ADDING MORE" in NO_ADD_MARKERS
    assert "NEW AVG" in NEW_AVERAGE_MARKERS
    for marker in ("SOLD HALF", "TRIM", "RUNNERS OUT", "ALL OUT"):
        assert marker in FULL_CLOSE_MARKERS
    assert SignalIntent.CLOSE_100_PERCENT.value == "CLOSE_100_PERCENT"


def test_strict_identity_and_exact_decimal_limit():
    c1 = OptionContract("SPY", OptionRight.CALL, Decimal("680"), datetime(2026, 11, 13).date())
    c2 = OptionContract("SPY", OptionRight.CALL, Decimal("681"), datetime(2026, 11, 13).date())
    assert c1 != c2
    intent = OrderIntent(
        "logical-1", "signal-1:entry", c1, OrderSide.BUY_TO_OPEN, 1, Decimal("1.20")
    )
    assert str(intent.limit_price) == "1.20"
    with pytest.raises(ValueError):
        OrderIntent("logical-1", "", c1, OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"))
