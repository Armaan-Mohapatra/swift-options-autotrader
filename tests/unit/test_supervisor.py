"""Requirement-driven unit test suite for Cross-Platform Service Supervisor (R3 / Feature 7).

Covers:
- Tier 1: Feature Coverage (Worker lifecycle, single-instance lock, crash detection,
  clean shutdown, atomic health heartbeat).
- Tier 2: Boundary & Corner Cases (Circuit breaker crash loop, exponential backoff,
  stale lock cleanup, voluntary exit, worker fault isolation, manifest validation).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from swift_autotrader.supervisor import ServiceSupervisor
from swift_autotrader.windows_runtime import (
    single_instance,
    validate_install_root,
)
from tests.fixtures.live_mocks import MockProcessHandle


def make_supervisor(root: Path) -> ServiceSupervisor:
    """Helper to instantiate production ServiceSupervisor with safe in-memory process spawner."""
    pid_counter = 5000

    def mock_spawn(name: str, cmd: list[str], env: dict[str, str]) -> MockProcessHandle:
        nonlocal pid_counter
        pid_counter += 1
        return MockProcessHandle(pid_counter, cmd, env)

    return ServiceSupervisor(root, spawn_fn=mock_spawn)


@pytest.fixture
def managed_root(tmp_path: Path) -> Path:
    """Create a valid managed installation root directory."""
    root = tmp_path / "SwiftManagedRoot"
    root.mkdir()
    for sub in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / sub).mkdir()
    manifest = root / "deployment.json"
    manifest.write_text(json.dumps({
        "product": "swift-options-autotrader",
        "mode": "PAPER_ONLY",
        "live_enabled": False,
        "broker_writes_enabled": False,
    }), encoding="utf-8")
    return root


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_supervisor_multi_worker_lifecycle(managed_root: Path) -> None:
    """T1-F7-01: Orchestrate 4 sub-services concurrently."""
    manager = make_supervisor(managed_root)
    w_dash = manager.spawn("dashboard", ["python", "-m", "swift_autotrader", "paper-dashboard"])
    w_tele = manager.spawn("telegram", ["python", "-m", "swift_autotrader", "telegram-listen"])
    w_disp = manager.spawn("live-dispatch", ["python", "-m", "swift_autotrader", "live-dispatch"])
    w_kill = manager.spawn("kill-switch", ["python", "-m", "swift_autotrader", "kill-switch"])

    assert len(manager.workers) == 4
    for handle in (w_dash, w_tele, w_disp, w_kill):
        assert handle.poll() is None
        assert handle.pid > 0

    manager.stop_all()
    for handle in manager.workers.values():
        assert handle.terminated is True


def test_single_instance_lock_mutex(managed_root: Path) -> None:
    """T1-F7-02: Enforce nonblocking single-instance lock on service.lock."""
    lock_file = managed_root / "state" / "service.lock"
    with single_instance(lock_file):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance(lock_file):
                pass

    # Lock is re-acquirable once exited
    with single_instance(lock_file):
        pass


def test_worker_crash_detection_and_restart(managed_root: Path) -> None:
    """T1-F7-03: Detect dead child process and automatically respawn."""
    manager = make_supervisor(managed_root)
    proc = manager.spawn("live-dispatch", ["live-dispatch"])
    old_pid = proc.pid

    # Simulate unexpected process crash
    proc.crash(exit_code=1)
    assert proc.poll() == 1

    restarted = manager.check_workers(now_ts=100.0)
    assert "live-dispatch" in restarted
    new_proc = manager.workers["live-dispatch"]
    assert new_proc.pid != old_pid
    assert new_proc.poll() is None


def test_clean_shutdown_terminates_all_workers(managed_root: Path) -> None:
    """T1-F7-04: Graceful shutdown terminates all child processes cleanly."""
    manager = make_supervisor(managed_root)
    p1 = manager.spawn("w1", ["w1"])
    p2 = manager.spawn("w2", ["w2"])

    manager.stop_all()
    assert p1.terminated is True
    assert p2.terminated is True
    assert p1.poll() == 0
    assert p2.poll() == 0


def test_atomic_health_heartbeat_generation(managed_root: Path) -> None:
    """T1-F7-05: Heartbeat writes valid, atomic JSON to state/health.json."""
    manager = make_supervisor(managed_root)
    manager.spawn("telegram", ["tele"])
    manager.spawn("live-dispatch", ["disp"])

    manager.write_health_heartbeat(state="HEALTHY", as_of_utc="2026-11-16T14:30:00Z")
    health_file = managed_root / "state" / "supervisor-health.json"
    assert health_file.is_file()

    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["status"] == "HEALTHY"
    assert data["as_of_utc"] == "2026-11-16T14:30:00Z"
    assert data["pid"] == os.getpid()
    assert data["workers"]["telegram"] is True
    assert data["workers"]["live-dispatch"] is True


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


def test_crash_loop_circuit_breaker(managed_root: Path) -> None:
    """T2-F7-01: Exceeding max restarts (5 within 600s) trips circuit breaker."""
    manager = make_supervisor(managed_root)
    manager.spawn("live-dispatch", ["live-dispatch"])

    # Simulate 5 crashes in rapid succession
    for i in range(5):
        manager.workers["live-dispatch"].crash(exit_code=1)
        manager.check_workers(now_ts=100.0 + i)

    # 6th crash exceeds threshold of 5 and trips circuit breaker
    manager.workers["live-dispatch"].crash(exit_code=1)
    with pytest.raises(RuntimeError, match="Circuit breaker tripped"):
        manager.check_workers(now_ts=105.0)

    assert manager.circuit_tripped.get("live-dispatch") is True


def test_exponential_backoff_on_repeated_crashes(managed_root: Path) -> None:
    """T2-F7-02: Backoff increases exponentially upon consecutive worker failures."""
    manager = make_supervisor(managed_root)
    manager.spawn("worker", ["worker"])

    now = 1000.0
    for i in range(4):
        manager.workers["worker"].crash(exit_code=1)
        manager.check_workers(now_ts=now + i)

    # Expected delays: 2^1=2s, 2^2=4s, 2^3=8s, 2^4=16s
    delays = manager.backoff_delays["worker"]
    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_stale_lock_cleanup_on_orphan_death(managed_root: Path) -> None:
    """T2-F7-03: Process termination releases OS flock automatically."""
    lock_file = managed_root / "state" / "service.lock"
    # Even if file exists on disk with content, acquiring lock succeeds once prior process ends
    lock_file.write_bytes(b"prior_pid_9999")
    with single_instance(lock_file):
        pass
    assert lock_file.is_file()


def test_voluntary_clean_exit_no_alarm(managed_root: Path) -> None:
    """T2-F7-04: Exit code 0 is treated as normal completion without restart alarm."""
    manager = make_supervisor(managed_root)
    proc = manager.spawn("short-task", ["task"])
    proc.terminate()  # clean exit: returncode = 0

    restarted = manager.check_workers(now_ts=500.0)
    assert "short-task" not in restarted
    assert manager.failures.get("short-task", 0) == 0


def test_isolated_worker_failure_independence(managed_root: Path) -> None:
    """T2-F7-05: Crash of dashboard does not impact running live-dispatch."""
    manager = make_supervisor(managed_root)
    dash = manager.spawn("dashboard", ["dash"])
    disp = manager.spawn("live-dispatch", ["disp"])

    dash.crash(exit_code=1)
    restarted = manager.check_workers(now_ts=200.0)

    assert restarted == ["dashboard"]
    assert disp.poll() is None  # live-dispatch remains healthy and unaffected


def test_manifest_validation_rejects_unmanaged_paths(managed_root: Path, tmp_path: Path) -> None:
    """T2-F7-06: Strict validation against unmanaged directories and invalid manifests."""
    # Managed root passes validation
    assert validate_install_root(managed_root) == managed_root

    # Random directory without manifest fails
    unmanaged = tmp_path / "random"
    unmanaged.mkdir()
    with pytest.raises(RuntimeError):
        validate_install_root(unmanaged)

    # Tampered manifest with unknown product fails
    manifest = managed_root / "deployment.json"
    manifest.write_text(json.dumps({"product": "malicious-app"}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        validate_install_root(managed_root)


def test_supervisor_lock_method(managed_root: Path) -> None:
    """T2-F7-07: ServiceSupervisor.lock() enforces single-instance lock."""
    manager = make_supervisor(managed_root)
    with manager.lock():
        with pytest.raises(RuntimeError, match="already running"):
            with manager.lock():
                pass


def test_supervisor_setup_default_workers_paper_vs_live(managed_root: Path) -> None:
    """T1-F7-06: setup_default_workers configures workers correctly for paper vs live mode."""
    # Paper mode
    manager_paper = make_supervisor(managed_root)
    manager_paper.env["LIVE_ENABLED"] = "false"
    workers_paper = manager_paper.setup_default_workers()
    assert set(workers_paper) == {"dashboard", "telegram", "paper-dispatch"}

    # Live mode
    manager_live = make_supervisor(managed_root)
    manager_live.env["LIVE_ENABLED"] = "true"
    with pytest.raises(RuntimeError, match="not integrated or certified"):
        manager_live.setup_default_workers()
    assert not manager_live.workers


def test_heartbeat_contains_restart_counts(managed_root: Path) -> None:
    """T1-F7-07: write_health_heartbeat includes restart_counts per worker."""
    manager = make_supervisor(managed_root)
    manager.spawn("w1", ["w1"])
    manager.failures["w1"] = 2
    manager.write_health_heartbeat(state="HEALTHY")

    health_file = managed_root / "state" / "supervisor-health.json"
    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["restart_counts"]["w1"] == 2


def test_run_supervisor_once_execution(managed_root: Path) -> None:
    """T1-F7-08: run_supervisor executes one cycle and terminates cleanly when once=True."""
    from swift_autotrader.supervisor import run_supervisor

    def mock_factory(root: Path):
        return make_supervisor(root)

    run_supervisor(managed_root, once=True, supervisor_factory=mock_factory)
    health_file = managed_root / "state" / "supervisor-health.json"
    assert health_file.is_file()
    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["status"] == "STOPPED"

