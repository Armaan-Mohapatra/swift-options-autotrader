"""Owner activation gates for unattended LIVE execution.

Every function here is a local check or an explicit, owner-requested write. Nothing
enables trading, contacts the broker or places an order. The owner-run
``deployment/windows/activate-live.ps1`` calls these commands in order and only writes
the owner receipt after the owner types the account ending and a confirmation phrase.

Reports carry gate names and refusal reasons only: never a full account number, a
token, a Telegram chat ID or a credential value.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from swift_autotrader.config import Settings
from swift_autotrader.orders.unattended_approval import (
    RECEIPT_NAME,
    UnattendedApprovalError,
    load_unattended_approval,
)
from swift_autotrader.risk_limits import (
    DAILY_NET_LOSS_LIMIT_USD,
    PREMIUM_CEILING_USD,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "database" / "migrations"
CANDIDATE_REQUIRED = frozenset({
    "mode", "live_enabled", "broker_writes_enabled", "max_contracts_per_signal",
    "max_premium_per_signal_usd", "max_daily_loss_usd", "live_journal_path",
    "robinhood_mcp_account", "telegram_enabled", "telegram_channel_id", "telegram_api_id",
})
CANDIDATE_OPTIONAL = frozenset({"log_level", "kill_switch_chat_id"})
CANDIDATE_KEYS = CANDIDATE_REQUIRED | CANDIDATE_OPTIONAL
MAX_RECEIPT_DAYS = 7
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"gate": self.name, "passed": self.passed, "reason": self.reason}


def _gate(name: str, ok: bool, reason: str) -> Gate:
    return Gate(name, bool(ok), reason)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_candidate(path: Path) -> dict[str, Any]:
    """Read the staged LIVE candidate. Small, UTF-8 (BOM tolerated), a JSON object."""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("live candidate file is missing or redirected")
    raw = target.read_bytes()
    if len(raw) > 16 * 1024:
        raise ValueError("live candidate file is too large")
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("live candidate is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("live candidate must be a JSON object")
    return document


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _root_paths(root: Path) -> dict[str, Path]:
    base = Path(root)
    return {
        "manifest": base / "deployment.json",
        "settings": base / "config" / "settings.json",
        "control_db": base / "data" / "live.sqlite3",
        "receipt": base / "secrets" / RECEIPT_NAME,
        "stop_reviewed": base / "state" / "SWIFT_REVIEWED_STOP",
    }


def check_candidate(candidate: Mapping[str, Any], *, root: Path) -> list[Gate]:
    """Mirror the LIVE rules of Settings.validate() plus the owner's fixed limits."""
    gates: list[Gate] = []
    keys = set(candidate)
    unknown = sorted(keys - CANDIDATE_KEYS)
    missing = sorted(CANDIDATE_REQUIRED - keys)
    gates.append(_gate(
        "candidate_fields", not unknown and not missing,
        "all fields recognised and present" if not unknown and not missing else
        f"unknown fields {unknown or '-'}; missing fields {missing or '-'}",
    ))
    gates.append(_gate(
        "candidate_live_flags",
        candidate.get("mode") == "LIVE" and candidate.get("live_enabled") is True
        and candidate.get("broker_writes_enabled") is True and candidate.get("telegram_enabled") is True,
        "mode LIVE, live_enabled, broker_writes_enabled and telegram_enabled are true",
    ))
    contracts = candidate.get("max_contracts_per_signal")
    gates.append(_gate(
        "candidate_contracts",
        type(contracts) is int and contracts == 1,
        "exactly one long contract per eligible entry" if type(contracts) is int and contracts == 1
        else "max_contracts_per_signal must be exactly 1",
    ))
    premium = _decimal(candidate.get("max_premium_per_signal_usd"))
    gates.append(_gate(
        "candidate_premium_cap",
        premium is not None and 0 < premium <= PREMIUM_CEILING_USD,
        f"premium ceiling within (0, {PREMIUM_CEILING_USD}]" if premium is not None and 0 < premium <= PREMIUM_CEILING_USD
        else f"max_premium_per_signal_usd must be a positive amount not above {PREMIUM_CEILING_USD}",
    ))
    loss = _decimal(candidate.get("max_daily_loss_usd"))
    gates.append(_gate(
        "candidate_daily_loss_cap",
        loss is not None and 0 < loss <= DAILY_NET_LOSS_LIMIT_USD,
        f"daily loss breaker within (0, {DAILY_NET_LOSS_LIMIT_USD}]" if loss is not None and 0 < loss <= DAILY_NET_LOSS_LIMIT_USD
        else f"max_daily_loss_usd must be a positive amount not above {DAILY_NET_LOSS_LIMIT_USD}",
    ))
    journal_raw = candidate.get("live_journal_path")
    journal_ok = False
    if isinstance(journal_raw, str) and Path(journal_raw).is_absolute():
        journal = Path(journal_raw)
        managed_data = (Path(root) / "data").resolve()
        journal_ok = journal.resolve().is_relative_to(managed_data) and not journal.is_symlink()
    gates.append(_gate(
        "candidate_journal_path", journal_ok,
        "journal is an absolute path inside the managed data directory" if journal_ok
        else "live_journal_path must be an absolute, non-linked path inside the managed data directory",
    ))
    account = candidate.get("robinhood_mcp_account")
    gates.append(_gate(
        "candidate_account_present",
        isinstance(account, str) and len(account.strip()) >= 3,
        "dedicated account is configured (value not shown)" if isinstance(account, str) and len(account.strip()) >= 3
        else "robinhood_mcp_account must be the dedicated account number",
    ))
    channel = candidate.get("telegram_channel_id")
    api_id = candidate.get("telegram_api_id")
    gates.append(_gate(
        "candidate_telegram_ids",
        type(channel) is int and channel < -1000000000000 and type(api_id) is int and api_id > 0,
        "source channel and API ID are well-formed (values not shown)"
        if type(channel) is int and channel < -1000000000000 and type(api_id) is int and api_id > 0
        else "telegram_channel_id must be a negative peer ID and telegram_api_id a positive integer",
    ))
    level = candidate.get("log_level", "INFO")
    kill_chat = candidate.get("kill_switch_chat_id")
    kill_ok = kill_chat is None or (type(kill_chat) is int and kill_chat != channel)
    gates.append(_gate(
        "candidate_optional_fields",
        level in _LOG_LEVELS and kill_ok,
        "log level and kill-switch chat are valid" if level in _LOG_LEVELS and kill_ok
        else "log_level must be a known level; kill_switch_chat_id must differ from the source channel",
    ))
    return gates


