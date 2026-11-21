"""Explicit preflight policy for synthetic PAPER execution; deny by default."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from swift_autotrader.risk_limits import (
    DAILY_NET_LOSS_LIMIT_USD,
    MAX_CONTRACTS_PER_ALERT,
    PREMIUM_CEILING_USD,
)
from swift_autotrader.signals.identity import OptionContract


def reject_price_chasing(env: Mapping[str, str]) -> None:
    """Entries use the exact alert premium; a slippage allowance is not configurable."""
    raw = env.get("SLIPPAGE_TOLERANCE_PCT", "").strip()
    if not raw:
        return
    try:
        value = Decimal(raw)
    except ArithmeticError as exc:
        raise ValueError("invalid paper risk environment") from exc
    if not value.is_finite() or value != 0:
        raise ValueError(
            "SLIPPAGE_TOLERANCE_PCT is unsupported: entries use the exact alert limit"
        )


def _optional_decimal(env: Mapping[str, str], name: str) -> Decimal | None:
    raw = env.get(name, "").strip()
    return Decimal(raw) if raw else None


def _optional_int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name, "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True, slots=True)
class RiskCeilings:
    contracts_per_signal: int = MAX_CONTRACTS_PER_ALERT
    premium_per_signal_usd: Decimal = Decimal("0")
    # None = no cap: the number of trades is bounded only by buying power. An
    # operator may still set an optional voluntary cap; the code imposes none.
    total_exposure_usd: Decimal | None = None
    open_signal_count: int | None = None
    entry_max_age_seconds: int = 120
    # Realized-loss circuit breaker. Unlike the dollar ceilings above this is a
    # restriction, not a buying allowance, so its default is the approved limit.
    daily_net_loss_usd: Decimal | None = DAILY_NET_LOSS_LIMIT_USD

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> RiskCeilings:
        """Strict optional risk environment values; zero-dollar default blocks buys."""
        env = os.environ if values is None else values
        reject_price_chasing(env)
        try:
            ceilings = cls(
                contracts_per_signal=int(
                    env.get("MAX_CONTRACTS_PER_SIGNAL", str(MAX_CONTRACTS_PER_ALERT))
                ),
                premium_per_signal_usd=Decimal(env.get("MAX_PREMIUM_PER_SIGNAL_USD", "0")),
                total_exposure_usd=_optional_decimal(env, "PAPER_MAX_TOTAL_EXPOSURE_USD"),
                open_signal_count=_optional_int(env, "PAPER_MAX_OPEN_SIGNALS"),
                entry_max_age_seconds=int(env.get("PAPER_ENTRY_MAX_AGE_SECONDS", "120")),
                daily_net_loss_usd=Decimal(
                    env.get("PAPER_MAX_DAILY_NET_LOSS_USD", str(DAILY_NET_LOSS_LIMIT_USD))
                ),
            )
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("invalid paper risk environment") from exc
        # Every runtime path builds its ceilings here, so the approved maxima are
        # enforced for real configuration. Synthetic test rigs may still construct
        # RiskCeilings directly with larger quantities to exercise partial fills.
        return ceilings.require_approved_limits()

    def require_approved_limits(self) -> RiskCeilings:
        """Fail closed if any ceiling exceeds the operator-approved hard maxima."""
        if self.contracts_per_signal > MAX_CONTRACTS_PER_ALERT:
            raise ValueError("contracts per alert exceeds approved maximum")
        if self.premium_per_signal_usd > PREMIUM_CEILING_USD:
            raise ValueError("premium ceiling exceeds approved maximum")
        return self

    def __post_init__(self) -> None:
        if not 1 <= self.contracts_per_signal <= 100:
            raise ValueError("invalid quantity ceiling")
        if self.open_signal_count is not None and not 1 <= self.open_signal_count <= 1000:
            raise ValueError("invalid active signal ceiling")
        if not 0 <= self.entry_max_age_seconds <= 600:
            raise ValueError("invalid entry freshness ceiling")
        dollars = [self.premium_per_signal_usd]
        if self.total_exposure_usd is not None:
            dollars.append(self.total_exposure_usd)
        if any(not x.is_finite() or x < 0 for x in dollars):
            raise ValueError("invalid dollar ceilings")
        # The loss circuit breaker is a restriction, so it is bounded everywhere.
        if self.daily_net_loss_usd is not None and (not self.daily_net_loss_usd.is_finite() or not (
            0 < self.daily_net_loss_usd <= DAILY_NET_LOSS_LIMIT_USD
        )):
            raise ValueError("daily net loss ceiling outside approved range")


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
    realized_net_pnl_today_usd: Decimal | None = Decimal(0),
) -> str | None:
    """Check supplied candidate limit and evidence without submitting any order.

    Never inspects the current option price: the exact Swift limit survives unchanged.
    """
    if (not isinstance(limit, Decimal) or not limit.is_finite() or limit <= 0
            or type(quantity) is not int or quantity < 1):
        return "INVALID_ORDER_INPUT"
    if (not isinstance(outstanding_usd, Decimal) or not outstanding_usd.is_finite()
            or outstanding_usd < 0 or type(active_signals) is not int or active_signals < 0):
        return "INVALID_RISK_EVIDENCE"
    # PAPER retains its daily circuit breaker. LIVE explicitly sets this to None.
    if ceilings.daily_net_loss_usd is not None:
        if (not isinstance(realized_net_pnl_today_usd, Decimal)
                or not realized_net_pnl_today_usd.is_finite()):
            return "DAILY_PNL_UNVERIFIED"
        if realized_net_pnl_today_usd <= -ceilings.daily_net_loss_usd:
            return "DAILY_LOSS_CEILING_REACHED"
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
    if ceilings.open_signal_count is not None and active_signals >= ceilings.open_signal_count:
        return "OPEN_SIGNAL_CAP_EXCEEDED"
    if (
        ceilings.total_exposure_usd is not None
        and outstanding_usd + notional > ceilings.total_exposure_usd
    ):
        return "TOTAL_EXPOSURE_CAP_EXCEEDED"
    # With no trade-count or exposure cap, buying power is the only bound.
    if not preflight.available_buying_power_usd.is_finite() or (
        preflight.available_buying_power_usd - outstanding_usd < notional
    ):
        return "INSUFFICIENT_BUYING_POWER"
    return None
