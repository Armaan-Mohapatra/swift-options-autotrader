"""Strong option contract identity; never match by underlying alone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    right: OptionRight
    strike: Decimal
    expiration: date

    def __post_init__(self) -> None:
        if not self.underlying or self.underlying != self.underlying.upper():
            raise ValueError("Underlying must be an uppercase nonblank symbol")
        if not self.strike.is_finite() or self.strike <= 0:
            raise ValueError("Strike must be finite and positive")
