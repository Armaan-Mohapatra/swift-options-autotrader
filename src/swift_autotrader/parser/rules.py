"""Immutable policy vocabulary. Full parsing MUST wait for Phase 2."""

from __future__ import annotations

from enum import Enum


class SignalIntent(str, Enum):
    ENTRY = "ENTRY"
    METADATA_ONLY = "METADATA_ONLY"
    CLOSE_100_PERCENT = "CLOSE_100_PERCENT"
    IGNORE_NON_OPTION = "IGNORE_NON_OPTION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


ACCEPTED_OPTION_LABELS = frozenset({"LOTTO"})
NO_ADD_MARKERS = frozenset({"ADDING MORE", "AVERAGING DOWN", "SCALE IN", "ADD"})
NEW_AVERAGE_MARKERS = frozenset({"NEW AVG", "NEW AVERAGE", "REVISED AVG"})
FULL_CLOSE_MARKERS = frozenset(
    {"SOLD HALF", "SOLD 1/3", "TRIM", "SCALED OUT", "RUNNERS OUT", "ALL OUT"}
)
