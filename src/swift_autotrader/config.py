"""Fail-closed paper-only settings. Secret fields excluded from representations."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ConfigurationError(ValueError):
    """Invalid or unsafe program configuration."""


def validate_private_path(path: Path, *, install_root: Path | None = None,
                          managed_name: str) -> Path:
    """Keep credentials outside source checkouts; allow only the managed secret slot."""
    if not path.is_absolute():
        raise ConfigurationError("Credential path must be absolute")
    target = path.resolve()
    # Detect checkouts even when the process starts in another directory or a worktree.
    if any((parent / ".git").exists() for parent in (target.parent, *target.parent.parents)):
        raise ConfigurationError("Credential path cannot be inside the checkout")
    # Reject symbolic links and Windows junctions/reparse points, including parents.
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ConfigurationError("Credential path cannot be redirected")
    if install_root is not None:
        root = install_root.resolve()
        marker = root / "deployment.json"
        expected = root / "secrets" / managed_name
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Managed credential manifest is invalid") from exc
        if (not install_root.is_absolute() or marker.is_symlink() or
                not isinstance(manifest, dict) or
                manifest.get("product") != "swift-options-autotrader" or
                manifest.get("mode") != "PAPER_ONLY" or
                manifest.get("live_enabled") is not False or
                manifest.get("broker_writes_enabled") is not False or
                target != expected or not expected.parent.is_dir()):
            raise ConfigurationError("Credential path is not the managed private slot")
        return target
    if target.is_relative_to(Path.cwd().resolve()):
        raise ConfigurationError("Credential path cannot be inside the checkout")
    return target


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
    # Live execution and safety configuration
    robinhood_mcp_account: str = field(default="", repr=False)
    max_daily_loss_usd: Decimal = Decimal("750")
    max_concurrent_positions: int = 3
    slippage_tolerance_pct: Decimal = Decimal("10")
    kill_switch_enabled: bool = False
    kill_switch_telegram_bot_token: str = field(default="", repr=False)
    kill_switch_telegram_chat_id: int | None = None
    managed_install_root: Path | None = field(default=None, repr=False)

    @property
    def is_live(self) -> bool:
        return self.live_enabled and self.allow_broker_writes and not self.paper_only

    @property
    def mode(self) -> str:
        return "LIVE" if self.is_live else "PAPER_ONLY"

    def validate(self) -> Settings:
        if self.is_live:
            # LIVE MODE VALIDATION
            if self.broker_backend != "robinhood_mcp":
                raise ConfigurationError("Live mode requires broker_backend='robinhood_mcp'")
            if not self.robinhood_mcp_account or len(self.robinhood_mcp_account.strip()) < 3:
                raise ConfigurationError("Live mode requires a valid ROBINHOOD_MCP_ACCOUNT")
            if not self.max_daily_loss_usd.is_finite() or self.max_daily_loss_usd <= 0:
                raise ConfigurationError("MAX_DAILY_LOSS_USD must be positive and finite")
            if self.max_daily_loss_usd > Decimal("10000"):
                raise ConfigurationError("MAX_DAILY_LOSS_USD cannot exceed 10000 for safety")
            if not 1 <= self.max_concurrent_positions <= 10:
                raise ConfigurationError("MAX_CONCURRENT_POSITIONS must be between 1 and 10")
            if not Decimal("0") <= self.slippage_tolerance_pct <= Decimal("25"):
                raise ConfigurationError("SLIPPAGE_TOLERANCE_PCT must be between 0 and 25 percent")
            if not self.telegram_enabled:
                raise ConfigurationError("Live mode requires TELEGRAM_ENABLED=true for signal source")
        else:
            # PAPER MODE VALIDATION (original safety invariant)
            if not self.paper_only or self.live_enabled or self.allow_broker_writes:
                raise ConfigurationError("Paper-only mode is mandatory unless explicitly configured for live")
            if self.broker_backend != "paper":
                raise ConfigurationError("Only the paper broker backend is permitted in paper mode")
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
            path = validate_private_path(
                self.telegram_session_path, install_root=self.managed_install_root,
                managed_name="telegram.session",
            )
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
            daily_loss = Decimal(data.get("MAX_DAILY_LOSS_USD", "750"))
            max_concurrent = int(data.get("MAX_CONCURRENT_POSITIONS", "3"))
            slippage_pct = Decimal(data.get("SLIPPAGE_TOLERANCE_PCT", "10"))
            kill_chat_id = _optional_int(data, "KILL_SWITCH_TELEGRAM_CHAT_ID")
        except (ValueError, InvalidOperation) as exc:
            raise ConfigurationError("Invalid numeric configuration") from exc
        install_root = data.get("SWIFT_INSTALL_ROOT", "").strip()
        session = data.get("TELEGRAM_SESSION_PATH", "").strip()
        account = data.get("ROBINHOOD_MCP_ACCOUNT", "").strip()
        kill_bot_token = data.get("KILL_SWITCH_TELEGRAM_BOT_TOKEN", "").strip()
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
            robinhood_mcp_account=account,
            max_daily_loss_usd=daily_loss,
            max_concurrent_positions=max_concurrent,
            slippage_tolerance_pct=slippage_pct,
            kill_switch_enabled=_bool("KILL_SWITCH_ENABLED", data.get("KILL_SWITCH_ENABLED", "false")),
            kill_switch_telegram_bot_token=kill_bot_token,
            kill_switch_telegram_chat_id=kill_chat_id,
            managed_install_root=Path(install_root) if install_root else None,
        ).validate()

    def public_summary(self) -> dict[str, str | int | bool]:
        return {
            "mode": self.mode,
            "paper_only": self.paper_only,
            "live_enabled": self.live_enabled,
            "allow_broker_writes": self.allow_broker_writes,
            "broker_backend": self.broker_backend,
            "telegram_enabled": self.telegram_enabled,
            "telegram_queue_capacity": self.telegram_queue_capacity,
            "telegram_max_live_age_seconds": self.telegram_max_live_age_seconds,
            "max_contracts_per_signal": self.max_contracts_per_signal,
            "max_premium_per_signal_usd": str(self.max_premium_per_signal_usd),
            "max_daily_loss_usd": str(self.max_daily_loss_usd),
            "max_concurrent_positions": self.max_concurrent_positions,
            "slippage_tolerance_pct": str(self.slippage_tolerance_pct),
            "kill_switch_enabled": self.kill_switch_enabled,
            "entry_timeout_seconds": self.entry_timeout_seconds,
        }
