"""Fail-closed paper-only settings. Secret fields excluded from representations."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ConfigurationError(ValueError):
    """Invalid or unsafe program configuration."""


def _bool(name: str, value: str) -> bool:
    if value.lower() not in ("true", "false"):
        raise ConfigurationError(f"{name} must be true or false")
    return value.lower() == "true"


def _optional_int(data: Mapping[str, str], name: str) -> int | None:
    raw = data.get(name, "")
    return int(raw) if raw.strip() else None


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
    telegram_channel_id: int | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str = field(default="", repr=False)
    telegram_session_path: Path | None = field(default=None, repr=False)
    telegram_queue_capacity: int = 512
    telegram_max_live_age_seconds: int = 120
    telegram_reconnect_attempts: int = 5

    def validate(self) -> Settings:
        if not self.paper_only or self.live_enabled or self.allow_broker_writes:
            raise ConfigurationError("Paper-only mode is mandatory; broker writes are forbidden")
        if self.broker_backend != "paper":
            raise ConfigurationError("Only the paper broker backend is permitted")
        if not 1 <= self.max_contracts_per_signal <= 100:
            raise ConfigurationError("MAX_CONTRACTS_PER_SIGNAL must be between 1 and 100")
        if not self.max_premium_per_signal_usd.is_finite():
            raise ConfigurationError("MAX_PREMIUM_PER_SIGNAL_USD must be finite")
        if self.max_premium_per_signal_usd < 0:
            raise ConfigurationError("MAX_PREMIUM_PER_SIGNAL_USD cannot be negative")
        if self.entry_timeout_seconds != 3600:
            raise ConfigurationError("ENTRY_TIMEOUT_SECONDS must be 3600")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("Unsupported LOG_LEVEL")
        if not str(self.db_path).strip():
            raise ConfigurationError("DB_PATH cannot be blank")
        if not 1 <= self.telegram_queue_capacity <= 4096:
            raise ConfigurationError("TELEGRAM_QUEUE_CAPACITY outside safe range")
        if not 0 <= self.telegram_max_live_age_seconds <= 600:
            raise ConfigurationError("TELEGRAM_MAX_LIVE_AGE_SECONDS outside safe range")
        if not 1 <= self.telegram_reconnect_attempts <= 20:
            raise ConfigurationError("TELEGRAM_RECONNECT_ATTEMPTS outside safe range")
        if self.telegram_enabled:
            if self.telegram_channel_id is None or self.telegram_channel_id >= -1000000000000:
                raise ConfigurationError("Specify one valid negative Telegram channel peer ID")
            if self.telegram_api_id is None or self.telegram_api_id <= 0:
                raise ConfigurationError("Telegram user API ID is required locally")
            if re.fullmatch(r"[a-fA-F0-9]{32}", self.telegram_api_hash) is None:
                raise ConfigurationError("Telegram API hash is missing or malformed")
            if self.telegram_session_path is None or not self.telegram_session_path.is_absolute():
                raise ConfigurationError("Session path must be an absolute local path")
            path = self.telegram_session_path.resolve()
            if path.is_relative_to(Path.cwd().resolve()):
                raise ConfigurationError("Session path cannot be inside the checkout")
            if path.suffix != ".session" or not path.is_file():
                raise ConfigurationError("A pre-authorized local .session file is required")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        data = os.environ if env is None else env
        try:
            contracts = int(data.get("MAX_CONTRACTS_PER_SIGNAL", "1"))
            premium = Decimal(data.get("MAX_PREMIUM_PER_SIGNAL_USD", "0"))
            timeout = int(data.get("ENTRY_TIMEOUT_SECONDS", "3600"))
            channel = _optional_int(data, "SWIFT_TELEGRAM_CHANNEL_ID")
            api_id = _optional_int(data, "TELEGRAM_API_ID")
            capacity = int(data.get("TELEGRAM_QUEUE_CAPACITY", "512"))
            max_age = int(data.get("TELEGRAM_MAX_LIVE_AGE_SECONDS", "120"))
            reconnect = int(data.get("TELEGRAM_RECONNECT_ATTEMPTS", "5"))
        except (ValueError, InvalidOperation) as exc:
            raise ConfigurationError("Invalid numeric configuration") from exc
        session = data.get("TELEGRAM_SESSION_PATH", "").strip()
        return cls(
            paper_only=_bool("PAPER_ONLY", data.get("PAPER_ONLY", "true")),
            live_enabled=_bool("LIVE_ENABLED", data.get("LIVE_ENABLED", "false")),
            allow_broker_writes=_bool(
                "ALLOW_BROKER_WRITES", data.get("ALLOW_BROKER_WRITES", "false")
            ),
            broker_backend=data.get("BROKER_BACKEND", "paper").lower(),
            telegram_enabled=_bool("TELEGRAM_ENABLED", data.get("TELEGRAM_ENABLED", "false")),
            db_path=Path(data.get("DB_PATH", "data/swift_autotrader.sqlite3")),
            max_contracts_per_signal=contracts,
            max_premium_per_signal_usd=premium,
            entry_timeout_seconds=timeout,
            log_level=data.get("LOG_LEVEL", "INFO").upper(),
            telegram_channel_id=channel,
            telegram_api_id=api_id,
            telegram_api_hash=data.get("TELEGRAM_API_HASH", ""),
            telegram_session_path=Path(session) if session else None,
            telegram_queue_capacity=capacity,
            telegram_max_live_age_seconds=max_age,
            telegram_reconnect_attempts=reconnect,
        ).validate()

    def public_summary(self) -> dict[str, str | int | bool]:
        return {
            "paper_only": self.paper_only,
            "live_enabled": self.live_enabled,
            "allow_broker_writes": self.allow_broker_writes,
            "broker_backend": self.broker_backend,
            "telegram_enabled": self.telegram_enabled,
            "telegram_queue_capacity": self.telegram_queue_capacity,
            "telegram_max_live_age_seconds": self.telegram_max_live_age_seconds,
            "max_contracts_per_signal": self.max_contracts_per_signal,
            "max_premium_per_signal_usd": str(self.max_premium_per_signal_usd),
            "entry_timeout_seconds": self.entry_timeout_seconds,
        }
