"""Opt-in local interactive OAuth, Windows DPAPI secret storage, no trading.

Never import or run in CI. Never prints OAuth URLs, codes, account IDs or tokens in logs.
Authentication and Robinhood's exact redirect behavior remain locally unverified.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from swift_autotrader.broker.mcp_transport import OAuthEnrollment, authenticated_mcp_client
from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker
from swift_autotrader.config import ConfigurationError, validate_private_path


class LocalAuthNotConfigured(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


class WindowsDPAPITokenStorage:
    """Official SDK TokenStorage shape; encrypt both token and registered client info.

    DPAPI user-scope ciphertext remains outside the repository. Requires pywin32,
    an interactive Windows user account and reviewed local filesystem ACLs.
    """

    def __init__(self, path: Path, *, install_root: Path | None = None):
        if not _is_windows():
            raise LocalAuthNotConfigured("Windows DPAPI is required")
        try:
            self.path = validate_private_path(path, install_root=install_root,
                                              managed_name="oauth.dpapi",
                                              allowed_modes=("PAPER_ONLY", "LIVE"))
        except ConfigurationError as exc:
            raise LocalAuthNotConfigured("OAuth data must live in a private, non-checkout location") from exc

    def _get(self, name: str):
        import win32crypt  # type: ignore[import-not-found]  # local Windows only
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        if not self.path.is_file():
            return None
        encrypted = self.path.read_bytes()
        try:
            clear = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]
            data = json.loads(clear)
            val = data.get(name)
            cls = OAuthToken if name == "tokens" else OAuthClientInformationFull
            return cls.model_validate(val) if val is not None else None
        except Exception as exc:
            raise LocalAuthNotConfigured("Unable to decrypt local OAuth data") from exc

    def _set(self, name: str, value: object) -> None:
        import win32crypt  # type: ignore[import-not-found]  # local Windows only

        if self.path.is_file():
            try:
                current = json.loads(win32crypt.CryptUnprotectData(
                    self.path.read_bytes(), None, None, None, 0,
                )[1])
            except Exception as exc:
                raise LocalAuthNotConfigured("Existing local OAuth data corrupt") from exc
        else:
            current = {}
        if not hasattr(value, "model_dump"):
            raise LocalAuthNotConfigured("OAuth credentials must be SDK models")
        current[name] = value.model_dump(mode="json")  # type: ignore[attr-defined]
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cipher = win32crypt.CryptProtectData(
            json.dumps(current).encode("utf-8"), None, None, None, None, 0,
        )
        if not isinstance(cipher, bytes) or not cipher:
            raise LocalAuthNotConfigured("Unexpected DPAPI encryption result")
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix="oauth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(cipher)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    async def get_tokens(self):
        return self._get("tokens")

    async def set_tokens(self, tokens) -> None:
        self._set("tokens", tokens)

    async def get_client_info(self):
        return self._get("client_info")

    async def set_client_info(self, client_info) -> None:
        self._set("client_info", client_info)


def load_pinned_agentic_account(root: Path) -> str:
    """Read only the operator-confirmed MCP account from the local DPAPI store."""
    if not _is_windows():
        raise LocalAuthNotConfigured("Windows DPAPI required for account binding")
    from swift_autotrader.windows_runtime import validate_install_root
    root = validate_install_root(Path(root), allowed_modes=("PAPER_ONLY", "LIVE"))
    import win32crypt  # type: ignore[import-not-found]

    path = root / "secrets" / "agentic_account.dpapi"
    if (path.is_symlink() or not path.is_file() or
            path.resolve().parent != (root / "secrets").resolve()):
        raise LocalAuthNotConfigured("Confirm the exclusive MCP account locally first")
    try:
        clear = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1]
        record = json.loads(clear)
        account = record["account_number"]
        if (record.get("version") != 1 or not isinstance(account, str)
                or not 4 <= len(account) <= 256):
            raise ValueError("Invalid account binding")
    except Exception as exc:
        raise LocalAuthNotConfigured("Exclusive MCP account binding invalid") from exc
    return account


def pin_agentic_account(root: Path, account_number: str) -> Path:
    """Encrypt and store operator-confirmed MCP account binding in local DPAPI store."""
    if not _is_windows():
        raise LocalAuthNotConfigured("Windows DPAPI required for account binding")
    account = account_number.strip()
    if not (4 <= len(account) <= 256):
        raise ValueError("Account number must be between 4 and 256 characters")
    from swift_autotrader.windows_runtime import validate_install_root
    root = validate_install_root(Path(root), allowed_modes=("PAPER_ONLY", "LIVE"))
    secrets_dir = (root / "secrets").resolve()
    target = (secrets_dir / "agentic_account.dpapi").resolve()
    if target.parent != secrets_dir:
        raise LocalAuthNotConfigured("Invalid secrets path")
    import win32crypt  # type: ignore[import-not-found]
    record = {"version": 1, "account_number": account}
    payload = json.dumps(record).encode("utf-8")
    cipher = win32crypt.CryptProtectData(
        payload,
        "SwiftAutoTrader Agentic Account Binding",
        None,
        None,
        None,
        0,
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_target = target.with_suffix(".tmp")
    temp_target.write_bytes(cipher)
    temp_target.replace(target)
    return target


class LoopbackAuthorization:
    """Start local callback BEFORE opening browser; do not log callback query."""

    def __init__(self, *, port: int = 8766):
        if not 1024 <= port <= 65535:
            raise ValueError("Unprivileged local callback port required")
        self.port = port
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[object] | None = None

    async def redirect(self, url: str) -> None:
        from mcp.client.auth import AuthorizationCodeResult
        if self._server is not None:
            raise LocalAuthNotConfigured("OAuth callback server already running")
        self._result = asyncio.get_running_loop().create_future()

        async def on_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                request = line.decode("ascii", errors="strict").split(" ")
                target = request[1] if len(request) >= 2 else ""
                parsed = urlsplit(target)
                if request[0] != "GET" or parsed.path != "/callback":
                    raise ValueError("Unexpected OAuth callback path")
                params = parse_qs(parsed.query)
                code = params["code"][0]
                state = params["state"][0]
                issuer = params.get("iss", [None])[0]
                assert self._result is not None
                if not self._result.done():
                    self._result.set_result(AuthorizationCodeResult(
                        code=code, state=state, iss=issuer,
                    ))
                body = b"Robinhood authentication callback received. Return to the application."
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             + b"Cache-Control: no-store\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            except Exception:
                body = b"Invalid authorization callback."
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n"
                             + b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
            finally:
                await writer.drain()
                writer.close()
                await writer.wait_closed()

        self._server = await asyncio.start_server(on_request, "127.0.0.1", self.port)
        if not webbrowser.open(url):
            await self.close()
            raise LocalAuthNotConfigured("Could not open local authorization browser")

    async def callback(self):
        if self._result is None:
            raise LocalAuthNotConfigured("OAuth callback not initialized")
        try:
            return await asyncio.wait_for(self._result, timeout=180)
        finally:
            await self.close()

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


async def local_readonly_diagnostics() -> dict[str, object]:
    """Only explicit, interactive, locally authorized Windows operation."""
    if os.name != "nt" or os.environ.get("CI") or (
        os.environ.get("ROBINHOOD_MCP_INTERACTIVE") != "true"
    ):
        raise LocalAuthNotConfigured("Windows interactive local authorization required")
    managed_root = os.environ.get("SWIFT_INSTALL_ROOT", "").strip()
    if not managed_root:
        raise LocalAuthNotConfigured("Approved installed Swift root is required for OAuth")
    from swift_autotrader.windows_runtime import validate_install_root

    root = validate_install_root(Path(managed_root))
    path = root / "secrets" / "oauth.dpapi"
    loopback = LoopbackAuthorization(port=8766)
    enrollment = OAuthEnrollment(
        WindowsDPAPITokenStorage(path, install_root=root), loopback.redirect, loopback.callback,
        redirect_uri="http://127.0.0.1:8766/callback",
    )
    async with authenticated_mcp_client(enrollment) as client:
        adapter = RobinhoodMCPBroker(client)
        diagnostic = await adapter.discover()
        # Privacy: do not print or persist account IDs and balances.
        accounts = await adapter.accounts()
        return {
            "status": diagnostic.status,
            "accessible_accounts": len(accounts),
            "agentic_accounts": sum(a.agentic_allowed for a in accounts),
            "option_approved_agentic_accounts": sum(a.eligible for a in accounts),
            "missing_tool_count": len(diagnostic.missing_tools),
            "schema_mismatch_count": len(diagnostic.schema_mismatches),
            "real_broker_writes_enabled": False,
        }
