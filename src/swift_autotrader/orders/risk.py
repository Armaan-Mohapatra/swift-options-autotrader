"""Explicit preflight policy for synthetic PAPER execution; deny by default."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from swift_autotrader.signals.identity import OptionContract


@dataclass(frozen=True, slots=True)
class RiskCeilings:
    contracts_per_signal: int = 1
    premium_per_signal_usd: Decimal = Decimal("0")
    total_exposure_usd: Decimal = Decimal("0")
    open_signal_count: int = 1
    entry_max_age_seconds: int = 120

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> RiskCeilings:
        """Strict optional paper-risk environment values; zero-dollar default blocks buys."""
        env = os.environ if values is None else values
        try:
            return cls(
                contracts_per_signal=int(env.get("MAX_CONTRACTS_PER_SIGNAL", "1")),
                premium_per_signal_usd=Decimal(env.get("MAX_PREMIUM_PER_SIGNAL_USD", "0")),
                total_exposure_usd=Decimal(env.get("PAPER_MAX_TOTAL_EXPOSURE_USD", "0")),
                open_signal_count=int(env.get("PAPER_MAX_OPEN_SIGNALS", "1")),
                entry_max_age_seconds=int(env.get("PAPER_ENTRY_MAX_AGE_SECONDS", "120")),
            )
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("invalid paper risk environment") from exc

    def __post_init__(self) -> None:
        if not 1 <= self.contracts_per_signal <= 100:
            raise ValueError("invalid quantity ceiling")
        if not 1 <= self.open_signal_count <= 1000:
            raise ValueError("invalid active signal ceiling")
        if not 0 <= self.entry_max_age_seconds <= 600:
            raise ValueError("invalid entry freshness ceiling")
        if any(not x.is_finite() or x < 0 for x in (
            self.premium_per_signal_usd, self.total_exposure_usd,
        )):
            raise ValueError("invalid dollar ceilings")


@dataclass(frozen=True, slots=True)
class Preflight:
    instrument_verified: bool = False
    options_permission: bool = False
    market_session_open: bool = False
    available_buying_power_usd: Decimal = Decimal("0")
    premium_tick: Decimal = Decimal("0")
    contract: OptionContract | None = None


class PreflightProvider(Protocol):
    def check(self, contract: OptionContract) -> Preflight: ...


class PaperPreflight:
    """Synthetic, explicitly supplied state. Not official market verification."""

    def __init__(self, instruments: dict[OptionContract, Preflight] | None = None):
        self.instruments = dict(instruments or {})

    def check(self, contract: OptionContract) -> Preflight:
        return self.instruments.get(contract, Preflight())


def rejection_reason(
    contract: OptionContract, limit: Decimal, quantity: int, ceilings: RiskCeilings,
    preflight: Preflight, *, outstanding_usd: Decimal, active_signals: int,
) -> str | None:
    """Never inspect current option price; exact Swift price survives unchanged."""
    if not preflight.instrument_verified or preflight.contract != contract:
        return "INSTRUMENT_NOT_VERIFIED"
    if not preflight.options_permission:
        return "OPTIONS_PERMISSION_REQUIRED"
    if not preflight.market_session_open:
        return "MARKET_SESSION_CLOSED"
    if not preflight.premium_tick.is_finite() or preflight.premium_tick <= 0:
        return "TICK_RULE_UNVERIFIED"
    if limit % preflight.premium_tick != 0:
        return "INVALID_PREMIUM_TICK"
    if quantity > ceilings.contracts_per_signal:
        return "QUANTITY_CAP_EXCEEDED"
    notional = limit * Decimal(quantity) * 100
    if notional > ceilings.premium_per_signal_usd:
        return "SIGNAL_PREMIUM_CAP_EXCEEDED"
    if active_signals >= ceilings.open_signal_count:
        return "OPEN_SIGNAL_CAP_EXCEEDED"
    if outstanding_usd + notional > ceilings.total_exposure_usd:
        return "TOTAL_EXPOSURE_CAP_EXCEEDED"
    if not preflight.available_buying_power_usd.is_finite() or (
        preflight.available_buying_power_usd - outstanding_usd < notional
    ):
        return "INSUFFICIENT_BUYING_POWER"
    return None
