"""Standalone production LIVE options trading service for Swift AutoTrader.

Connects to Robinhood Agentic MCP under explicit owner authorization.
Ingests authentic signals from the designated Telegram channel, validates
options eligibility, exact-limit pricing, buying power, and risk limits, and
submits live buy/sell orders with durable reconciliation.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic_ns

from swift_autotrader.broker.mcp_local_auth import (
    LocalAuthNotConfigured,
    WindowsDPAPITokenStorage,
    load_pinned_agentic_account,
)
from swift_autotrader.broker.mcp_paper_data import (
    ExchangeCalendarsSession,
    MCPReadOnlyOptionData,
    SessionCalendar,
)
from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
from swift_autotrader.broker.robinhood_mcp import (
    RobinhoodMCPBroker,
)
from swift_autotrader.config import ConfigurationError, Settings, validate_private_path
from swift_autotrader.live_dispatch import LiveDispatcher, LiveDispatchResult
from swift_autotrader.orders.acceptance_scope import AcceptanceScope, load_acceptance_scope
from swift_autotrader.orders.exposure_state import has_active_exposure
from swift_autotrader.orders.unattended_approval import (
    RECEIPT_NAME,
    UnattendedApproval,
    UnattendedApprovalError,
    load_unattended_approval,
)

log = logging.getLogger("swift_autotrader.live_service")

# Owner-visible warning window before an unattended approval runs out.
EXPIRY_WARNING_WINDOW = timedelta(hours=24)


def validate_live_install_root(root: Path) -> Path:
    """Validate managed installation root for live execution."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("Swift install directory not present")
    marker = root / "deployment.json"
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError("Managed installation marker is missing")
    document = json.loads(marker.read_text(encoding="utf-8"))
    if document.get("product") != "swift-options-autotrader":
        raise RuntimeError("Unsafe or unknown installation manifest")
    for part in ("data", "logs", "backups", "secrets", "state", "releases"):
        location = root / part
        if location.is_symlink() or not location.is_dir():
            raise RuntimeError("Missing or redirected managed directory")
    return root


async def _forbid_interactive_authorization(*_args, **_kwargs):
    raise LocalAuthNotConfigured("Unattended live service cannot initiate browser authorization")


def token_file(root: Path) -> Path:
    """Return managed OAuth token storage path with directory validation."""
    managed = validate_live_install_root(root)
    destination = (managed / "secrets" / "oauth.dpapi").resolve()
    if destination.parent != (managed / "secrets").resolve():
        raise LocalAuthNotConfigured("Untrusted MCP token location")
    return destination


def write_stop_marker_durable(root: Path, reason: str = "STOP", filename: str = "stop.request") -> Path:
    """Durably persist stop marker using atomic write, flush, fsync and atomic replace.

    Fails closed by raising RuntimeError on any OSError instead of swallowing.
    """
    root = Path(root).resolve()
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / filename
    tmp = state_dir / f"{filename}.{os.getpid()}.{monotonic_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(reason)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        log.critical("Failed to durably write stop marker %s: %s", target, exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to durably persist stop marker {filename}: {exc}") from exc
    return target


def _has_active_orders_or_positions(db_path: Path) -> bool:
    """Check if there are any non-terminal orders or open positions in the database."""
    return has_active_exposure(db_path)


def write_live_health(
    root: Path,
    state: str,
    *,
    result: LiveDispatchResult | None = None,
    open_positions: int = 0,
    daily_loss: Decimal = Decimal("0"),
    simulated: bool = False,
    entries_allowed: bool = True,
) -> None:
    """Record cycle evidence, not a certificate of trading authorization."""
    if state == "RUNNING":
        if result is None:
            state = "STARTING"
        elif result.status != "LIVE_CYCLE_COMPLETED" or result.unresolved_exit_count:
            state = "DEGRADED"
    health_file = root / "state" / "live-health.json"
    health_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "status": state,
        "as_of_utc": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "mode": "LIVE",
        "live_enabled": True,
        "broker_writes_configured": True,
        "broker_writes_enabled": not simulated and state in {"RUNNING", "DEGRADED"},
        "simulation": simulated,
        "health_scope": "CYCLE_OUTCOME_ONLY",
        "live_certified": False,
        "last_dispatch_status": result.status if result is not None else None,
        "open_positions_count": result.open_positions_count if result else open_positions,
        "realized_daily_loss_usd": str(result.realized_daily_loss_usd if result else daily_loss),
        "last_cancellations": result.cancellations if result else 0,
        "last_entry_submissions": result.entry_orders_submitted if result else 0,
        "last_close_submissions": result.close_orders_submitted if result else 0,
        "unresolved_exit_count": result.unresolved_exit_count if result else None,
        "exit_entry_hold": bool(result.unresolved_exit_count) if result else None,
        # Receipt only. The owner's pause is separate: check EntriesStatus, not this field.
        "approval_allows_entries": entries_allowed,
    }
    tmp = health_file.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(health_file)
    finally:
        tmp.unlink(missing_ok=True)


