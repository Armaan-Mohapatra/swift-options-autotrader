"""Operator-approved PAPER risk limits: one definition, enforced everywhere."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.orders.risk import Preflight, RiskCeilings, rejection_reason
from swift_autotrader.risk_limits import (
    DAILY_NET_LOSS_LIMIT_USD,
    MAX_CONTRACTS_PER_ALERT,
    PREMIUM_CEILING_USD,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight

CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
AT_LIMITS = {
    "MAX_CONTRACTS_PER_SIGNAL": "1",
    "MAX_PREMIUM_PER_SIGNAL_USD": "999",
    "PAPER_MAX_DAILY_NET_LOSS_USD": "750",
}


def test_approved_limits_are_the_documented_values():
    approved = (DAILY_NET_LOSS_LIMIT_USD, MAX_CONTRACTS_PER_ALERT, PREMIUM_CEILING_USD)
    assert approved == (Decimal("750"), 1, Decimal("999"))


def test_environment_defaults_stay_fail_closed_and_trade_count_is_uncapped():
    ceilings = RiskCeilings.from_environment({})
    assert ceilings.contracts_per_signal == 1
    assert ceilings.premium_per_signal_usd == 0  # zero still denies every buy
    assert ceilings.daily_net_loss_usd == Decimal("750")
    assert ceilings.total_exposure_usd is None  # no exposure cap
    assert ceilings.open_signal_count is None  # no trade-count cap


def test_blank_environment_caps_mean_unlimited():
    ceilings = RiskCeilings.from_environment(
        {**AT_LIMITS, "PAPER_MAX_TOTAL_EXPOSURE_USD": "", "PAPER_MAX_OPEN_SIGNALS": " "}
    )
    assert ceilings.total_exposure_usd is None
    assert ceilings.open_signal_count is None


def test_environment_accepts_the_approved_maximums_exactly():
    ceilings = RiskCeilings.from_environment(AT_LIMITS)
    assert ceilings.contracts_per_signal == 1
    assert ceilings.premium_per_signal_usd == Decimal("999")
    assert ceilings.daily_net_loss_usd == Decimal("750")


def test_optional_voluntary_caps_are_still_honored_when_an_operator_sets_them():
    ceilings = RiskCeilings.from_environment(
        {**AT_LIMITS, "PAPER_MAX_TOTAL_EXPOSURE_USD": "2500", "PAPER_MAX_OPEN_SIGNALS": "3"}
    )
    assert ceilings.total_exposure_usd == Decimal("2500")
    assert ceilings.open_signal_count == 3


@pytest.mark.parametrize(
    "override",
    [
        {"MAX_CONTRACTS_PER_SIGNAL": "2"},
        {"MAX_PREMIUM_PER_SIGNAL_USD": "999.01"},
        {"PAPER_MAX_DAILY_NET_LOSS_USD": "750.01"},
        {"PAPER_MAX_DAILY_NET_LOSS_USD": "0"},
        {"PAPER_MAX_DAILY_NET_LOSS_USD": "-5"},
        {"PAPER_MAX_DAILY_NET_LOSS_USD": "NaN"},
        {"PAPER_MAX_DAILY_NET_LOSS_USD": "Infinity"},
        {"PAPER_MAX_TOTAL_EXPOSURE_USD": "-1"},
        {"PAPER_MAX_OPEN_SIGNALS": "0"},
    ],
)
def test_environment_rejects_values_beyond_the_approved_maximums(override):
    with pytest.raises(ValueError):
        RiskCeilings.from_environment({**AT_LIMITS, **override})


@pytest.mark.parametrize(
    "override",
    [
        {"MAX_CONTRACTS_PER_SIGNAL": "2"},
        {"MAX_PREMIUM_PER_SIGNAL_USD": "999.01"},
    ],
)
def test_settings_enforce_the_same_maximums(override):
    with pytest.raises(ConfigurationError):
        Settings.from_env(override)


def test_settings_accept_the_approved_maximums():
    settings = Settings.from_env(
        {"MAX_CONTRACTS_PER_SIGNAL": "1", "MAX_PREMIUM_PER_SIGNAL_USD": "999"}
    )
    assert settings.max_contracts_per_signal == 1
    assert settings.max_premium_per_signal_usd == Decimal("999")


def gate(
    limit: str, *, pnl: str = "0", quantity: int = 1, outstanding: str = "0",
    active: int = 0, buying_power: str = "100000", env: dict[str, str] | None = None,
) -> str | None:
    ceilings = RiskCeilings.from_environment({**AT_LIMITS, **(env or {})})
    preflight = Preflight(
        True, True, True, Decimal(buying_power), Decimal("0.01"), CONTRACT,
    )
    return rejection_reason(
        CONTRACT, Decimal(limit), quantity, ceilings, preflight,
        outstanding_usd=Decimal(outstanding), active_signals=active,
        realized_net_pnl_today_usd=Decimal(pnl),
    )


def test_daily_loss_circuit_breaker_trips_exactly_at_the_ceiling():
    assert gate("1.20", pnl="0") is None
    assert gate("1.20", pnl="-749.99") is None
    assert gate("1.20", pnl="-750") == "DAILY_LOSS_CEILING_REACHED"
    assert gate("1.20", pnl="-1200.50") == "DAILY_LOSS_CEILING_REACHED"


def test_daily_profit_never_blocks_and_unverifiable_pnl_fails_closed():
    assert gate("1.20", pnl="500") is None
    assert gate("1.20", pnl="NaN") == "DAILY_PNL_UNVERIFIED"


def test_premium_ceiling_is_999_dollars_per_alert():
    assert gate("9.99") is None  # 9.99 x 1 x 100 = 999.00
    assert gate("10.00") == "SIGNAL_PREMIUM_CAP_EXCEEDED"  # 1000.00


def test_second_contract_is_rejected_by_the_gate_under_approved_ceilings():
    assert gate("1.20", quantity=2) == "QUANTITY_CAP_EXCEEDED"


def test_trade_count_has_no_cap_only_buying_power_decides():
    # 500 trades and $50,000 already committed: still allowed while buying power covers it.
    assert gate("1.20", active=500, outstanding="50000", buying_power="100000") is None
    # Buying power of $50,100 leaves $100, less than this trade's $120 premium.
    assert gate(
        "1.20", active=500, outstanding="50000", buying_power="50100",
    ) == "INSUFFICIENT_BUYING_POWER"


def test_each_trade_is_limited_by_remaining_buying_power_not_a_count():
    assert gate("1.20", outstanding="0", buying_power="300") is None
    assert gate("1.20", outstanding="120", buying_power="300") is None
    assert gate("1.20", outstanding="240", buying_power="300") == "INSUFFICIENT_BUYING_POWER"
