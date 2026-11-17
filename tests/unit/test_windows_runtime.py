"""Portable, isolated Phase 6 runtime tests (no Windows machine touched)."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.windows_runtime import (
    Child,
    PaperSupervisor,
    backup_database,
    restore_database,
    service_status,
    single_instance,
    validate_install_root,
)


@pytest.fixture
def install(tmp_path: Path) -> Path:
    root = tmp_path / "SwiftAutoTraderPaper"
    root.mkdir()
    for name in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / name).mkdir()
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "PAPER_ONLY",
        "live_enabled": False, "broker_writes_enabled": False,
    }), encoding="utf-8")
    apply_migrations(root / "data" / "swift.sqlite3")
    return root


def test_manifest_rejects_unmanaged_paths(install: Path, tmp_path: Path) -> None:
    assert validate_install_root(install) == install
    with pytest.raises(RuntimeError):
        validate_install_root(tmp_path)
    doc = json.loads((install / "deployment.json").read_text())
    doc["broker_writes_enabled"] = True
    (install / "deployment.json").write_text(json.dumps(doc))
    with pytest.raises(RuntimeError):
        validate_install_root(install)


def test_validate_install_root_live_manifest(install: Path) -> None:
    # Valid live manifest
    live_doc = {
        "product": "swift-options-autotrader", "mode": "LIVE",
        "live_enabled": True, "broker_writes_enabled": True,
    }
    (install / "deployment.json").write_text(json.dumps(live_doc))
    assert validate_install_root(install) == install

    # Unsafe live manifest (live_enabled False)
    bad_live_doc = {
        "product": "swift-options-autotrader", "mode": "LIVE",
        "live_enabled": False, "broker_writes_enabled": True,
    }
    (install / "deployment.json").write_text(json.dumps(bad_live_doc))
    with pytest.raises(RuntimeError, match="Unsafe or unknown installation manifest"):
        validate_install_root(install)

    # Allowed modes restriction
    (install / "deployment.json").write_text(json.dumps(live_doc))
    with pytest.raises(RuntimeError, match="Unsafe or unknown installation manifest"):
        validate_install_root(install, allowed_modes=("PAPER_ONLY",))


def test_nonblocking_single_instance(install: Path) -> None:
    lock = install / "state" / "service.lock"
    with (
        single_instance(lock),
        pytest.raises(RuntimeError, match="already running"),
        single_instance(lock),
    ):
        pass
    with single_instance(lock):
        pass


def test_online_sqlite_backup_restoration_and_checksum(install: Path) -> None:
    db = install / "data" / "swift.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE synthetic_check (amount INTEGER)")
        connection.execute("INSERT INTO synthetic_check VALUES (17)")
        connection.commit()  # online SQLite backup copies committed state, not uncommitted writes
        first = backup_database(install)
        assert first.is_file()
        with sqlite3.connect(first) as snapshot:
            assert snapshot.execute("SELECT amount FROM synthetic_check").fetchone() == (17,)
        connection.execute("UPDATE synthetic_check SET amount = 99")
    connection.close()  # Release WAL snapshots before operator-confirmed offline restore.
    restore_database(install, first, confirm_offline=True)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT amount FROM synthetic_check").fetchone() == (17,)
    # Replaying the prior snapshot into the same state must not overwrite
    # existing immutable snapshots or erase the user's rollback evidence.
    assert len(list((install / "backups").glob("swift-*.sqlite3"))) >= 2


def test_restore_requires_shutdown_valid_snapshot_and_no_wal(install: Path) -> None:
    saved = backup_database(install)
    with pytest.raises(PermissionError):
        restore_database(install, saved)
    (saved.with_suffix(".sha256.json")).write_text('{"sha256":"tampered"}')
    with pytest.raises(RuntimeError, match="hash"):
        restore_database(install, saved, confirm_offline=True)
    saved = backup_database(install)
    wal = Path(str(install / "data" / "swift.sqlite3") + "-wal")
    wal.write_text("uncheckpointed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="WAL"):
        restore_database(install, saved, confirm_offline=True)


def test_backup_retention_and_isolation(install: Path, tmp_path: Path) -> None:
    for _ in range(3):
        backup_database(install, keep=2)
    assert len(list((install / "backups").glob("swift-*.sqlite3"))) == 2
    with pytest.raises(RuntimeError):
        backup_database(install, target=tmp_path / "other.sqlite3")


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = io.BytesIO(b"")
        self.terminated = False

    def poll(self):
        return self.returncode

    def send_signal(self, _signal):
        self.terminate()

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=8):
        return 0

    def kill(self):
        self.terminate()


def test_supervisor_single_worker_start_stop_and_no_order_path(install: Path) -> None:
    calls = []
    processes = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        proc = FakeProcess()
        processes.append(proc)
        return proc

    env = {
        "PAPER_ONLY": "true", "LIVE_ENABLED": "false",
        "ALLOW_BROKER_WRITES": "false", "BROKER_BACKEND": "paper",
        "DB_PATH": str(install / "data" / "swift.sqlite3"),
        "TELEGRAM_STATUS_BOT_TOKEN": "should-not-go-to-dashboard",
    }
    supervisor = PaperSupervisor(install, env=env, spawn=spawn, sleep=lambda _x: None)
    supervisor._notify_scan = lambda: None
    supervisor._daily_backup = lambda: None
    supervisor.run(max_cycles=1)
    assert len(calls) == 1
    assert calls[0][0][-5:] == [
        "paper-dashboard", "--host", "127.0.0.1", "--port", "8765",
    ]
    assert "TELEGRAM_STATUS_BOT_TOKEN" not in calls[0][1]["env"]
    assert "paper-cycle" not in repr(calls)
    assert "parse-replay" not in repr(calls)
    assert processes[0].terminated is True
    assert service_status(install)["status"] == "STOPPED"


def test_supervisor_respects_fail_closed_config(install: Path) -> None:
    safe = {"PAPER_ONLY": "true", "LIVE_ENABLED": "true",
            "ALLOW_BROKER_WRITES": "false",
            "DB_PATH": str(install / "data" / "swift.sqlite3")}
    with pytest.raises(ValueError):
        PaperSupervisor(install, env=safe)
    safe["LIVE_ENABLED"] = "false"
    safe["DB_PATH"] = str(install / "data" / "other.sqlite3")
    with pytest.raises(RuntimeError):
        PaperSupervisor(install, env=safe)


def test_heartbeat_staleness_and_stopped_status(install: Path) -> None:
    old = datetime.now(UTC) - timedelta(minutes=3)
    health = install / "state" / "health.json"
    health.write_text(json.dumps({"status": "RUNNING", "as_of_utc": old.isoformat()}))
    assert service_status(install)["status"] == "STALE"
    health.write_text(json.dumps({"status": "STOPPED", "as_of_utc": old.isoformat()}))
    assert service_status(install)["status"] == "STOPPED"


def test_worker_crash_restarts_with_bound_and_without_new_orders(install: Path) -> None:
    running = []
    wait_times = []

    def spawn(argv, **kwargs):
        proc = FakeProcess()
        running.append((tuple(argv), proc))
        return proc

    env = {
        "PAPER_ONLY": "true", "LIVE_ENABLED": "false",
        "ALLOW_BROKER_WRITES": "false", "BROKER_BACKEND": "paper",
        "DB_PATH": str(install / "data" / "swift.sqlite3"),
    }
    supervisor = PaperSupervisor(install, env=env, spawn=spawn,
                                 sleep=wait_times.append, now=lambda: 500.0)
    supervisor._start(supervisor.children[0])
    running[0][1].returncode = 1
    supervisor._check_workers()
    assert len(running) == 2
    assert supervisor.children[0].failures == 1
    assert wait_times == [2]
    assert "paper-dashboard" in running[-1][0]
    assert "paper-cycle" not in repr(running)
    for _ in range(5):
        supervisor.children[0].process.returncode = 1
        if _ == 4:
            with pytest.raises(RuntimeError, match="Repeated worker failure"):
                supervisor._check_workers()
        else:
            supervisor._check_workers()


def test_windows_runtime_drains_output_and_bounds_worker_logs(install: Path) -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import logging,sys; from swift_autotrader.logging_config import configure_logging; "
        "configure_logging(); log=logging.getLogger('swift_autotrader.test'); "
        "[log.info('private alert %s', 'x'*100) for _ in range(40000)]; "
        "sys.stdout.write('z'*2000000+'\\n'); sys.stdout.flush()"
    )
    env = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(source_root),
        "PAPER_ONLY": "true", "LIVE_ENABLED": "false",
        "ALLOW_BROKER_WRITES": "false", "BROKER_BACKEND": "paper",
        "DB_PATH": str(install / "data" / "swift.sqlite3"),
    }

    def spawn(_argv, **kwargs):
        return subprocess.Popen([sys.executable, "-c", code], **kwargs)

    supervisor = PaperSupervisor(install, env=env, spawn=spawn)
    child = Child("noisy", ("test",))
    supervisor._start(child)
    assert child.process is not None
    assert child.process.wait(timeout=10) == 0
    for thread in supervisor._threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    files = list((install / "logs").glob("worker-noisy.jsonl*"))
    assert files and len(files) <= 4
    assert sum(path.stat().st_size for path in files) <= 8 * 1024 * 1024
    assert all("private alert" not in path.read_text(encoding="utf-8") for path in files)


def test_windows_runtime_terminates_real_worker_process(install: Path) -> None:
    env = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "PATH": os.environ.get("PATH", ""), "PAPER_ONLY": "true",
        "LIVE_ENABLED": "false", "ALLOW_BROKER_WRITES": "false",
        "BROKER_BACKEND": "paper", "DB_PATH": str(install / "data" / "swift.sqlite3"),
    }

    def spawn(_argv, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    supervisor = PaperSupervisor(install, env=env, spawn=spawn)
    child = Child("sleeper", ("test",))
    supervisor._start(child)
    assert child.process is not None
    time.sleep(0.2)
    supervisor._stop_child(child)
    assert child.process.poll() is not None


def test_stop_marker_stops_service_and_prevents_new_worker(install: Path) -> None:
    pids = []

    def spawn(_argv, **_kwargs):
        p = FakeProcess()
        pids.append(p)
        return p

    env = {"PAPER_ONLY": "true", "DB_PATH": str(install / "data/swift.sqlite3")}
    supervisor = PaperSupervisor(install, env=env, spawn=spawn, sleep=lambda _: None)
    supervisor._notify_scan = lambda: None
    supervisor._daily_backup = lambda: None

    def stop_soon(_seconds):
        (install / "state/stop.request").write_text("STOP")

    supervisor.sleep = stop_soon
    supervisor.run()
    assert len(pids) == 1
    assert pids[0].terminated
    assert not (install / "state/stop.request").exists()


def test_backup_integrity_rejects_file_not_database(install: Path) -> None:
    first = backup_database(install)
    first.write_bytes(b"not an sqlite database")
    with pytest.raises(RuntimeError, match="hash"):
        restore_database(install, first, confirm_offline=True)


def test_notification_scan_works_without_telegram_secrets(install: Path, monkeypatch) -> None:
    from swift_autotrader import windows_runtime
    from swift_autotrader.config import Settings

    session = install / "secrets/telegram.session"
    session.write_bytes(b"synthetic")
    env = {
        "SWIFT_INSTALL_ROOT": str(install), "DB_PATH": str(install / "data/swift.sqlite3"),
        "TELEGRAM_ENABLED": "true", "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_SESSION_PATH": str(session), "TELEGRAM_STATUS_BOT_TOKEN": "synthetic",
        "KILL_SWITCH_TELEGRAM_BOT_TOKEN": "synthetic", "ROBINHOOD_MCP_ACCOUNT": "synthetic",
    }
    monkeypatch.chdir(install)
    observed = []

    def fake_run(argv, **kwargs):
        scanned = kwargs["env"]
        assert Settings.from_env(scanned).paper_only
        assert not Settings.from_env(scanned).telegram_enabled
        for key in ("TELEGRAM_API_HASH", "TELEGRAM_SESSION_PATH", "TELEGRAM_API_ID",
                    "TELEGRAM_STATUS_BOT_TOKEN", "KILL_SWITCH_TELEGRAM_BOT_TOKEN",
                    "ROBINHOOD_MCP_ACCOUNT"):
            assert key not in scanned
        observed.append(argv[-1])
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(windows_runtime.subprocess, "run", fake_run)
    PaperSupervisor(install, env=env)._notify_scan()
    assert observed == ["paper-notify-scan"]


def test_paper_supervisor_rejects_fully_live_settings(install: Path, tmp_path: Path) -> None:
    session = tmp_path / "synthetic.session"
    session.write_bytes(b"synthetic")
    env = {
        "PAPER_ONLY": "false", "LIVE_ENABLED": "true", "ALLOW_BROKER_WRITES": "true",
        "MAX_PREMIUM_PER_SIGNAL_USD": "999", "MAX_TOTAL_EXPOSURE_USD": "9999",
        "BROKER_BACKEND": "robinhood_mcp", "ROBINHOOD_MCP_ACCOUNT": "synthetic",
        "DB_PATH": str(install / "data/swift.sqlite3"),
        "TELEGRAM_ENABLED": "true", "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_SESSION_PATH": str(session),
    }
    with pytest.raises(PermissionError, match="PAPER supervisor"):
        PaperSupervisor(install, env=env)


def test_dead_worker_heartbeat_is_degraded(install: Path) -> None:
    supervisor = PaperSupervisor(install, env={"DB_PATH": str(install / "data/swift.sqlite3")})
    supervisor._heartbeat("RUNNING")
    result = service_status(install)
    assert result["status"] == "DEGRADED"
    assert result["health_scope"] == "PROCESS_LIVENESS_ONLY"
    assert result["live_startup_ready"] is False
    assert result["workers"] == {"dashboard": False}


@pytest.mark.parametrize("workers", [{}, {"dashboard": False}, {"dashboard": "true"}, None])
def test_fresh_running_record_does_not_hide_missing_worker_evidence(install: Path, workers) -> None:
    now = datetime.now(UTC)
    (install / "state/health.json").write_text(json.dumps({
        "status": "RUNNING", "as_of_utc": now.isoformat(), "workers": workers,
    }), encoding="utf-8")
    assert service_status(install, now=now)["status"] == "DEGRADED"


def test_degraded_heartbeat_also_expires(install: Path) -> None:
    old = datetime.now(UTC) - timedelta(minutes=3)
    (install / "state/health.json").write_text(json.dumps({
        "status": "DEGRADED", "as_of_utc": old.isoformat(),
    }), encoding="utf-8")
    assert service_status(install)["status"] == "STALE"