def _approval_notices(center, approval: UnattendedApproval, now: datetime, notified: set[str]) -> None:
    """Make the receipt's expiry visible through the existing local notifications, once per code."""
    if approval.active(now):
        if approval.expires_at - now > EXPIRY_WARNING_WINDOW or "EXPIRING" in notified:
            return
        code, severity = "UNATTENDED_APPROVAL_EXPIRING", "WARNING"
    else:
        if "EXPIRED" in notified:
            return
        code, severity = "UNATTENDED_APPROVAL_EXPIRED", "ERROR"
    try:
        center.record("APPLICATION", code, source_key="unattended-approval",
                      severity=severity, at=now)
    except (OSError, sqlite3.Error, ValueError):
        log.warning("approval expiry notification unavailable")
        return
    notified.add("EXPIRED" if code.endswith("EXPIRED") else "EXPIRING")


async def run_live_service(
    config: Settings,
    root: Path,
    *,
    once: bool = False,
    single_entry: bool = False,
    authorized_symbol: str | None = None,
    target_message_id: int | None = None,
    interval_seconds: float = 5.0,
    stop_event: asyncio.Event | None = None,
    client_override=None,
    clock=None,
    calendar: SessionCalendar | None = None,
    acceptance_scope: AcceptanceScope | None = None,
    approval_override: UnattendedApproval | None = None,
) -> None:
    """Run production live execution dispatcher loop with complete safety validation.

    ``approval_override`` is a test seam only: production always loads the owner receipt.
    """
    config.validate()

    # Fail closed on non-live configuration
    if not config.is_live or not config.allow_broker_writes:
        raise ConfigurationError(
            "Live service requires LIVE_ENABLED=true and ALLOW_BROKER_WRITES=true"
        )
    if config.broker_backend != "robinhood_mcp":
        raise ConfigurationError("Live service requires BROKER_BACKEND='robinhood_mcp'")
    if config.telegram_channel_id is None:
        raise ConfigurationError("Live service requires a designated authorized Telegram channel ID")

    managed_root = validate_live_install_root(root)

    # Check persistent stop markers
    for marker in ("stop.request", "SWIFT_REVIEWED_STOP"):
        if (managed_root / "state" / marker).exists():
            write_live_health(managed_root, "STOPPED", simulated=client_override is not None)
            raise RuntimeError(f"Persistent stop marker active: {marker}")

    # Production is distinct from the paper installation. Never allow local
    # flags alone to turn a paper journal or credential store into a live one.
    # Mock-client tests exercise the service without reaching local credentials.
    approved_account = config.robinhood_mcp_account
    unattended_approval: UnattendedApproval | None = None
    if client_override is None:
        document = json.loads((managed_root / "deployment.json").read_text(encoding="utf-8"))
        if (document.get("mode") != "LIVE" or document.get("live_enabled") is not True
                or document.get("broker_writes_enabled") is not True):
            write_live_health(managed_root, "FAILED")
            raise LocalAuthNotConfigured("Managed installation is not approved for production execution")
        live_db = managed_root / "data" / "live.sqlite3"
        if (config.db_path.resolve() != live_db or config.db_path.is_symlink()
                or not live_db.is_file() or getattr(live_db.lstat(), "st_file_attributes", 0) & 0x400):
            write_live_health(managed_root, "FAILED")
            raise LocalAuthNotConfigured("A distinct managed production journal is required")
        pinned_account = load_pinned_agentic_account(managed_root)
        if approved_account != pinned_account:
            write_live_health(managed_root, "FAILED")
            raise LocalAuthNotConfigured("Configured account differs from local account binding")
        if single_entry:
            # Exact local operator receipt required; symbol-only CLI flags
            # do not authorize any real transaction. Never load from checkout.
            if once:
                raise LocalAuthNotConfigured("Single-entry lifecycle service cannot run with --once")
            receipt = validate_private_path(
                managed_root / "secrets" / "supervised-authorization.json",
                install_root=managed_root, managed_name="supervised-authorization.json",
                allowed_modes=("LIVE",),
            )
            acceptance_scope = load_acceptance_scope(receipt, account=pinned_account)
        else:
            # Unrestricted multi-entry execution needs a separate, expiring owner
            # receipt that caps every configured ceiling. Flags alone never suffice.
            try:
                approval_path = validate_private_path(
                    managed_root / "secrets" / RECEIPT_NAME,
                    install_root=managed_root, managed_name=RECEIPT_NAME,
                    allowed_modes=("LIVE",),
                )
                unattended_approval = load_unattended_approval(
                    approval_path, account=pinned_account, settings=config,
                    now=(clock or (lambda: datetime.now(UTC)))(),
                )
            except (ConfigurationError, UnattendedApprovalError) as exc:
                write_live_health(managed_root, "FAILED")
                raise LocalAuthNotConfigured(
                    f"Unattended live execution is not approved: {exc}"
                ) from exc
    if client_override is not None:
        unattended_approval = approval_override
    if acceptance_scope is not None and not single_entry:
        raise ConfigurationError("Acceptance scope requires single-entry mode")

    write_live_health(managed_root, "STARTING", simulated=client_override is not None)

    # Connect to Robinhood MCP
    if client_override is not None:
        client_ctx = client_override
    else:
        path = token_file(managed_root)
        if not path.is_file():
            raise LocalAuthNotConfigured("Robinhood OAuth tokens not found in managed secrets directory")
        enrollment = OAuthEnrollment(
            WindowsDPAPITokenStorage(path, install_root=managed_root),
            _forbid_interactive_authorization,
            _forbid_interactive_authorization,
            redirect_uri="http://127.0.0.1:8766/callback",
        )
        client_ctx = authenticated_mcp_client(enrollment)

    async with client_ctx as client:
        mcp_broker = RobinhoodMCPBroker(
            client,
            approved_account_number=approved_account,
        )

        # 1. Verify Robinhood accounts and permissions
        accounts = await mcp_broker.accounts()
        matched_account = next(
            (a for a in accounts if a.account_number == approved_account),
            None,
        )
        if matched_account is None:
            raise PermissionError("Configured account not found among broker accounts")
        if not matched_account.eligible:
            raise PermissionError(
                "Configured account is not eligible for options trading with external agents"
            )

        # 2. Build live market data provider and dispatcher
        provider = MCPReadOnlyOptionData(
            mcp_broker,
            calendar=calendar or ExchangeCalendarsSession(),
            contracts_per_signal=config.max_contracts_per_signal,
            clock=clock,
        )

        dispatcher = LiveDispatcher(
            config.db_path,
            config.telegram_channel_id,
            mcp_broker,
            matched_account,
            provider,
            config,
            clock=clock,
            single_entry=single_entry,
            authorized_symbol=authorized_symbol,
            target_message_id=target_message_id,
            acceptance_scope=acceptance_scope,
        )

        # The single canonical broker must itself confirm the pinned, agentic-enabled account
        # (read-only get_accounts) before any cycle can reach a write boundary.
        if not await asyncio.to_thread(dispatcher.live_broker.verify_account):
            raise PermissionError("Live broker could not verify the pinned dedicated account")

        write_live_health(managed_root, "STARTING", simulated=client_override is not None)
        log.info("Execution service initialized")
        last_result = None
        notified: set[str] = set()
        blocked_logged = False
        entries_allowed = True

        try:
            while True:
                # Check for stop markers before each cycle
                for marker in ("stop.request", "SWIFT_REVIEWED_STOP"):
                    if os.path.lexists(managed_root / "state" / marker):
                        log.info("Stop marker %s detected; shutting down execution service", marker)
                        break

                if stop_event is not None and stop_event.is_set():
                    log.info("Stop event signaled; shutting down live service")
                    break

                if any(os.path.lexists(managed_root / "state" / marker)
                       for marker in ("stop.request", "SWIFT_REVIEWED_STOP")):
                    break
                # Expiry blocks NEW entries only. The loop keeps running so exits, the
                # 60-minute cancellation backstop and P/L housekeeping still execute.
                now_utc = (clock or (lambda: datetime.now(UTC)))()
                entries_allowed = unattended_approval is None or unattended_approval.active(now_utc)
                if unattended_approval is not None:
                    _approval_notices(dispatcher.notifications, unattended_approval, now_utc, notified)
                if not entries_allowed and not blocked_logged:
                    log.warning("Unattended live approval expired: new entries blocked; exits and "
                                "cancellations continue until renewal or PauseEntries")
                    blocked_logged = True
                last_result = await dispatcher.run_once(entries_allowed=entries_allowed)
                write_live_health(managed_root, "RUNNING", result=last_result,
                                  simulated=client_override is not None,
                                  entries_allowed=entries_allowed)

                if single_entry:
                    import sqlite3
                    from contextlib import closing
                    with closing(sqlite3.connect(str(config.db_path))) as con:
                        buys_count = con.execute(
                            "SELECT count(*) FROM order_intents WHERE side='BUY_TO_OPEN'"
                        ).fetchone()[0]
                    if buys_count > 0:
                        has_active = _has_active_orders_or_positions(config.db_path)
                        if not has_active:
                            log.info("Supervised single-trade lifecycle completed (all positions settled); stopping")
                            write_stop_marker_durable(managed_root, "STOP")
                            break
                        elif once:
                            write_stop_marker_durable(managed_root, "STOP")
                            break

                if once:
                    break

                try:
                    await asyncio.wait_for(
                        stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                        timeout=interval_seconds,
                    )
                    if stop_event and stop_event.is_set():
                        break
                except TimeoutError:
                    pass
        except BaseException:
            write_live_health(managed_root, "FAILED", result=last_result,
                              simulated=client_override is not None,
                              entries_allowed=entries_allowed)
            raise
        else:
            write_live_health(managed_root, "STOPPED", result=last_result,
                              simulated=client_override is not None,
                              entries_allowed=entries_allowed)
            log.info("Execution service stopped")
