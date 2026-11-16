"""Standalone production LIVE options trading service for Swift AutoTrader.

Connects to Robinhood Agentic MCP under explicit owner authorization.
Ingests authentic signals from the designated Telegram channel, validates
options eligibility, slippage ceiling, buying power, and risk limits, and
submits live buy/sell orders with durable reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from swift_autotrader.broker.mcp_local_auth import (
    LocalAuthNotConfigured,
    WindowsDPAPITokenStorage,
    load_pinned_agentic_account,
)
from swift_autotrader.broker.mcp_paper_data import ExchangeCalendarsSession, MCPReadOnlyOptionData
from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodLiveBroker,
    RobinhoodMCPBroker,
)
from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.live_dispatch import LiveDispatcher, LiveDispatchResult
from swift_autotrader.orders.risk import RiskCeilings

log = logging.getLogger("swift_autotrader.live_service")


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


def write_live_health(
    root: Path,
    state: str,
    *,
    result: LiveDispatchResult | None = None,
    open_positions: int = 0,
    daily_loss: Decimal = Decimal("0"),
) -> None:
    """Record atomic structured live service health heartbeat."""
    health_file = root / "state" / "live-health.json"
    health_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "status": state,
        "as_of_utc": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "mode": "LIVE",
        "live_enabled": True,
        "broker_writes_enabled": True,
        "open_positions_count": result.open_positions_count if result else open_positions,
        "realized_daily_loss_usd": str(result.realized_daily_loss_usd if result else daily_loss),
        "last_cancellations": result.cancellations if result else 0,
        "last_entry_submissions": result.entry_orders_submitted if result else 0,
        "last_close_submissions": result.close_orders_submitted if result else 0,
    }
    tmp = health_file.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(health_file)
    finally:
        tmp.unlink(missing_ok=True)


async def run_live_service(
    config: Settings,
    root: Path,
    *,
    once: bool = False,
    interval_seconds: float = 5.0,
    stop_event: asyncio.Event | None = None,
    client_override=None,
    clock=None,
) -> None:
    """Run production live execution dispatcher loop with complete safety validation."""
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
            write_live_health(managed_root, "STOPPED")
            raise RuntimeError(f"Persistent stop marker active: {marker}")

    # Resolve token storage
    approved_account = config.robinhood_mcp_account
    if not approved_account:
        approved_account = load_pinned_agentic_account(managed_root)

    write_live_health(managed_root, "STARTING")

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
            allow_live_writes=True,
        )

        # 1. Verify Robinhood accounts and permissions
        accounts = await mcp_broker.accounts()
        matched_account = next(
            (a for a in accounts if a.account_number == approved_account),
            None,
        )
        if matched_account is None:
            raise PermissionError(f"Approved account {approved_account} not found among broker accounts")
        if not matched_account.eligible:
            raise PermissionError(
                f"Account {approved_account} is not eligible for options trading with external agents"
            )

        # 2. Build live market data provider and dispatcher
        provider = MCPReadOnlyOptionData(
            mcp_broker,
            calendar=ExchangeCalendarsSession(),
            contracts_per_signal=config.max_contracts_per_signal,
        )

        dispatcher = LiveDispatcher(
            config.db_path,
            config.telegram_channel_id,
            mcp_broker,
            matched_account,
            provider,
            config,
            clock=clock,
        )

        write_live_health(managed_root, "RUNNING")
        log.info("Live trading service started for account %s", approved_account)

        try:
            while True:
                # Check for stop markers before each cycle
                for marker in ("stop.request", "SWIFT_REVIEWED_STOP"):
                    if (managed_root / "state" / marker).exists():
                        log.info("Stop marker %s detected; shutting down live service", marker)
                        write_live_health(managed_root, "STOPPED")
                        return

                if stop_event is not None and stop_event.is_set():
                    log.info("Stop event signaled; shutting down live service")
                    write_live_health(managed_root, "STOPPED")
                    return

                result = await dispatcher.run_once()
                write_live_health(managed_root, "RUNNING", result=result)

                if once:
                    break

                try:
                    await asyncio.wait_for(
                        stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                        timeout=interval_seconds,
                    )
                    if stop_event and stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass
        finally:
            write_live_health(managed_root, "STOPPED")
            log.info("Live trading service stopped cleanly")
