"""Unit tests for operator MCP account pinning and storage."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swift_autotrader.app import main as app_main
from swift_autotrader.broker.mcp_local_auth import (
    LocalAuthNotConfigured,
    load_pinned_agentic_account,
    pin_agentic_account,
)
from scripts.pin_account import main as pin_script_main


@pytest.fixture
def managed_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    for part in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / part).mkdir(parents=True, exist_ok=True)
    (root / "deployment.json").write_text(
        json.dumps({
            "product": "swift-options-autotrader",
            "mode": "LIVE",
            "live_enabled": True,
            "broker_writes_enabled": True,
        }),
        encoding="utf-8",
    )
    return root


def test_pin_account_refuses_non_windows(managed_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: False)
    with pytest.raises(LocalAuthNotConfigured, match="Windows DPAPI required"):
        pin_agentic_account(managed_root, "RH_TEST_1234")


def test_load_account_refuses_non_windows(managed_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: False)
    with pytest.raises(LocalAuthNotConfigured, match="Windows DPAPI required"):
        load_pinned_agentic_account(managed_root)


def test_pin_and_load_account_roundtrip(managed_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: True)

    fake_win32crypt = MagicMock()

    # Emulate CryptProtectData returning a prefix + plaintext
    def fake_protect(data, *args, **kwargs):
        return b"ENC:" + data

    # Emulate CryptUnprotectData returning (desc, cleartext)
    def fake_unprotect(cipher, *args, **kwargs):
        assert cipher.startswith(b"ENC:")
        return ("desc", cipher[4:])

    fake_win32crypt.CryptProtectData = fake_protect
    fake_win32crypt.CryptUnprotectData = fake_unprotect
    monkeypatch.setitem(sys.modules, "win32crypt", fake_win32crypt)

    target = pin_agentic_account(managed_root, "RH_ACCOUNT_987654")
    assert target == managed_root / "secrets" / "agentic_account.dpapi"
    assert target.is_file()

    loaded = load_pinned_agentic_account(managed_root)
    assert loaded == "RH_ACCOUNT_987654"


def test_pin_account_length_validation(managed_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: True)
    with pytest.raises(ValueError, match="between 4 and 256"):
        pin_agentic_account(managed_root, "abc")
    with pytest.raises(ValueError, match="between 4 and 256"):
        pin_agentic_account(managed_root, "x" * 300)


def test_pin_account_script_cli(managed_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: True)
    fake_win32crypt = MagicMock()
    fake_win32crypt.CryptProtectData = lambda d, *a, **k: b"ENC:" + d
    monkeypatch.setitem(sys.modules, "win32crypt", fake_win32crypt)

    monkeypatch.setattr(
        sys, "argv",
        ["pin_account.py", "--root", str(managed_root), "--account-number", "RH_SCRIPT_ACC_1"],
    )
    code = pin_script_main()
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "ACCOUNT_PINNED"
    assert payload["account_length"] == len("RH_SCRIPT_ACC_1")


def test_app_pin_account_command(managed_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth._is_windows", lambda: True)
    fake_win32crypt = MagicMock()
    fake_win32crypt.CryptProtectData = lambda d, *a, **k: b"ENC:" + d
    monkeypatch.setitem(sys.modules, "win32crypt", fake_win32crypt)

    code = app_main(["pin-account", "--root", str(managed_root), "--account-number", "RH_APP_ACC_2"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "ACCOUNT_PINNED"
    assert payload["account_length"] == len("RH_APP_ACC_2")
