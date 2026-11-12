"""Fail-closed Phase 0 configuration; no secret values are loaded or logged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Invalid or unsafe program configuration."""


def _bool(name: str, value: str) -> bool:
    if value.lower() not in ("true", "false"):
        raise ConfigurationError(f"{name} must be the literal true or false")
    return value.lower() == "true"


@dataclass(frozen=True, slots=True)
class Settings:
    paper_only: bool = True
    live_enabled: bool = False
    allow_broker_writes: bool = False
    broker_backend: str = "paper"
    telegram_enabled: bool = False
    db_path: Path = Path("data/swift_autotrader.sqlite3")
    max_contracts_per_signal: int = 1
    max_premium_per_signal_usd: Decimal = Decimal("0")
    entry_timeout_seconds: int = 3600
    log_level: str = "INFO"

    def validate(self) -> Settings:
        # Even setting ALL three flags by hand cannot activate a live broker in Phase 0.
        if not self.paper_only or self.live_enabled or self.allow_broker_writes:
            raise ConfigurationError("Phase 0 enforces PAPER_ONLY=true and disables all live writes")
        if self.broker_backend != "paper":
            raise ConfigurationError("Only BROKER_BACKEND=paper is supported in Phase 0")
        if self.telegram_enabled:
            raise ConfigurationError("TELEGRAM_ENABLED must be false until Phase 1")
        if not 1 <= self.max_contracts_per_signal <= 100:
            raise ConfigurationError("MAX_CONTRACTS_PER_SIGNAL must be between 1 and 100")
        if not self.max_premium_per_signal_usd.is_finite():
            raise ConfigurationError("MAX_PREMIUM_PER_SIGNAL_USD must be finite")
        if self.max_premium_per_signal_usd < 0:
            raise ConfigurationError("MAX_PREMIUM_PER_SIGNAL_USD cannot be negative")
        if self.entry_timeout_seconds != 3600:
            raise ConfigurationError("ENTRY_TIMEOUT_SECONDS is fixed at 3600")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("Unsupported LOG_LEVEL")
        if not str(self.db_path).strip():
            raise ConfigurationError("DB_PATH cannot be blank")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        data = os.environ if env is None else env
        try:
            contracts = int(data.get("MAX_CONTRACTS_PER_SIGNAL", "1"))
            premium = Decimal(data.get("MAX_PREMIUM_PER_SIGNAL_USD", "0"))
            timeout = int(data.get("ENTRY_TIMEOUT_SECONDS", "3600"))
        except (ValueError, InvalidOperation) as exc:
            raise ConfigurationError("Invalid numeric configuration") from exc
        return cls(
            paper_only=_bool("PAPER_ONLY", data.get("PAPER_ONLY", "true")),
            live_enabled=_bool("LIVE_ENABLED", data.get("LIVE_ENABLED", "false")),
            allow_broker_writes=_bool(
                "ALLOW_BROKER_WRITES", data.get("ALLOW_BROKER_WRITES", "false")
            ),
            broker_backend=data.get("BROKER_BACKEND", "paper").lower(),
            telegram_enabled=_bool(
                "TELEGRAM_ENABLED", data.get("TELEGRAM_ENABLED", "false")
            ),
            db_path=Path(data.get("DB_PATH", "data/swift_autotrader.sqlite3")),
            max_contracts_per_signal=contracts,
            max_premium_per_signal_usd=premium,
            entry_timeout_seconds=timeout,
            log_level=data.get("LOG_LEVEL", "INFO").upper(),
        ).validate()

    def public_summary(self) -> dict[str, str | int | bool]:
        # Deliberately exclude paths and future secret-bearing configuration.
        return {
            "paper_only": self.paper_only,
            "live_enabled": self.live_enabled,
            "allow_broker_writes": self.allow_broker_writes,
            "broker_backend": self.broker_backend,
            "telegram_enabled": self.telegram_enabled,
            "max_contracts_per_signal": self.max_contracts_per_signal,
            "max_premium_per_signal_usd": str(self.max_premium_per_signal_usd),
            "entry_timeout_seconds": self.entry_timeout_seconds,
        }
