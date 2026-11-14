"""Explicit opt-in local Windows PAPER dispatcher using official read-only MCP.

No login UI in unattended service. Tokens must already exist in a user-scoped
DPAPI store and OAuth may only refresh an existing authorization. Any absent
market field or broker review leaves orders unexecuted and raises a local notice.
"""
from __future__ import annotations

import os
from pathlib import Path

from swift_autotrader.broker.mcp_local_auth import (
    LocalAuthNotConfigured,
    WindowsDPAPITokenStorage,
)
from swift_autotrader.broker.mcp_paper_data import ExchangeCalendarsSession, MCPReadOnlyOptionData
from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker
from swift_autotrader.config import Settings
from swift_autotrader.orders.risk import RiskCeilings
from swift_autotrader.paper_dispatch import PaperDispatcher, supervise_dispatch
from swift_autotrader.windows_runtime import validate_install_root


async def _forbid_interactive_authorization(*_args, **_kwargs):
    raise LocalAuthNotConfigured("Unattended OAuth cannot initiate a browser or new approval")


def token_file(root: Path) -> Path:
    """Accept only a managed, private secrets directory; never a repository path."""
    managed = validate_install_root(root)
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
    enrollment = OAuthEnrollment(
        WindowsDPAPITokenStorage(path),
        _forbid_interactive_authorization,
        _forbid_interactive_authorization,
        redirect_uri="http://127.0.0.1:8766/callback",
    )
    async with authenticated_mcp_client(enrollment) as client:
        provider = MCPReadOnlyOptionData(
            RobinhoodMCPBroker(client), calendar=ExchangeCalendarsSession(),
            contracts_per_signal=RiskCeilings.from_environment().contracts_per_signal,
        )
        dispatcher = PaperDispatcher(
            config.db_path, config.telegram_channel_id, provider,
            limits=RiskCeilings.from_environment(),
        )
        await supervise_dispatch(dispatcher, interval_seconds=5)
