"""Official MCP Python SDK v2 transport; requires explicit local OAuth enrollment.

No credential storage, implicit login, network or brokerage calls at import time.
The caller owns token storage and PKCE callbacks, and must use secure local storage.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

ROBINHOOD_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"


class MCPTransportUnavailable(RuntimeError):
    """SDK missing, OAuth unavailable, or unsupported transport configuration."""


def _endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "agent.robinhood.com"
        or parsed.port is not None
        or parsed.path != "/mcp/trading"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise MCPTransportUnavailable("Robinhood MCP endpoint must be official HTTPS URL")
    return ROBINHOOD_MCP_ENDPOINT


@dataclass(frozen=True, slots=True)
class OAuthEnrollment:
    """Explicit human-authorized local flow; no static passwords or token dumps."""

    token_storage: Any
    redirect_handler: Callable[[str], Awaitable[None]]
    callback_handler: Callable[[], Awaitable[Any]]
    redirect_uri: str = "http://127.0.0.1:8765/callback"

    def __post_init__(self) -> None:
        uri = urlparse(self.redirect_uri)
        if (uri.scheme != "http" or uri.hostname not in {"127.0.0.1", "localhost"}
                or not uri.path or uri.query or uri.fragment):
            raise ValueError("OAuth redirect must be loopback HTTP without query or fragment")
        if self.token_storage is None or not callable(self.redirect_handler) or not callable(
            self.callback_handler
        ):
            raise ValueError("OAuth requires injected secure storage and human callbacks")


def build_oauth_provider(enrollment: OAuthEnrollment, *, endpoint: str = ROBINHOOD_MCP_ENDPOINT):
    """Build official SDK OAuth+PKCE provider; no authorization request is started here."""
    url = _endpoint(endpoint)
    try:
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl
    except ImportError as exc:
        raise MCPTransportUnavailable("Install pinned official mcp==2.3.0 locally") from exc
    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Swift AutoTrader Read-Only Diagnostics",
            redirect_uris=[AnyUrl(enrollment.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=enrollment.token_storage,
        redirect_handler=enrollment.redirect_handler,
        callback_handler=enrollment.callback_handler,
    )


@asynccontextmanager
async def authenticated_mcp_client(
    enrollment: OAuthEnrollment, *, endpoint: str = ROBINHOOD_MCP_ENDPOINT,
    connect_timeout_seconds: float = 15.0, read_timeout_seconds: float = 60.0,
) -> AsyncIterator[Any]:
    """Open SDK Streamable HTTP client with injected PKCE OAuth; no writes here.

    OAuth may prompt for a browser-based human approval at the authorized host.
    No token storage is created; the caller chooses storage backed by OS secrets.
    """
    url = _endpoint(endpoint)
    if not 1 <= connect_timeout_seconds <= 60 or not 1 <= read_timeout_seconds <= 300:
        raise ValueError("MCP transport timeouts outside supported bounds")
    try:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise MCPTransportUnavailable("Install pinned official mcp==2.3.0 locally") from exc
    provider = build_oauth_provider(enrollment, endpoint=url)
    timeout = httpx2.Timeout(
        read=read_timeout_seconds, connect=connect_timeout_seconds,
        write=connect_timeout_seconds, pool=connect_timeout_seconds,
    )
    async with (
        httpx2.AsyncClient(auth=provider, timeout=timeout, trust_env=False) as http,
        streamable_http_client(url, http_client=http) as transport,
        Client(transport) as client,
    ):
        yield client
