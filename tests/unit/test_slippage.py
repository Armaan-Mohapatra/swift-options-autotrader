from decimal import Decimal
import pytest
from swift_autotrader.orders.slippage import resolve_live_entry_limit, SlippageExceededError


def test_resolve_entry_limit_at_or_below_alert():
    # Ask is lower than alert price -> keep alert price for guaranteed fill
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.20"),
        current_ask=Decimal("1.15"),
        tick=Decimal("0.01"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("1.20")


def test_resolve_entry_limit_within_slippage_tolerance():
    # Alert was 1.20, Ask is 1.25 (+4.17%), 10% max allowed -> limit becomes 1.25
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.20"),
        current_ask=Decimal("1.25"),
        tick=Decimal("0.01"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("1.25")


def test_resolve_entry_limit_tick_rounding():
    # 5-cent tick: Alert was 1.20, Ask is 1.22 -> rounded up to 1.25 (which is within 10% of 1.20 = 1.32)
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.20"),
        current_ask=Decimal("1.22"),
        tick=Decimal("0.05"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("1.25")


def test_resolve_entry_limit_exceeds_slippage():
    # Alert was 1.20, Ask jumped to 1.40 (+16.7%), 10% tolerance -> raises error
    with pytest.raises(SlippageExceededError, match="exceeds max allowed"):
        resolve_live_entry_limit(
            alert_limit=Decimal("1.20"),
            current_ask=Decimal("1.40"),
            tick=Decimal("0.01"),
            max_slippage_pct=Decimal("10"),
        )


def test_resolve_entry_limit_invalid_inputs():
    with pytest.raises(ValueError, match="positive and finite"):
        resolve_live_entry_limit(Decimal("0"), Decimal("1.20"), Decimal("0.01"))
    with pytest.raises(ValueError, match="positive and finite"):
        resolve_live_entry_limit(Decimal("-1.20"), Decimal("1.20"), Decimal("0.01"))
    with pytest.raises(ValueError, match="positive and finite"):
        resolve_live_entry_limit(Decimal("1.20"), Decimal("0"), Decimal("0.01"))
    with pytest.raises(ValueError, match="positive and finite"):
        resolve_live_entry_limit(Decimal("1.20"), Decimal("1.20"), Decimal("0"))


def test_resolve_entry_limit_exact_boundary():
    # Exact 10% boundary: 1.00 + 10% = 1.10, current ask is exactly 1.10
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.00"),
        current_ask=Decimal("1.10"),
        tick=Decimal("0.01"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("1.10")


def test_resolve_entry_limit_tick_cap_fallback():
    # Alert 1.20 with 10% -> max allowed is 1.32.
    # Current ask 1.31 with 0.05 tick: 1.31 rounds up to 1.35, which exceeds 1.32.
    # Engine falls back to highest valid tick <= max_allowed: 1.30
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.20"),
        current_ask=Decimal("1.31"),
        tick=Decimal("0.05"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("1.30")


def test_resolve_entry_limit_zero_slippage_tolerance():
    # 0% tolerance allows exact match
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.20"),
        current_ask=Decimal("1.20"),
        tick=Decimal("0.01"),
        max_slippage_pct=Decimal("0"),
    )
    assert limit == Decimal("1.20")

    # 0% tolerance rejects any higher ask
    with pytest.raises(SlippageExceededError):
        resolve_live_entry_limit(
            alert_limit=Decimal("1.20"),
            current_ask=Decimal("1.21"),
            tick=Decimal("0.01"),
            max_slippage_pct=Decimal("0"),
        )


def test_resolve_entry_limit_negative_slippage_tolerance():
    """T2-F4-04: Negative slippage tolerance requires price improvement below alert."""
    # Negative tolerance (-5% on $1.00 alert -> max allowed is $0.95)
    # Ask of $0.98 breaches max allowed price of $0.95
    with pytest.raises(SlippageExceededError, match="exceeds max allowed"):
        resolve_live_entry_limit(
            alert_limit=Decimal("1.00"),
            current_ask=Decimal("0.98"),
            tick=Decimal("0.01"),
            max_slippage_pct=Decimal("-5"),
        )


def test_resolve_entry_limit_subpenny_tick_precision():
    """T2-F4-05: Sub-penny tick precision (0.001) resolves exact fractional limit."""
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("1.200"),
        current_ask=Decimal("1.205"),
        tick=Decimal("0.001"),
        max_slippage_pct=Decimal("5"),
    )
    assert limit == Decimal("1.205")


def test_resolve_entry_limit_large_spread_tick_rounding():
    """T2-F4-06: Wide tick sizes (0.10) round up cleanly within tolerance window."""
    limit = resolve_live_entry_limit(
        alert_limit=Decimal("2.00"),
        current_ask=Decimal("2.15"),
        tick=Decimal("0.10"),
        max_slippage_pct=Decimal("10"),
    )
    assert limit == Decimal("2.20")
