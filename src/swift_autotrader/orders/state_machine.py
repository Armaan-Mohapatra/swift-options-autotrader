"""Allowed durable order lifecycle. Broker fills, never request text, prove completion."""
from enum import StrEnum


class OrderState(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    UNKNOWN = "UNKNOWN"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    ALERT_REQUIRED = "ALERT_REQUIRED"
    # Legacy Phase 0 names retained for compatible reporting.
    SEEN = "SEEN"
    VALIDATED = "VALIDATED"
    ENTRY_READY = "ENTRY_READY"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    CLOSED = "CLOSED"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"


TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED"})
ACTIVE = frozenset({"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"})

_ALLOWED = {
    "INTENT_RECORDED": frozenset({
        "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED", "REJECTED",
    }),
    "SUBMITTED": frozenset({
        "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED", "REJECTED",
    }),
    "ACCEPTED": frozenset({
        "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED", "REJECTED",
    }),
    "PARTIALLY_FILLED": frozenset({
        "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELED",
    }),
    "CANCEL_PENDING": frozenset({"CANCEL_PENDING", "CANCELED", "FILLED"}),
    "FILLED": frozenset({"FILLED"}),
    "CANCELED": frozenset({"CANCELED"}),
    "REJECTED": frozenset({"REJECTED"}),
}


def broker_transition_allowed(previous: str, current: str) -> bool:
    """Terminal statuses are irreversible; pending cancellation may race a final fill."""
    return current in _ALLOWED.get(previous, frozenset())
