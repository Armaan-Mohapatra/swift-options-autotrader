"""Pure validation of brokerage quantities; no client or network access."""
from decimal import Decimal, InvalidOperation


def exact_contract_quantity(value: object, *, minimum: int = 0) -> int:
    """Reject missing, fractional, nonfinite and inexact quantities."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Contract quantity is missing or inexact")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Malformed contract quantity") from exc
    if (not quantity.is_finite() or quantity < minimum or
            quantity != quantity.to_integral_value()):
        raise ValueError("Contract quantity must be a nonnegative whole number")
    return int(quantity)


def reconciled_processed_quantity(order: dict) -> int:
    """A broker must explicitly report processed quantity; aliases must agree."""
    keys = [key for key in ("filled_quantity", "processed_quantity") if key in order]
    if not keys:
        raise ValueError("Broker processed quantity missing")
    quantities = [exact_contract_quantity(order[key]) for key in keys]
    if any(quantity != quantities[0] for quantity in quantities):
        raise ValueError("Broker processed quantities conflict")
    return quantities[0]