def check_manifest(root: Path, expected_mode: str) -> Gate:
    try:
        document = json.loads(_root_paths(root)["manifest"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _gate("manifest", False, "deployment.json is missing or unreadable")
    if document.get("product") != "swift-options-autotrader":
        return _gate("manifest", False, "deployment.json is not the Swift manifest")
    if expected_mode == "PAPER_ONLY":
        ok = (document.get("mode") == "PAPER_ONLY" and document.get("live_enabled") is False
              and document.get("broker_writes_enabled") is False)
    else:
        ok = (document.get("mode") == "LIVE" and document.get("live_enabled") is True
              and document.get("broker_writes_enabled") is True)
    return _gate("manifest", ok, f"manifest is {expected_mode}" if ok
                 else f"deployment.json is not in the expected {expected_mode} state")


def check_settings_file(root: Path, candidate: Mapping[str, Any], *, live: bool) -> Gate:
    """Pre: settings untouched by live. Post: every LIVE field equals the reviewed candidate."""
    path = _root_paths(root)["settings"]
    try:
        current = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return _gate("settings_file", False, "config/settings.json is missing or unreadable")
    if not live:
        return _gate("settings_file", current.get("mode") == "PAPER_ONLY",
                     "settings.json is still PAPER_ONLY" if current.get("mode") == "PAPER_ONLY"
                     else "settings.json is not PAPER_ONLY before activation")
    mismatched = sorted(
        key for key in CANDIDATE_KEYS
        if key in candidate and current.get(key) != candidate.get(key)
    )
    return _gate("settings_file", not mismatched and current.get("mode") == "LIVE",
                 "settings.json carries the reviewed LIVE fields" if not mismatched and current.get("mode") == "LIVE"
                 else f"settings.json differs from the candidate in {mismatched or ['mode']}")


def settings_diff(root: Path, candidate: Mapping[str, Any]) -> list[str]:
    """Human-readable LIVE changes with account, chat and channel values masked."""
    try:
        current = json.loads(_root_paths(root)["settings"].read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        current = {}
    masked = {"robinhood_mcp_account", "telegram_channel_id", "kill_switch_chat_id"}
    lines: list[str] = []
    for key in sorted(CANDIDATE_KEYS):
        if key not in candidate:
            continue
        new = candidate[key]
        old = current.get(key)
        status = "unchanged" if old == new else "CHANGE"
        if key in masked:
            shown_old, shown_new = ("set" if old else "unset"), ("set" if new else "unset")
        else:
            shown_old, shown_new = repr(old), repr(new)
        lines.append(f"{status:9} {key}: {shown_old} -> {shown_new}")
    return lines


def check_journal(root: Path, candidate: Mapping[str, Any]) -> Gate:
    raw = candidate.get("live_journal_path")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        return _gate("live_journal", False, "journal path not configured")
    journal = Path(raw)
    if not journal.is_file() or journal.is_symlink():
        return _gate("live_journal", False, "journal file is missing (run live-journal-init)")
    if journal.resolve() == _root_paths(root)["control_db"].resolve():
        return _gate("live_journal", False, "journal is the control database; refusing")
    if journal.stat().st_size == 0:
        return _gate("live_journal", True, "empty journal present; schema is created on first live start")
    try:
        with closing(sqlite3.connect(f"{journal.as_uri()}?mode=ro", uri=True, timeout=5)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return _gate("live_journal", False, "journal integrity check failed")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return _gate("live_journal", False, "journal is not a readable SQLite database")
    if not {"live_orders", "live_risk_latches"} <= tables:
        return _gate("live_journal", False, "journal is not a Swift live order journal")
    return _gate("live_journal", True, "journal integrity ok and schema present")


def check_control_db(control_db: Path) -> list[Gate]:
    """Schema and migration currency of the LIVE control database, and the entry kill switch."""
    if not control_db.is_file() or control_db.is_symlink():
        return [_gate("control_db", False, "live control database is missing")]
    expected = {path.name for path in MIGRATIONS_DIR.glob("*.sql")}
    try:
        with closing(sqlite3.connect(f"{control_db.as_uri()}?mode=ro", uri=True, timeout=5)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return [_gate("control_db", False, "control database integrity check failed")]
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            control = db.execute(
                "SELECT paused, emergency_stop FROM paper_engine_control WHERE singleton=1"
            ).fetchone()
    except sqlite3.Error:
        return [_gate("control_db", False, "control database is not readable")]
    gates = [_gate("control_db_migrations", applied == expected,
                   f"{len(applied)} migrations applied and current" if applied == expected
                   else f"migrations differ: missing {sorted(expected - applied) or '-'}, "
                        f"unexpected {sorted(applied - expected) or '-'}")]
    if control is None:
        gates.append(_gate("kill_switch_readable", False, "entry control row missing"))
    else:
        state = "EMERGENCY" if control[1] else ("PAUSED" if control[0] else "clear")
        gates.append(_gate("kill_switch_readable", True, f"entry control readable: {state}"))
    return gates


def check_credentials(root: Path) -> Gate:
    secrets = Path(root) / "secrets"
    required = ("telegram.session", "telegram_api_hash.dpapi", "oauth.dpapi", "agentic_account.dpapi")
    missing = [name for name in required if not (secrets / name).is_file()]
    return _gate("credentials_present", not missing,
                 "Telegram session, Telegram hash, OAuth token and pinned account are present"
                 if not missing else f"missing local credential files: {missing}")


def check_pinned_account(root: Path, candidate: Mapping[str, Any]) -> Gate:
    """Local DPAPI read only (no network). The candidate must name the pinned account."""
    from swift_autotrader.broker.mcp_local_auth import (
        LocalAuthNotConfigured,
        load_pinned_agentic_account,
    )
    try:
        pinned = load_pinned_agentic_account(Path(root))
    except (LocalAuthNotConfigured, OSError, ValueError):
        return _gate("pinned_account", False, "pinned account cannot be read from local DPAPI storage")
    configured = candidate.get("robinhood_mcp_account")
    same = isinstance(configured, str) and hmac.compare_digest(configured.strip(), pinned.strip())
    return _gate("pinned_account", same,
                 "candidate account matches the locally pinned account" if same
                 else "candidate account differs from the locally pinned account")


def check_stop_markers(root: Path) -> Gate:
    stop = _root_paths(root)["stop_reviewed"]
    return _gate("persistent_stop", not stop.exists(),
                 "no persistent review stop marker" if not stop.exists()
                 else "SWIFT_REVIEWED_STOP is present; review and remove it before activation")


def check_layout(root: Path) -> Gate:
    from swift_autotrader.live_service import validate_live_install_root
    try:
        validate_live_install_root(Path(root))
    except (RuntimeError, OSError, ValueError) as exc:
        return _gate("managed_layout", False, str(exc) or "managed layout invalid")
    return _gate("managed_layout", True, "managed install layout is valid")


def candidate_settings(root: Path, candidate: Mapping[str, Any]) -> Settings:
    """Typed settings for receipt validation only. Not validated here; Settings.validate runs at start."""
    return Settings(
        paper_only=False,
        live_enabled=True,
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        telegram_enabled=True,
        db_path=Path(root) / "data" / "live.sqlite3",
        max_contracts_per_signal=int(candidate["max_contracts_per_signal"]),
        max_premium_per_signal_usd=Decimal(str(candidate["max_premium_per_signal_usd"])),
        max_daily_loss_usd=Decimal(str(candidate["max_daily_loss_usd"])),
        live_journal_path=Path(str(candidate["live_journal_path"])),
        robinhood_mcp_account=str(candidate["robinhood_mcp_account"]),
        telegram_channel_id=int(candidate["telegram_channel_id"]),
        telegram_api_id=int(candidate["telegram_api_id"]),
    )


def check_receipt(root: Path, candidate: Mapping[str, Any], now: datetime) -> Gate:
    path = _root_paths(root)["receipt"]
    if not path.is_file():
        return _gate("owner_receipt", False, "owner receipt has not been written")
    try:
        approval = load_unattended_approval(
            path, account=str(candidate["robinhood_mcp_account"]),
            settings=candidate_settings(root, candidate), now=now,
        )
    except UnattendedApprovalError as exc:
        return _gate("owner_receipt", False, str(exc))
    remaining = approval.expires_at - now
    return _gate("owner_receipt", True,
                 f"owner receipt valid; expires {approval.expires_at.isoformat()} "
                 f"({max(0, int(remaining.total_seconds() // 3600))} hours left)")


def build_receipt(account: str, candidate: Mapping[str, Any], now: datetime, days: int) -> dict[str, Any]:
    """Version-2 receipt capped at exactly the reviewed configured ceilings."""
    if type(days) is not int or not 1 <= days <= MAX_RECEIPT_DAYS:
        raise ValueError(f"receipt validity must be 1 to {MAX_RECEIPT_DAYS} days")
    if now.utcoffset() is None:
        raise ValueError("an offset-aware clock is required")
    return {
        "version": 2,
        "account_sha256": sha256_text(account),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=days)).isoformat(),
        "max_contracts_per_signal": int(candidate["max_contracts_per_signal"]),
        "max_premium_per_signal_usd": str(Decimal(str(candidate["max_premium_per_signal_usd"]))),
        "max_daily_loss_usd": str(Decimal(str(candidate["max_daily_loss_usd"]))),
    }


def account_ending_matches(account: str, typed: str) -> bool:
    """The owner must type the last four digits of the pinned account, which they reviewed."""
    digits = account.strip()
    return len(digits) >= 4 and re.fullmatch(r"\d{4}", typed.strip() or "") is not None \
        and hmac.compare_digest(digits[-4:], typed.strip())


def write_receipt(root: Path, document: Mapping[str, Any], candidate: Mapping[str, Any], now: datetime) -> Path:
    """Write the owner receipt atomically, then prove it loads. Refuses to overwrite."""
    path = _root_paths(root)["receipt"]
    if path.exists():
        raise FileExistsError("an owner receipt already exists; it is not overwritten")
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(dict(document), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        load_unattended_approval(path, account=str(candidate["robinhood_mcp_account"]),
                                 settings=candidate_settings(root, candidate), now=now)
    except UnattendedApprovalError:
        path.unlink(missing_ok=True)
        raise
    return path


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(dict(document), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def apply_live_config(root: Path, candidate: Mapping[str, Any]) -> dict[str, str]:
    """Flip settings.json and deployment.json to LIVE. Only from a valid PAPER_ONLY state."""
    paths = _root_paths(root)
    settings = json.loads(paths["settings"].read_text(encoding="utf-8-sig"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if settings.get("mode") != "PAPER_ONLY" or manifest.get("mode") != "PAPER_ONLY":
        raise ValueError("LIVE config can only be applied from a PAPER_ONLY installation")
    for key in CANDIDATE_KEYS:
        if key in candidate:
            settings[key] = candidate[key]
    for document in (settings, manifest):
        document.update(mode="LIVE", live_enabled=True, broker_writes_enabled=True)
    _atomic_write_json(paths["settings"], settings)
    _atomic_write_json(paths["manifest"], manifest)
    return {"status": "LIVE_CONFIG_WRITTEN"}


async def readonly_account_check(root: Path) -> dict[str, Any]:
    """Broker read-only check: the pinned account exists, is agentic and option-eligible.

    Uses the same non-interactive OAuth enrollment as service startup and calls only
    get_accounts. It cannot begin a browser login or place, change or cancel an order.
    """
    from swift_autotrader.broker.mcp_local_auth import (
        LocalAuthNotConfigured,
        WindowsDPAPITokenStorage,
        load_pinned_agentic_account,
    )
    from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
    from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker
    from swift_autotrader.live_service import token_file

    async def no_browser(*_args: object, **_kwargs: object) -> None:
        raise LocalAuthNotConfigured("account check cannot start interactive authorization")

    pinned = load_pinned_agentic_account(Path(root))
    enrollment = OAuthEnrollment(
        WindowsDPAPITokenStorage(token_file(Path(root)), install_root=Path(root)),
        no_browser, no_browser, redirect_uri="http://127.0.0.1:8766/callback",
    )
    async with authenticated_mcp_client(enrollment) as client:
        accounts = await RobinhoodMCPBroker(client, approved_account_number=pinned).accounts()
    matched = [account for account in accounts if account.account_number == pinned]
    if len(matched) != 1:
        return {"passed": False, "reason": "pinned account not returned by the broker"}
    if not matched[0].eligible:
        return {"passed": False, "reason": "pinned account is not option-eligible for agentic trading"}
    return {"passed": True, "reason": "pinned account verified read-only",
            "account_ending": pinned.strip()[-4:]}


def run_checks(root: Path, candidate_path: Path, *, phase: str, now: datetime) -> dict[str, Any]:
    """Read-only evaluation. phase 'pre' = before any LIVE change, 'post' = after the owner write."""
    if phase not in {"pre", "post"}:
        raise ValueError("phase must be pre or post")
    gates: list[Gate] = []
    try:
        candidate = load_candidate(candidate_path)
    except ValueError as exc:
        return {"passed": False, "phase": phase, "gates": [_gate("candidate", False, str(exc)).as_dict()]}
    gates.append(_gate("candidate", True, "candidate readable"))
    gates.append(check_layout(root))
    gates.extend(check_candidate(candidate, root=root))
    gates.append(check_manifest(root, "PAPER_ONLY" if phase == "pre" else "LIVE"))
    gates.append(check_settings_file(root, candidate, live=phase == "post"))
    gates.append(check_journal(root, candidate))
    gates.extend(check_control_db(_root_paths(root)["control_db"]))
    gates.append(check_credentials(root))
    gates.append(check_stop_markers(root))
    gates.append(check_pinned_account(root, candidate))
    if phase == "post":
        gates.append(check_receipt(root, candidate, now))
    return {"phase": phase, "passed": all(g.passed for g in gates),
            "gates": [g.as_dict() for g in gates]}
