"""Explicit opt-in local Windows PAPER dispatcher using official read-only MCP.

No login UI in unattended service. Tokens must already exist in a user-scoped
DPAPI store and OAuth may only refresh an existing authorization. Any absent
market field or broker review leaves orders unexecuted and raises a local notice.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from swift_autotrader.broker.live_broker import LiveBroker, engine_kill_switch, entry_signal_gate
from swift_autotrader.broker.mcp_local_auth import (
    LocalAuthNotConfigured,
    WindowsDPAPITokenStorage,
    load_pinned_agentic_account,
)
from swift_autotrader.broker.mcp_paper_data import ExchangeCalendarsSession, MCPReadOnlyOptionData
from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker
from swift_autotrader.config import Settings
from swift_autotrader.live_runtime import (
    BorrowedMCPLoop,
    LiveMCPSession,
    ReadOnlyClient,
    build_broker,
    rehearse_entries,
    startup_self_check,
)
from swift_autotrader.orders.risk import RiskCeilings
from swift_autotrader.paper_dispatch import PaperDispatcher, supervise_dispatch
from swift_autotrader.windows_runtime import validate_install_root


async def _forbid_interactive_authorization(*_args, **_kwargs):
    raise LocalAuthNotConfigured("Unattended OAuth cannot initiate a browser or new approval")


def token_file(root: Path) -> Path:
    """Accept only a managed, private secrets directory; never a repository path."""
    managed = validate_install_root(root, allowed_modes=("PAPER_ONLY",))
    destination = (managed / "secrets" / "oauth.dpapi").resolve()
    if destination.parent != (managed / "secrets").resolve():
        raise LocalAuthNotConfigured("Untrusted MCP token location")
    return destination


async def run_paper_service(config: Settings, root: Path) -> None:
    """Required local account/market authorization is separate from source development."""
    config.validate()
    if os.name != "nt" or os.environ.get("CI"):
        raise LocalAuthNotConfigured("Unattended read-only MCP requires approved Windows user")
    if os.environ.get("PAPER_DISPATCH_ENABLED") != "true":
        raise LocalAuthNotConfigured("PAPER dispatcher is not manually enabled")
    if os.environ.get("PAPER_MARKET_DATA_SOURCE") != "robinhood_mcp_readonly":
        raise LocalAuthNotConfigured("Unsupported or unverified market data source")
    # Source session runs in its own supervised read-only child. This worker is
    # intentionally not given Telegram authentication secrets.
    if config.telegram_channel_id is None:
        raise LocalAuthNotConfigured("Designated authorized source channel ID is required")
    if config.db_path.resolve() != (root.resolve() / "data" / "swift.sqlite3").resolve():
        raise LocalAuthNotConfigured("PAPER dispatcher must share managed SQLite file")
    path = token_file(root)
    if not path.is_file():
        raise LocalAuthNotConfigured("Enroll Robinhood read-only OAuth locally before enabling")
    approved_account = load_pinned_agentic_account(root)
    enrollment = OAuthEnrollment(
        WindowsDPAPITokenStorage(path, install_root=root),
        _forbid_interactive_authorization,
        _forbid_interactive_authorization,
        redirect_uri="http://127.0.0.1:8766/callback",
    )
    async with authenticated_mcp_client(enrollment) as client:
        provider = MCPReadOnlyOptionData(
            RobinhoodMCPBroker(client, approved_account_number=approved_account),
            calendar=ExchangeCalendarsSession(),
            contracts_per_signal=RiskCeilings.from_environment().contracts_per_signal,
        )
        broker = None
        try:
            if config.live_mode:
                # Reuse the already-authenticated read-only provider's MCP client.
                # A separate session sharing the same token file could race OAuth
                # refresh. Blocking Broker calls run via asyncio.to_thread from
                # PaperDispatcher, and are dispatched back to this live loop.
                broker = build_broker(
                    config, config.db_path, clock=lambda: datetime.now(UTC),
                    client=client, loop=BorrowedMCPLoop(asyncio.get_running_loop()))
            # Prints MODE/account ending; refuses a live start if any check fails.
            await asyncio.to_thread(startup_self_check, config, config.db_path, broker)
            dispatcher = PaperDispatcher(
                config.db_path, config.telegram_channel_id, provider,
                limits=RiskCeilings.from_environment(), broker=broker,
            )
            await supervise_dispatch(dispatcher, interval_seconds=5)
        finally:
            if broker is not None and hasattr(broker, "close"):
                broker.close()


def run_live_rehearsal(config: Settings, root: Path, *, limit: int = 50) -> list[dict[str, object]]:
    """Connect to the real MCP with writes forced OFF and rehearse every recorded ENTRY.

    Uses the already-enrolled token (no browser, no new approval). The client is wrapped
    so place_option_order / cancel_option_order can never be called, and the broker is
    built with rehearsal=True. Results are logged to logs/live_rehearsal.jsonl.
    """
    config.validate()
    if os.name != "nt" or os.environ.get("CI"):
        raise LocalAuthNotConfigured("Rehearsal requires the approved Windows user session")
    if os.environ.get("LIVE_REHEARSAL") != "true":
        raise LocalAuthNotConfigured("Set LIVE_REHEARSAL=true to run a live rehearsal")
    if not config.live_account_number.strip() or config.live_journal_path is None:
        raise LocalAuthNotConfigured("LIVE_ACCOUNT_NUMBER and LIVE_JOURNAL_PATH are required")
    if config.db_path.resolve() != (root.resolve() / "data" / "swift.sqlite3").resolve():
        raise LocalAuthNotConfigured("Rehearsal must read the managed SQLite file")
    path = token_file(root)
    if not path.is_file():
        raise LocalAuthNotConfigured("Enroll Robinhood OAuth locally before rehearsing")
    enrollment = OAuthEnrollment(
        WindowsDPAPITokenStorage(path), _forbid_interactive_authorization,
        _forbid_interactive_authorization, redirect_uri="http://127.0.0.1:8766/callback",
    )
    session = LiveMCPSession(enrollment)
    try:
        broker = LiveBroker(
            config.live_journal_path, ReadOnlyClient(session.start()),
            account_number=config.live_account_number, rehearsal=True,
            kill_switch=engine_kill_switch(config.db_path),
            signal_gate=entry_signal_gate(config.db_path), loop=session.loop)
        broker.verify_account()
        return rehearse_entries(broker, config.db_path, limit=limit,
                                log_path=root / "logs" / "live_rehearsal.jsonl")
    finally:
        session.close()
