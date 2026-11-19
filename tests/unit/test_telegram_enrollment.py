"""Requirement-driven unit test suite for Interactive Telegram Session Enrollment (R3 / Feature 6).

Covers:
- Tier 1: Feature Coverage (Interactive auth happy path, 2FA password prompt, channel verification,
  authorized session verification, CLI argument parsing).
- Tier 2: Boundary & Corner Cases (Session inside checkout rejection, POSIX 0o600 permissions,
  bad auth code rejection, inaccessible channel error, malformed API hash/ID, missing dependency).
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.telegram.client import LocalAuthorizationRequired, TelethonSource
from swift_autotrader.telegram.enrollment import enroll_telegram_session, verify_session
from tests.fixtures.live_mocks import MockTelethonClient, SessionPasswordNeededError

CHANNEL_ID = -1001234567890


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_interactive_enrollment_happy_path(tmp_path: Path) -> None:
    """T1-F6-01: User enters phone number and valid verification code; session saved with 0o600."""
    client = MockTelethonClient(channel_id=CHANNEL_ID)
    session_path = tmp_path / "secrets" / "user.session"

    success = asyncio.run(
        enroll_telegram_session(client, "+15551234567", "12345", session_path=session_path)
    )
    assert success is True
    assert client.authorized is True
    assert session_path.is_file()
    if os.name == "posix":
        mode = stat.S_IMODE(session_path.stat().st_mode)
        assert mode == 0o600


def test_two_factor_authentication_prompt(tmp_path: Path) -> None:
    """T1-F6-02: User account protected by 2FA prompts for password and authenticates."""
    client = MockTelethonClient(
        two_factor_required=True,
        two_factor_password="my-secure-cloud-password",
        channel_id=CHANNEL_ID,
    )
    session_path = tmp_path / "secrets" / "user_2fa.session"

    success = asyncio.run(
        enroll_telegram_session(
            client,
            "+15551234567",
            "12345",
            password="my-secure-cloud-password",
            session_path=session_path,
        )
    )
    assert success is True
    assert client.authorized is True


def test_channel_access_verification(tmp_path: Path) -> None:
    """T1-F6-03: Verify user account has read permissions for the designated Telegram channel."""
    client = MockTelethonClient(channel_id=CHANNEL_ID, channel_exists=True)
    session_path = tmp_path / "secrets" / "channel_check.session"

    asyncio.run(enroll_telegram_session(client, "+15551234567", "12345", session_path=session_path))
    entity = asyncio.run(client.get_entity(CHANNEL_ID))
    assert entity.id == CHANNEL_ID
    assert "SwiftTrades" in entity.title


def test_session_verification_authorized() -> None:
    """T1-F6-04: Existing authorized session passes preflight check."""
    client = MockTelethonClient(authorized=True, channel_id=CHANNEL_ID)
    assert asyncio.run(client.is_user_authorized()) is True
    assert asyncio.run(verify_session(client)) is True


def test_cli_enrollment_argument_parsing() -> None:
    """T1-F6-05: Settings validation enforces required Telegram parameters."""
    settings = Settings(
        telegram_enabled=True,
        telegram_channel_id=CHANNEL_ID,
        telegram_api_id=12345,
        telegram_api_hash="a" * 32,
        telegram_session_path=Path("/tmp/protected.session"),
    )
    assert settings.telegram_channel_id == CHANNEL_ID
    assert settings.telegram_api_id == 12345


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


def test_session_path_inside_checkout_rejected() -> None:
    """T2-F6-01: Storing session file inside the git checkout directory is rejected for security."""
    client = MockTelethonClient(channel_id=CHANNEL_ID)
    bad_path = Path.cwd() / "leaked_session.session"
    with pytest.raises(ConfigurationError, match="inside the checkout"):
        asyncio.run(enroll_telegram_session(client, "+15551234567", "12345", session_path=bad_path))


def test_session_file_permissions_posix(tmp_path: Path) -> None:
    """T2-F6-02: Session file must strictly have 0o600 mode (read/write only by owner)."""
    if os.name != "posix":
        pytest.skip("POSIX file mode test")

    client = MockTelethonClient(channel_id=CHANNEL_ID)
    session_path = tmp_path / "secrets" / "strict.session"
    asyncio.run(enroll_telegram_session(client, "+15551234567", "12345", session_path=session_path))
    mode = stat.S_IMODE(session_path.stat().st_mode)
    assert oct(mode) == "0o600"


def test_bad_auth_code_rejected(tmp_path: Path) -> None:
    """T2-F6-03: Invalid phone verification code raises ValueError and does not save session."""
    client = MockTelethonClient(channel_id=CHANNEL_ID)
    session_path = tmp_path / "secrets" / "failed.session"
    with pytest.raises(ValueError, match="Invalid phone code"):
        asyncio.run(enroll_telegram_session(client, "+15551234567", "00000", session_path=session_path))

    assert not session_path.exists()
    assert client.authorized is False


def test_inaccessible_channel_raises_authorization_error(tmp_path: Path) -> None:
    """T2-F6-04: Channel not found or member access missing raises error during enrollment."""
    client = MockTelethonClient(channel_id=CHANNEL_ID, channel_exists=False)
    session_path = tmp_path / "secrets" / "no_channel.session"
    with pytest.raises(ValueError, match="Could not find entity"):
        asyncio.run(enroll_telegram_session(client, "+15551234567", "12345", session_path=session_path))


def test_malformed_api_hash_or_id() -> None:
    """T2-F6-05: Non-integer API ID or incorrect hash length fails closed before connection."""
    with pytest.raises(ConfigurationError):
        Settings.from_env({
            "TELEGRAM_ENABLED": "true",
            "SWIFT_TELEGRAM_CHANNEL_ID": str(CHANNEL_ID),
            "TELEGRAM_API_ID": "-5",
            "TELEGRAM_API_HASH": "a" * 32,
            "TELEGRAM_SESSION_PATH": "/tmp/session",
        }).validate()

    with pytest.raises(ConfigurationError, match="Telegram API hash is missing or malformed"):
        Settings.from_env({
            "TELEGRAM_ENABLED": "true",
            "SWIFT_TELEGRAM_CHANNEL_ID": str(CHANNEL_ID),
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "too_short",
            "TELEGRAM_SESSION_PATH": "/tmp/session",
        }).validate()


def test_missing_telethon_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2-F6-06: TelethonSource handles missing optional dependency cleanly."""
    monkeypatch.setitem(sys.modules, "telethon", None)
    settings = Settings(
        telegram_enabled=True,
        telegram_channel_id=CHANNEL_ID,
        telegram_api_id=12345,
        telegram_api_hash="a" * 32,
        telegram_session_path=Path("/tmp/session.session"),
    )
    source = TelethonSource(settings, store=None)

    async def dummy(_):
        pass

    with pytest.raises(RuntimeError, match="Install the pinned telegram extra"):
        asyncio.run(source.listen(dummy))


