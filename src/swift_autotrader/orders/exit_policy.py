"""No market/price-guessed exit orders: explicit fresh paper quote required."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from swift_autotrader.broker.paper import as_utc
from swift_autotrader.signals.identity import OptionContract


@dataclass(frozen=True, slots=True)
class ExitQuote:
    contract: OptionContract
    bid: Decimal
    tick: Decimal
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    max_quote_age_seconds: int = 30

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> ExitPolicy:
        env = os.environ if values is None else values
        return cls(max_quote_age_seconds=int(env.get("PAPER_EXIT_QUOTE_MAX_AGE_SECONDS", "30")))

    def __post_init__(self) -> None:
        if not 0 <= self.max_quote_age_seconds <= 300:
            raise ValueError("invalid quote age ceiling")

    def choose_limit(
        self, contract: OptionContract, quote: ExitQuote | None, now: datetime,
    ) -> tuple[Decimal | None, str | None]:
        if quote is None:
            return None, "EXIT_QUOTE_MISSING"
        if quote.contract != contract:
            return None, "EXIT_QUOTE_CONTRACT_MISMATCH"
        age = (as_utc(now) - as_utc(quote.observed_at_utc)).total_seconds()
        if age < 0 or age > self.max_quote_age_seconds:
            return None, "EXIT_QUOTE_STALE"
        if (not quote.tick.is_finite() or quote.tick <= 0 or
            not quote.bid.is_finite() or quote.bid <= 0):
            return None, "EXIT_QUOTE_INVALID"
        if quote.bid % quote.tick != 0:
            return None, "EXIT_INVALID_TICK"
        return quote.bid, None
