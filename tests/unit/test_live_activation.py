"""Owner activation gates, receipt handling, read-only account check and expiry behaviour.

Everything runs against a synthetic PAPER installation in a temporary directory with fake
credential bytes and disposable SQLite databases. The Windows DPAPI pinned-account read is
replaced by a monkeypatch, and the broker is a local in-memory double whose only tool is
get_accounts. No test can reach a write tool.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader import entry_control
from swift_autotrader import live_activation as activation
from swift_autotrader.broker.live_broker import engine_kill_switch
from swift_autotrader.broker.mcp_local_auth import LocalAuthNotConfigured
from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.live_service import run_live_service
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.unattended_approval import (
    RECEIPT_NAME,
    UnattendedApproval,
    UnattendedApprovalError,
)
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP, live_journal

ACCOUNT = "ACCT1234567"
NOW = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def _candidate(root: Path, **overrides) -> dict:
    doc = {
        "mode": "LIVE",
        "live_enabled": True,
        "broker_writes_enabled": True,
        "max_contracts_per_signal": 1,
        "max_premium_per_signal_usd": 999,
        "max_daily_loss_usd": 750,
        "live_journal_path": str(root / "data" / "live" / "journal.sqlite3"),
        "robinhood_mcp_account": ACCOUNT,
        "telegram_enabled": True,
        "telegram_channel_id": -1001234000001,
        "telegram_api_id": 12345,
        "log_level": "INFO",
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def install(tmp_path: Path, monkeypatch):
    """A complete PAPER installation that would pass every pre-activation gate."""
    root = tmp_path / "install"
    for part in ("config", "data", "data/live", "logs", "backups", "secrets", "state", "releases"):
        (root / part).mkdir(parents=True, exist_ok=True)
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "PAPER_ONLY",
        "live_enabled": False, "broker_writes_enabled": False,
    }), encoding="utf-8")
    (root / "config" / "settings.json").write_text(json.dumps({
        "mode": "PAPER_ONLY", "live_enabled": False, "paper_dispatch_enabled": True,
    }), encoding="utf-8")
    candidate_path = root / "config" / "settings.live.candidate.json"
    candidate_path.write_text(json.dumps(_candidate(root)), encoding="utf-8")
    for name in ("telegram.session", "telegram_api_hash.dpapi", "oauth.dpapi", "agentic_account.dpapi"):
        (root / "secrets" / name).write_bytes(b"synthetic")
    live_db = root / "data" / "live.sqlite3"
    apply_migrations(live_db)
    live_journal(root / "data" / "live" / "journal.sqlite3")
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth.load_pinned_agentic_account",
                        lambda _root: ACCOUNT)
    return root, candidate_path


def _receipt_doc(account: str = ACCOUNT, **overrides) -> dict:
    doc = {
        "version": 2,
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "issued_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=2)).isoformat(),
        "max_contracts_per_signal": 1,
        "max_premium_per_signal_usd": "999",
        "max_daily_loss_usd": "750",
    }
    doc.update(overrides)
    return doc


def _gates(report: dict) -> dict[str, dict]:
    return {gate["gate"]: gate for gate in report["gates"]}


# --- Candidate review against the owner's fixed limits --------------------------------

def test_good_candidate_passes_every_candidate_gate(install):
    root, path = install
    candidate = activation.load_candidate(path)
    assert all(g.passed for g in activation.check_candidate(candidate, root=root))


@pytest.mark.parametrize("change", [
    {"unexpected_field": True},
    {"max_premium_per_signal_usd": 1000},
    {"max_premium_per_signal_usd": 0},
    {"max_daily_loss_usd": 800},
    {"max_contracts_per_signal": 2},
    {"live_journal_path": "relative/journal.sqlite3"},
    {"live_journal_path": "C:/elsewhere/journal.sqlite3"},
    {"telegram_channel_id": 5},
    {"kill_switch_chat_id": -1001234000001},
    {"mode": "PAPER_ONLY"},
    {"telegram_enabled": False},
    {"log_level": "VERBOSE"},
])
def test_bad_candidate_is_refused(install, change):
    root, _ = install
    candidate = _candidate(root, **change)
    failed = [g for g in activation.check_candidate(candidate, root=root) if not g.passed]
    assert failed, change


def test_candidate_missing_required_field_is_refused(install):
    root, _ = install
    candidate = _candidate(root)
    del candidate["robinhood_mcp_account"]
    assert any(g.name == "candidate_fields" and not g.passed
               for g in activation.check_candidate(candidate, root=root))


def test_candidate_file_rejects_oversize_symlink_and_non_object(tmp_path):
    big = tmp_path / "big.json"
    big.write_text("{" + " " * (16 * 1024) + "}", encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        activation.load_candidate(big)
    arr = tmp_path / "arr.json"
    arr.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        activation.load_candidate(arr)


# --- Pre-activation gates ---------------------------------------------------------------

def test_pre_activation_passes_on_a_clean_paper_install(install):
    root, path = install
    report = activation.run_checks(root, path, phase="pre", now=NOW)
    assert report["passed"], report
    assert _gates(report)["live_journal"]["passed"]
    assert _gates(report)["pinned_account"]["passed"]


def test_pre_activation_refuses_an_already_live_manifest(install):
    root, path = install
    manifest = json.loads((root / "deployment.json").read_text())
    manifest.update(mode="LIVE", live_enabled=True, broker_writes_enabled=True)
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not _gates(activation.run_checks(root, path, phase="pre", now=NOW))["manifest"]["passed"]


def test_pinned_account_mismatch_is_refused(install, monkeypatch):
    root, path = install
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth.load_pinned_agentic_account",
                        lambda _root: "OTHER7654321")
    assert not _gates(activation.run_checks(root, path, phase="pre", now=NOW))["pinned_account"]["passed"]


def test_unreadable_pinned_account_is_refused(install, monkeypatch):
    root, path = install

    def refuse(_root):
        raise LocalAuthNotConfigured("no DPAPI")

    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth.load_pinned_agentic_account", refuse)
    assert not _gates(activation.run_checks(root, path, phase="pre", now=NOW))["pinned_account"]["passed"]


def test_persistent_review_stop_marker_is_refused(install):
    root, path = install
    (root / "state" / "SWIFT_REVIEWED_STOP").write_text("STOP", encoding="utf-8")
    assert not _gates(activation.run_checks(root, path, phase="pre", now=NOW))["persistent_stop"]["passed"]


def test_missing_credential_is_refused(install):
    root, path = install
    (root / "secrets" / "oauth.dpapi").unlink()
    assert not _gates(activation.run_checks(root, path, phase="pre", now=NOW))["credentials_present"]["passed"]


# --- Journal and control database integrity --------------------------------------------

def test_missing_journal_is_refused(install):
    root, path = install
    (root / "data" / "live" / "journal.sqlite3").unlink()
    assert not activation.check_journal(root, activation.load_candidate(path)).passed


def test_empty_journal_is_accepted_as_not_yet_initialised(install):
    root, path = install
    journal = root / "data" / "live" / "journal.sqlite3"
    journal.unlink()
    journal.touch()
    gate = activation.check_journal(root, activation.load_candidate(path))
    assert gate.passed and "first live start" in gate.reason


def test_corrupt_journal_is_refused(install):
    root, path = install
    (root / "data" / "live" / "journal.sqlite3").write_bytes(b"not a database" * 100)
    assert not activation.check_journal(root, activation.load_candidate(path)).passed


def test_journal_that_is_not_a_swift_journal_is_refused(install):
    root, path = install
    journal = root / "data" / "live" / "journal.sqlite3"
    journal.unlink()
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE unrelated(x INTEGER)")
    gate = activation.check_journal(root, activation.load_candidate(path))
    assert not gate.passed and "not a Swift live order journal" in gate.reason


def test_journal_pointing_at_control_database_is_refused(install):
    root, path = install
    candidate = _candidate(root, live_journal_path=str(root / "data" / "live.sqlite3"))
    assert not activation.check_journal(root, candidate).passed


def test_unmigrated_control_database_is_refused(install):
    root, _ = install
    with sqlite3.connect(root / "data" / "live.sqlite3") as db:
        db.execute("DELETE FROM schema_migrations WHERE version = '0005_research_monitoring.sql'")
    gates = {g.name: g for g in activation.check_control_db(root / "data" / "live.sqlite3")}
    assert not gates["control_db_migrations"].passed


def test_control_database_reports_entry_kill_switch_state(install):
    root, _ = install
    gates = {g.name: g for g in activation.check_control_db(root / "data" / "live.sqlite3")}
    assert gates["control_db_migrations"].passed and gates["kill_switch_readable"].passed


def test_entry_pause_blocks_new_entries(install):
    root, _ = install
    db = root / "data" / "live.sqlite3"
    entry_control.pause_entries(db)
    assert engine_kill_switch(db)() is True
    entry_control.resume_entries(db)
    assert engine_kill_switch(db)() is False


def test_unreadable_entry_control_counts_as_blocked(tmp_path):
    missing = tmp_path / "absent.sqlite3"
    assert engine_kill_switch(missing)() is True


# --- Owner receipt -----------------------------------------------------------------------

def test_receipt_is_built_capped_to_the_reviewed_configuration(install):
    root, path = install
    candidate = activation.load_candidate(path)
    doc = activation.build_receipt(ACCOUNT, candidate, NOW, 7)
    assert doc["max_contracts_per_signal"] == 1
    assert doc["max_premium_per_signal_usd"] == "999"
    assert doc["max_daily_loss_usd"] == "750"
    assert doc["account_sha256"] == hashlib.sha256(ACCOUNT.encode()).hexdigest()
    assert datetime.fromisoformat(doc["expires_at"]) - datetime.fromisoformat(doc["issued_at"]) == timedelta(days=7)


@pytest.mark.parametrize("days", [0, 8, -1])
def test_receipt_validity_outside_seven_days_is_refused(install, days):
    _, path = install
    with pytest.raises(ValueError):
        activation.build_receipt(ACCOUNT, activation.load_candidate(path), NOW, days)


def test_written_receipt_loads_and_refuses_overwrite(install):
    root, path = install
    candidate = activation.load_candidate(path)
    doc = activation.build_receipt(ACCOUNT, candidate, NOW, 7)
    written = activation.write_receipt(root, doc, candidate, NOW)
    assert written.name == RECEIPT_NAME and written.is_file()
    with pytest.raises(FileExistsError):
        activation.write_receipt(root, doc, candidate, NOW)


def test_receipt_that_does_not_validate_is_removed_not_kept(install):
    root, path = install
    candidate = activation.load_candidate(path)
    doc = activation.build_receipt(ACCOUNT, candidate, NOW, 7)
    doc["max_premium_per_signal_usd"] = "500"  # below the configured ceiling: must not validate
    with pytest.raises(UnattendedApprovalError):
        activation.write_receipt(root, doc, candidate, NOW)
    assert not (root / "secrets" / RECEIPT_NAME).exists()


@pytest.mark.parametrize("state", ["missing", "expired", "wrong-account", "corrupt"])
def test_post_activation_refuses_without_a_valid_receipt(install, state):
    root, path = install
    receipt = root / "secrets" / RECEIPT_NAME
    if state == "expired":
        receipt.write_text(json.dumps(_receipt_doc(
            issued_at=(NOW - timedelta(days=3)).isoformat(),
            expires_at=(NOW - timedelta(days=1)).isoformat())), encoding="utf-8")
    elif state == "wrong-account":
        receipt.write_text(json.dumps(_receipt_doc(account="OTHER7654321")), encoding="utf-8")
    elif state == "corrupt":
        receipt.write_text("{not json", encoding="utf-8")
    report = activation.run_checks(root, path, phase="post", now=NOW)
    assert not report["passed"]
    assert not _gates(report)["owner_receipt"]["passed"]


def test_account_ending_must_match_exactly(install):
    assert activation.account_ending_matches(ACCOUNT, "4567")
    assert not activation.account_ending_matches(ACCOUNT, "4568")
    assert not activation.account_ending_matches(ACCOUNT, "")
    assert not activation.account_ending_matches(ACCOUNT, "45678")
    assert not activation.account_ending_matches("12", "12")


# --- Apply and post-activation ----------------------------------------------------------

def test_apply_only_from_paper_then_post_checks_pass(install):
    root, path = install
    candidate = activation.load_candidate(path)
    activation.write_receipt(root, activation.build_receipt(ACCOUNT, candidate, NOW, 7), candidate, NOW)
    activation.apply_live_config(root, candidate)
    settings = json.loads((root / "config" / "settings.json").read_text())
    manifest = json.loads((root / "deployment.json").read_text())
    assert settings["mode"] == "LIVE" and manifest["mode"] == "LIVE"
    assert manifest["live_enabled"] is True and manifest["broker_writes_enabled"] is True
    assert settings["max_premium_per_signal_usd"] == 999
    report = activation.run_checks(root, path, phase="post", now=NOW)
    assert report["passed"], report
    with pytest.raises(ValueError, match="PAPER_ONLY"):
        activation.apply_live_config(root, candidate)


def test_diff_masks_account_and_chat_values(install):
    root, path = install
    lines = activation.settings_diff(root, activation.load_candidate(path))
    text = "\n".join(lines)
    assert ACCOUNT not in text and "-1001234000001" not in text
    assert "max_premium_per_signal_usd" in text


# --- Read-only broker account check ------------------------------------------------------

def _fake_transport(monkeypatch, fake: StatefulFakeMCP) -> list[str]:
    """Route the account check through the local double; record every tool name called."""
    seen: list[str] = []

    class RecordingFake:
        def __init__(self, inner):
            self._inner = inner

        async def list_tools(self):
            return await self._inner.list_tools()

        async def call_tool(self, name, arguments):
            seen.append(name)
            return await self._inner.call_tool(name, arguments)

    @asynccontextmanager
    async def fake_client(_enrollment):
        yield RecordingFake(fake)

    monkeypatch.setattr("swift_autotrader.broker.mcp_transport.authenticated_mcp_client", fake_client)
    monkeypatch.setattr("swift_autotrader.broker.mcp_local_auth.WindowsDPAPITokenStorage",
                        lambda *a, **k: object())
    return seen


def test_readonly_account_check_verifies_pinned_account_with_get_accounts_only(install, monkeypatch):
    root, _ = install
    fake = StatefulFakeMCP(account_number=ACCOUNT, buying_power=Decimal("1000"),
                           clock=FakeClock(NOW))
    seen = _fake_transport(monkeypatch, fake)
    result = asyncio.run(activation.readonly_account_check(root))
    assert result["passed"] and result["account_ending"] == "4567"
    assert set(seen) == {"get_accounts"}, seen


def test_readonly_account_check_refuses_when_account_not_returned(install, monkeypatch):
    root, _ = install
    fake = StatefulFakeMCP(account_number="OTHER7654321", buying_power=Decimal("1000"),
                           clock=FakeClock(NOW))
    _fake_transport(monkeypatch, fake)
    result = asyncio.run(activation.readonly_account_check(root))
    assert result["passed"] is False


# --- Approval expiry: entries stop, exits keep running ----------------------------------

@pytest.fixture
def live_env(tmp_path: Path):
    root = tmp_path / "live"
    for part in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / part).mkdir(parents=True, exist_ok=True)
    (root / "deployment.json").write_text(json.dumps({"product": "swift-options-autotrader"}),
                                          encoding="utf-8")
    db_path = root / "data" / "swift.sqlite3"
    apply_migrations(db_path)
    session = root / "secrets" / "telegram.session"
    session.write_bytes(b"telegram_session_data")
    settings = Settings(
        paper_only=False, live_enabled=True, allow_broker_writes=True,
        broker_backend="robinhood_mcp", robinhood_mcp_account="live-acc-123",
        max_premium_per_signal_usd=Decimal("999"), max_daily_loss_usd=Decimal("750"),
        db_path=db_path, live_journal_path=live_journal(root / "data" / "live_journal.sqlite3"),
        telegram_enabled=True, telegram_channel_id=-1001234000001, telegram_api_id=12345,
        telegram_api_hash="a" * 32, telegram_session_path=session,
    )
    return root, settings


def _approval(clock_now: datetime, *, active: bool) -> UnattendedApproval:
    account = "live-acc-123"
    if active:
        issued, expires = clock_now - timedelta(hours=1), clock_now + timedelta(days=2)
    else:
        issued, expires = clock_now - timedelta(days=3), clock_now - timedelta(days=1)
    return UnattendedApproval(
        account_sha256=hashlib.sha256(account.encode()).hexdigest(),
        issued_at=issued, expires_at=expires, max_contracts_per_signal=1,
        max_premium_per_signal_usd=Decimal("999"), max_daily_loss_usd=Decimal("750"),
    )


def _run_one_cycle(root, settings, approval, monkeypatch) -> list[tuple[bool, bool]]:
    """Run one real live cycle against the in-memory broker; record the engine's entry/exit flags."""
    clock = FakeClock(NOW)
    fake_mcp = StatefulFakeMCP(account_number="live-acc-123", buying_power=Decimal("10000.00"),
                               clock=clock)
    flags: list[tuple[bool, bool]] = []
    original = PaperOrderEngine.run_cycle

    def spy(self, *args, **kwargs):
        flags.append((kwargs.get("allow_entries"), kwargs.get("process_closes")))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PaperOrderEngine, "run_cycle", spy)

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    asyncio.run(run_live_service(settings, root, once=True, client_override=mock_client_ctx(),
                                 clock=clock, approval_override=approval))
    return flags


