"""Static deployment assertions run on Linux/Windows, no OS or account changes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deployment" / "windows"


def test_required_scripts_are_present_and_readable():
    for script in ("common.ps1", "setup.ps1", "manage.ps1", "service.ps1",
                   "secrets.ps1", "healthcheck.ps1"):
        code = (WINDOWS / script).read_text(encoding="utf-8")
        assert "#requires -Version 5.1" in code or script == "common.ps1"
        assert len(code) > 100


def test_preview_branch_precedes_installer_write_actions():
    code = (WINDOWS / "setup.ps1").read_text(encoding="utf-8")
    assert code.index("if (-not $Apply -or $DryRun -or $WhatIfPreference)") < code.index(
        "New-Item -ItemType Directory -Path $root"
    )
    assert "-NoSchedule" in code
    assert "--locked --no-dev --extra telegram" in code
    assert "ExpectedCommit" in code


def test_phase6_preserves_paper_only_no_unapproved_order_worker():
    source = (ROOT / "src" / "swift_autotrader" / "windows_runtime.py").read_text()
    assert '"paper-dashboard"' in source
    assert '"telegram-listen"' in source
    assert '"paper-cycle"' not in source
    assert '"parse-replay"' not in source
    assert '"broker_writes_enabled": False' in source


def test_task_user_only_and_bounded_restarts():
    setup = (WINDOWS / "setup.ps1").read_text()
    assert "-AtLogOn" in setup
    assert "-LogonType Interactive -RunLevel Limited" in setup
    assert "-RestartCount 3" in setup
    assert "-MultipleInstances IgnoreNew" in setup


def test_uninstall_guards_and_backups():
    manage = (WINDOWS / "manage.ps1").read_text()
    assert "IUnderstandDataLoss" in manage
    assert "Stop-SwiftTask" in manage
    assert "Unregister-ScheduledTask" in manage
    assert "-confirm-offline" in manage
    assert "BACKED_UP" not in manage or "Backup" in manage
