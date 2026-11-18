"""Requirement-driven unit test suite for Live Preflight & Daily Loss Risk Controls (R2 / Feature 3).

Covers:
- Tier 1: Feature Coverage (Clearance happy path, daily loss threshold, concurrent position cap,
  buying power check, premium cap, environment mapping).
- Tier 2: Boundary & Corner Cases (Exact loss boundaries, exact concurrent position counts,
  negative/NaN buying power, tick misalignment, market closed, contract mismatch, permissions).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from swift_autotrader.orders.risk import Preflight, RiskCeilings, rejection_reason
from swift_autotrader.signals.identity import OptionContract, OptionRight

CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
OTHER_CONTRACT = OptionContract("SPY", OptionRight.PUT, Decimal("670"), date(2026, 11, 20))


def make_ceilings(
    *,
    max_daily_loss: str = "500",
    contracts_per_signal: int = 1,
    premium_per_signal: str = "999",
    total_exposure: str | None = None,
    open_signal_count: int | None = None,
) -> RiskCeilings:
    # Owner contract: no trade-count or exposure cap by default; both are optional
    # voluntary operator caps and buying power is the only mandatory bound.
    return RiskCeilings(
        contracts_per_signal=contracts_per_signal,
        premium_per_signal_usd=Decimal(premium_per_signal),
        total_exposure_usd=None if total_exposure is None else Decimal(total_exposure),
        open_signal_count=open_signal_count,
        entry_max_age_seconds=120,
        daily_net_loss_usd=Decimal(max_daily_loss),
    )


def make_preflight(
    *,
    contract: OptionContract = CONTRACT,
    instrument_verified: bool = True,
    options_permission: bool = True,
    market_session_open: bool = True,
    available_buying_power: str = "5000",
    premium_tick: str = "0.01",
) -> Preflight:
    return Preflight(
        instrument_verified=instrument_verified,
        options_permission=options_permission,
        market_session_open=market_session_open,
        available_buying_power_usd=Decimal(available_buying_power),
        premium_tick=Decimal(premium_tick),
        contract=contract,
    )


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_preflight_full_clearance() -> None:
    """T1-F3-01: All risk gates pass; returns None (order approved)."""
    ceilings = make_ceilings()
    preflight = make_preflight()
    reason = rejection_reason(
        CONTRACT,
        Decimal("1.20"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=0,
        realized_net_pnl_today_usd=Decimal("0"),
    )
    assert reason is None


def test_daily_loss_limit_rejection() -> None:
    """T1-F3-02: Realized loss exceeds daily ceiling; blocks new entry."""
    ceilings = make_ceilings(max_daily_loss="500")
    preflight = make_preflight()
    reason = rejection_reason(
        CONTRACT,
        Decimal("1.20"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=0,
        realized_net_pnl_today_usd=Decimal("-500.01"),
    )
    assert reason == "DAILY_LOSS_CEILING_REACHED"


def test_no_trade_count_cap_by_default() -> None:
    """T1-F3-03a: Owner contract: no default trade-count cap; buying power bounds entries."""
    ceilings = make_ceilings()
    assert ceilings.open_signal_count is None and ceilings.total_exposure_usd is None
    reason = rejection_reason(
        CONTRACT, Decimal("1.20"), 1, ceilings, make_preflight(available_buying_power="100000"),
        outstanding_usd=Decimal("50000"), active_signals=500,
    )
    assert reason is None


def test_optional_open_signal_cap_when_operator_sets_one() -> None:
    """T1-F3-03: A voluntary operator open-signal cap is still honoured when configured."""
    ceilings = make_ceilings(open_signal_count=3)
    preflight = make_preflight()
    reason = rejection_reason(
        CONTRACT,
        Decimal("1.20"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=3,
        realized_net_pnl_today_usd=Decimal("0"),
    )
    assert reason == "OPEN_SIGNAL_CAP_EXCEEDED"


def test_buying_power_insufficiency() -> None:
    """T1-F3-04: Order notional exceeds available buying power."""
    ceilings = make_ceilings()
    preflight = make_preflight(available_buying_power="100.00")
    # Notional for 1 contract at 1.20 is $120.00 > $100.00
    reason = rejection_reason(
        CONTRACT,
        Decimal("1.20"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=0,
        realized_net_pnl_today_usd=Decimal("0"),
    )
    assert reason == "INSUFFICIENT_BUYING_POWER"


def test_signal_premium_ceiling_enforcement() -> None:
    """T1-F3-05: Order notional exceeds premium_per_signal_usd."""
    ceilings = make_ceilings(premium_per_signal="200.00")
    preflight = make_preflight()
    # 1 contract at 2.50 is $250.00 > $200.00 ceiling
    reason = rejection_reason(
        CONTRACT,
        Decimal("2.50"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=0,
        realized_net_pnl_today_usd=Decimal("0"),
    )
    assert reason == "SIGNAL_PREMIUM_CAP_EXCEEDED"


def test_risk_ceilings_from_environment() -> None:
    """T1-F3-06: Parse risk ceilings from environment mapping."""
    env = {
        "PAPER_MAX_DAILY_NET_LOSS_USD": "750",
        "MAX_CONTRACTS_PER_SIGNAL": "1",
        "MAX_PREMIUM_PER_SIGNAL_USD": "500",
        "PAPER_MAX_TOTAL_EXPOSURE_USD": "8000",
        "PAPER_MAX_OPEN_SIGNALS": "5",
        "PAPER_ENTRY_MAX_AGE_SECONDS": "90",
    }
    ceilings = RiskCeilings.from_environment(env)
    assert ceilings.daily_net_loss_usd == Decimal("750")
    assert ceilings.open_signal_count == 5
    with pytest.raises(ValueError, match="exact alert limit"):
        RiskCeilings.from_environment(dict(env, SLIPPAGE_TOLERANCE_PCT="12"))
    with pytest.raises(ValueError, match="approved maximum"):
        RiskCeilings.from_environment(dict(env, MAX_CONTRACTS_PER_SIGNAL="2"))
    with pytest.raises(ValueError, match="invalid paper risk environment"):
        RiskCeilings.from_environment(dict(env, PAPER_MAX_DAILY_NET_LOSS_USD="751"))
    assert ceilings.contracts_per_signal == 1
    assert ceilings.premium_per_signal_usd == Decimal("500")


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


@pytest.mark.parametrize(
    ("loss", "expected"),
    [
        (Decimal("499.99"), None),
        (Decimal("500.00"), "DAILY_LOSS_CEILING_REACHED"),
        (Decimal("500.01"), "DAILY_LOSS_CEILING_REACHED"),
        (Decimal("1000.00"), "DAILY_LOSS_CEILING_REACHED"),
    ],
)
def test_exact_loss_ceiling_boundary(loss: Decimal, expected: str | None) -> None:
    """T2-F3-01: Exact boundary check for cumulative daily loss limit."""
    ceilings = make_ceilings(max_daily_loss="500")
    preflight = make_preflight()
    result = rejection_reason(
        CONTRACT,
        Decimal("1.00"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=0,
        realized_net_pnl_today_usd=-loss,
    )
    assert result == expected


@pytest.mark.parametrize(
    ("active_signals", "expected"),
    [
        (2, None),
        (3, "OPEN_SIGNAL_CAP_EXCEEDED"),
        (4, "OPEN_SIGNAL_CAP_EXCEEDED"),
    ],
)
def test_exact_concurrent_positions_boundary(active_signals: int, expected: str | None) -> None:
    """T2-F3-02: Exact boundary check for concurrent position caps."""
    ceilings = make_ceilings(open_signal_count=3)
    preflight = make_preflight()
    result = rejection_reason(
        CONTRACT,
        Decimal("1.00"),
        1,
        ceilings,
        preflight,
        outstanding_usd=Decimal("0"),
        active_signals=active_signals,
        realized_net_pnl_today_usd=Decimal("0"),
    )
    assert result == expected


def test_infinite_or_negative_buying_power() -> None:
    """T2-F3-03: Negative or non-finite buying power fails closed."""
    ceilings = make_ceilings()
    # Negative buying power
    preflight_neg = make_preflight(available_buying_power="-50.00")
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight_neg,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "INSUFFICIENT_BUYING_POWER"

    # NaN buying power
    preflight_nan = Preflight(
        instrument_verified=True,
        options_permission=True,
        market_session_open=True,
        available_buying_power_usd=Decimal("NaN"),
        premium_tick=Decimal("0.01"),
        contract=CONTRACT,
    )
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight_nan,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "INSUFFICIENT_BUYING_POWER"


def test_non_standard_premium_tick_alignment() -> None:
    """T2-F3-04: Limit price must align to the option's minimum tick size."""
    ceilings = make_ceilings()
    # Nickel tick option ($0.05) with non-aligned limit ($1.23)
    preflight = make_preflight(premium_tick="0.05")
    assert rejection_reason(
        CONTRACT, Decimal("1.23"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "INVALID_PREMIUM_TICK"

    # Nickel tick option with aligned limit ($1.25)
    assert rejection_reason(
        CONTRACT, Decimal("1.25"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) is None


def test_market_closed_guard() -> None:
    """T2-F3-05: Reject orders outside regular market trading hours."""
    ceilings = make_ceilings()
    preflight = make_preflight(market_session_open=False)
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "MARKET_SESSION_CLOSED"


def test_contract_mismatch_guard() -> None:
    """T2-F3-06: Preflight contract must strictly match requested contract."""
    ceilings = make_ceilings()
    preflight = make_preflight(contract=OTHER_CONTRACT)
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "INSTRUMENT_NOT_VERIFIED"


def test_instrument_not_verified() -> None:
    """T2-F3-07: Unverified broker instrument rejects entry."""
    ceilings = make_ceilings()
    preflight = make_preflight(instrument_verified=False)
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "INSTRUMENT_NOT_VERIFIED"


def test_options_permission_required() -> None:
    """T2-F3-08: Ineligible option account rejects entry."""
    ceilings = make_ceilings()
    preflight = make_preflight(options_permission=False)
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "OPTIONS_PERMISSION_REQUIRED"


def test_quantity_cap_exceeded() -> None:
    """T2-F3-09: Quantity requested exceeds contracts_per_signal."""
    ceilings = make_ceilings(contracts_per_signal=1)
    preflight = make_preflight()
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 2, ceilings, preflight,
        outstanding_usd=Decimal("0"), active_signals=0,
    ) == "QUANTITY_CAP_EXCEEDED"


def test_total_exposure_cap_exceeded() -> None:
    """T2-F3-10: Outstanding exposure plus order notional exceeds exposure ceiling."""
    ceilings = make_ceilings(total_exposure="500.00")
    preflight = make_preflight()
    # Outstanding = $450, order notional = $100 -> Total $550 > $500
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, ceilings, preflight,
        outstanding_usd=Decimal("450.00"), active_signals=0,
    ) == "TOTAL_EXPOSURE_CAP_EXCEEDED"


def test_risk_ceilings_post_init_validation() -> None:
    """T2-F3-11: RiskCeilings dataclass enforces strict boundary validation."""
    with pytest.raises(ValueError, match="invalid quantity ceiling"):
        RiskCeilings(contracts_per_signal=0)

    with pytest.raises(ValueError, match="invalid quantity ceiling"):
        RiskCeilings(contracts_per_signal=101)

    with pytest.raises(ValueError, match="invalid active signal ceiling"):
        RiskCeilings(open_signal_count=0)

    with pytest.raises(ValueError, match="daily net loss ceiling"):
        RiskCeilings(daily_net_loss_usd=Decimal("-1"))

    with pytest.raises(ValueError, match="daily net loss ceiling"):
        RiskCeilings(daily_net_loss_usd=Decimal("750.01"))

    with pytest.raises(ValueError, match="invalid dollar ceilings"):
        RiskCeilings(premium_per_signal_usd=Decimal("-1"))


def test_unknown_daily_pnl_blocks_entries() -> None:
    """T2-F3-12: Unknown (non-finite) daily P&L fails closed."""
    assert rejection_reason(
        CONTRACT, Decimal("1.00"), 1, make_ceilings(), make_preflight(),
        outstanding_usd=Decimal("0"), active_signals=0,
        realized_net_pnl_today_usd=Decimal("NaN"),
    ) == "DAILY_PNL_UNVERIFIED"