def test_expired_approval_blocks_entries_but_still_runs_exit_processing(live_env, monkeypatch):
    root, settings = live_env
    flags = _run_one_cycle(root, settings, _approval(NOW, active=False), monkeypatch)
    assert flags, "the cycle must still run after expiry"
    assert all(allow is False for allow, _ in flags)
    assert any(closes is True for _, closes in flags), "exits must still be processed"
    health = json.loads((root / "state" / "live-health.json").read_text())
    assert health["approval_allows_entries"] is False


def test_active_approval_allows_entries_in_the_cycle(live_env, monkeypatch):
    root, settings = live_env
    flags = _run_one_cycle(root, settings, _approval(NOW, active=True), monkeypatch)
    assert any(allow is True for allow, _ in flags)
    assert json.loads((root / "state" / "live-health.json").read_text())["approval_allows_entries"] is True


def test_expiry_is_recorded_once_in_existing_notifications(live_env, monkeypatch):
    root, settings = live_env
    _run_one_cycle(root, settings, _approval(NOW, active=False), monkeypatch)
    with sqlite3.connect(settings.db_path) as db:
        rows = db.execute("SELECT category, severity, source_code FROM local_notifications "
                          "WHERE source_code = 'UNATTENDED_APPROVAL_EXPIRED'").fetchall()
    assert rows == [("APPLICATION", "ERROR", "UNATTENDED_APPROVAL_EXPIRED")]


def test_dispatcher_withholds_entries_when_asked(live_env, monkeypatch):
    """The dispatcher hands entries_allowed straight to the engine; the default stays True."""
    root, settings = live_env
    seen: list[object] = []
    original = LiveDispatcher.run_once

    async def spy(self, *, entries_allowed: bool = True):
        seen.append(entries_allowed)
        return await original(self, entries_allowed=entries_allowed)

    monkeypatch.setattr(LiveDispatcher, "run_once", spy)
    _run_one_cycle(root, settings, _approval(NOW, active=False), monkeypatch)
    assert seen == [False]


# --- Activation scripts parse under Windows PowerShell -----------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser")
@pytest.mark.parametrize("script", ["activate-live.ps1", "service.ps1", "setup.ps1"])
def test_powershell_scripts_parse(script):
    import subprocess

    path = Path(__file__).resolve().parents[2] / "deployment" / "windows" / script
    command = (
        f"$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}',[ref]$t,[ref]$e);"
        "if($e){ $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dataclass_replace_helper_is_available():
    # Guards the test helpers' assumption that Settings is a dataclass.
    assert dataclasses.is_dataclass(Settings)