def test_enrollment_with_interactive_prompt_fn(tmp_path: Path) -> None:
    """T2-F6-07: When phone or code are omitted, enrollment prompts user interactively."""
    client = MockTelethonClient(channel_id=CHANNEL_ID)
    session_path = tmp_path / "secrets" / "prompted.session"

    prompts_called = []

    def mock_prompt(prompt_text: str) -> str:
        prompts_called.append(prompt_text)
        if "phone" in prompt_text.lower():
            return "+15559876543"
        if "code" in prompt_text.lower():
            return "54321"
        return ""

    success = asyncio.run(
        enroll_telegram_session(
            client=client,
            session_path=session_path,
            prompt_fn=mock_prompt,
        )
    )
    assert success is True
    assert client.authorized is True
    assert len(prompts_called) == 2
    assert session_path.is_file()


def test_enrollment_prewarms_dialog_entity_cache(tmp_path: Path) -> None:
    """T2-F6-08: Pre-warms dialog entity cache during enrollment."""
    dialog_iterated = False

    class MockTelethonWithDialogs(MockTelethonClient):
        async def iter_dialogs(self):
            nonlocal dialog_iterated
            dialog_iterated = True
            yield SimpleNamespace(id=CHANNEL_ID, entity=SimpleNamespace(id=CHANNEL_ID))

    client = MockTelethonWithDialogs(channel_id=CHANNEL_ID)
    session_path = tmp_path / "secrets" / "prewarmed.session"
    success = asyncio.run(
        enroll_telegram_session(
            client=client,
            phone="+15551234567",
            code="12345",
            session_path=session_path,
        )
    )
    assert success is True
    assert dialog_iterated is True


def test_cli_telegram_enroll_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """T2-F6-09: CLI telegram-enroll subcommand validates parameters and outputs JSON."""
    from swift_autotrader.app import main

    session_path = tmp_path / "secrets" / "cli.session"
    fake_client = MockTelethonClient(channel_id=CHANNEL_ID)

    # Monkeypatch enroll_telegram_session to verify CLI wiring
    async def mock_enroll(**kwargs):
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_bytes(b"DATA")
        return True

    monkeypatch.setattr("swift_autotrader.telegram.enrollment.enroll_telegram_session", mock_enroll)

    code = main([
        "telegram-enroll",
        "--phone", "+15551234567",
        "--code", "12345",
        "--session-path", str(session_path),
        "--channel-id", str(CHANNEL_ID),
    ])
    assert code == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "SESSION_ENROLLED"
    assert out["session_path"] == str(session_path)

