"""Installed-directory regression; exclusively synthetic credentials and processes."""
import json
from pathlib import Path

import pytest

from swift_autotrader.config import ConfigurationError, Settings, validate_private_path
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.windows_runtime import PaperSupervisor


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "install-837aec03"
    for name in ("secrets", "data", "logs", "backups", "state", "releases"):
        (root / name).mkdir(parents=True)
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "PAPER_ONLY",
        "live_enabled": False, "broker_writes_enabled": False,
    }), encoding="utf-8")
    (root / "secrets/telegram.session").write_bytes(b"synthetic")
    (root / "secrets/oauth.dpapi").write_bytes(b"synthetic")
    db = root / "data/swift.sqlite3"
    apply_migrations(db)
    monkeypatch.chdir(root)
    env = {"SWIFT_INSTALL_ROOT": str(root), "DB_PATH": str(db),
           "TELEGRAM_ENABLED": "true", "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
           "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "a" * 32,
           "TELEGRAM_SESSION_PATH": str(root / "secrets/telegram.session")}
    return root, env


def test_installed_service_configuration(installed):
    root, env = installed
    settings = Settings.from_env(env)
    assert settings.paper_only and not settings.allow_broker_writes
    supervisor = PaperSupervisor(root, env=env)
    assert {c.name for c in supervisor.children} == {"dashboard", "telegram"}


def test_managed_oauth_under_working_directory(installed):
    root, _ = installed
    path = root / "secrets/oauth.dpapi"
    assert validate_private_path(path, install_root=root, managed_name="oauth.dpapi") == path


def test_unmanaged_working_directory_denied(installed):
    _, env = installed
    with pytest.raises(ConfigurationError, match="checkout"):
        Settings.from_env({k: v for k, v in env.items() if k != "SWIFT_INSTALL_ROOT"})


def test_manifest_required(installed):
    root, env = installed
    (root / "deployment.json").unlink()
    with pytest.raises(ConfigurationError, match="manifest"):
        Settings.from_env(env)


def test_wrong_secret_slot_denied(installed):
    root, env = installed
    other = root / "data/other.session"
    other.write_bytes(b"synthetic")
    with pytest.raises(ConfigurationError, match="private slot"):
        Settings.from_env(dict(env, TELEGRAM_SESSION_PATH=str(other)))


def test_checkout_even_with_valid_manifest_denied(installed):
    root, env = installed
    (root / ".git").mkdir()
    with pytest.raises(ConfigurationError, match="checkout"):
        Settings.from_env(env)


def test_other_checkout_denied(installed, tmp_path):
    _, env = installed
    checkout = tmp_path / "other-checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    path = checkout / "leaked.session"
    path.write_bytes(b"synthetic")
    with pytest.raises(ConfigurationError, match="checkout"):
        Settings.from_env(dict(env, TELEGRAM_SESSION_PATH=str(path)))


def test_redirected_secret_denied(installed, tmp_path):
    root, env = installed
    path = root / "secrets/telegram.session"
    path.unlink()
    outside = tmp_path / "elsewhere.session"
    outside.write_bytes(b"synthetic")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit creating symbolic links")
    with pytest.raises(ConfigurationError, match="redirected"):
        Settings.from_env(env)


def test_live_manifest_not_accepted(installed):
    root, env = installed
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "LIVE",
        "live_enabled": True, "broker_writes_enabled": True,
    }), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="private slot"):
        Settings.from_env(env)
