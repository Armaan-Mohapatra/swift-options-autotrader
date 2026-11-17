"""Local credential validation uses synthetic values only."""
from pathlib import Path

import pytest

from swift_autotrader.config import ConfigurationError, Settings


def test_telegram_disabled_remains_default():
    settings = Settings.from_env({})
    assert not settings.telegram_enabled
    assert settings.telegram_api_hash == ""


def test_missing_credentials_fail_closed():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"TELEGRAM_ENABLED": "true"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TELEGRAM_QUEUE_CAPACITY", "0"),
        ("TELEGRAM_QUEUE_CAPACITY", "9000"),
        ("TELEGRAM_MAX_LIVE_AGE_SECONDS", "-1"),
        ("TELEGRAM_MAX_LIVE_AGE_SECONDS", "10000"),
        ("TELEGRAM_RECONNECT_ATTEMPTS", "0"),
        ("TELEGRAM_RECONNECT_ATTEMPTS", "50"),
        ("SWIFT_TELEGRAM_CHANNEL_ID", "not-an-int"),
        ("TELEGRAM_API_ID", "no"),
    ],
)
def test_invalid_telegram_settings_rejected(key, value):
    with pytest.raises(ConfigurationError):
        Settings.from_env({key: value})


def test_fake_local_session_can_validate_without_network(tmp_path):
    path = tmp_path / "synthetic.session"
    path.write_text("not a real session", encoding="utf-8")
    values = {
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "1234",
        "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_SESSION_PATH": str(path),
    }
    assert path.is_absolute()
    cfg = Settings.from_env(values)
    assert cfg.telegram_enabled
    assert "a" * 32 not in repr(cfg)
    assert "1234" not in str(cfg.public_summary())
    assert str(path) not in str(cfg.public_summary())


def test_relative_session_path_denied():
    with pytest.raises(ConfigurationError):
        Settings.from_env({
            "TELEGRAM_ENABLED": "true",
            "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
            "TELEGRAM_API_ID": "1234",
            "TELEGRAM_API_HASH": "a" * 32,
            "TELEGRAM_SESSION_PATH": str(Path("relative.session")),
        })


def _session_env(path):
    return {
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "1234",
        "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_SESSION_PATH": str(path),
    }


def test_session_in_cwd_without_git_is_allowed(tmp_path, monkeypatch):
    """The service runs with cwd=<install root>, where secrets/telegram.session lives."""
    session = tmp_path / "secrets" / "telegram.session"
    session.parent.mkdir()
    session.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env(_session_env(session)).telegram_enabled


def test_session_inside_git_checkout_denied(tmp_path):
    (tmp_path / ".git").mkdir()
    session = tmp_path / "telegram.session"
    session.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="checkout"):
        Settings.from_env(_session_env(session))
