"""Intelligent slippage and limit price resolution for live option entries.

When a live alert arrives from SwiftTrades:
- If the current Ask <= Alert Limit: We submit at Alert Limit (or Ask) as a limit; a fill is not guaranteed.
- If Alert Limit < current Ask <= Alert Limit * (1 + tolerance): Price moved slightly;
  the limit can be adjusted within the ceiling; fills are not guaranteed.
- If current Ask > Alert Limit * (1 + tolerance): Price ran away; reject with SLIPPAGE_EXCEEDED.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_UP


class SlippageExceededError(ValueError):
    """The current market Ask exceeded the maximum allowed slippage threshold."""


def resolve_live_entry_limit(
    alert_limit: Decimal,
    current_ask: Decimal,
    tick: Decimal,
    max_slippage_pct: Decimal = Decimal("10"),
) -> Decimal:
    """Calculate the actionable limit price for a live buy-to-open order.

    Args:
        alert_limit: Exact numeric limit specified in Swift alert (e.g. 1.20)
        current_ask: Current broker/market NBBO ask price (e.g. 1.25)
        tick: Minimum option tick size (e.g. 0.01 or 0.05)
        max_slippage_pct: Maximum allowed positive slippage percentage (e.g. 10 for 10%)

    Returns:
        The valid tick-rounded limit price to submit.

    Raises:
        SlippageExceededError if current ask exceeds alert_limit + max_slippage_pct.
    """
    if not all(isinstance(v, Decimal) and v.is_finite() for v in
               (alert_limit, current_ask, tick, max_slippage_pct)):
        raise ValueError("Prices and tick must be positive and finite; tolerance must be finite")
    if alert_limit <= 0 or current_ask <= 0 or tick <= 0:
        raise ValueError("Prices and tick must be positive and finite")

    max_allowed_price = alert_limit * (Decimal("1") + (max_slippage_pct / Decimal("100")))

    if current_ask > max_allowed_price:
        raise SlippageExceededError(
            f"Current ask ${current_ask} exceeds max allowed ${max_allowed_price:.2f} "
            f"({max_slippage_pct}% slippage from alert ${alert_limit})"
        )

    # If current ask is at or below alert limit, resting limit at alert_limit is safe and fills
    # Always enforce the tick grid, including when the quote is below the alert.
    target = (max(alert_limit, current_ask) / tick).to_integral_value(rounding=ROUND_UP) * tick

    # Ensure the tick-rounded target doesn't breach the max allowed
    if target > max_allowed_price:
        # Fall back to highest tick at or below max_allowed_price
        target = (max_allowed_price // tick) * tick

    if target <= 0 or target > max_allowed_price or target % tick != 0:
        raise SlippageExceededError("No positive valid tick within slippage ceiling")
    return target
