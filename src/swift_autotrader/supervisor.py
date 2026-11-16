"""Cross-platform daemon supervisor for Swift AutoTrader services.

Manages child worker processes (dashboard, telegram-listen, live-dispatch, kill-switch)
with independent crash recovery, exponential backoff, health heartbeat writes,
and circuit breaker tripping across macOS, Linux, and Windows.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swift_autotrader.config import Settings
from swift_autotrader.windows_runtime import single_instance, validate_install_root

logger = logging.getLogger(__name__)

MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 600.0


class ProcessHandle:
    """Protocol-compatible wrapper around subprocess.Popen or simulated mock process."""

    def __init__(self, pid: int, cmd: list[str], env: dict[str, str], proc: subprocess.Popen[bytes] | Any) -> None:
        self.pid = pid
        self.cmd = list(cmd)
        self.env = dict(env)
        self._proc = proc
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self.terminated = True
        self._proc.terminate()

    def kill(self) -> None:
        self.killed = True
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()


class ServiceSupervisor:
    """Cross-platform daemon supervisor managing child worker lifecycles."""

    def __init__(
        self,
        root: Path,
        *,
        env: dict[str, str] | None = None,
        now_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
        spawn_fn: Callable[..., Any] | None = None,
        max_restarts: int = MAX_RESTARTS,
        restart_window_seconds: float = RESTART_WINDOW_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.env = dict(os.environ if env is None else env)
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.spawn_fn = spawn_fn
        self.max_restarts = max_restarts
        self.restart_window_seconds = restart_window_seconds

        self.workers: dict[str, Any] = {}
        self.crash_times: dict[str, list[float]] = {}
        self.failures: dict[str, int] = {}
        self.circuit_tripped: dict[str, bool] = {}
        self.backoff_delays: dict[str, list[float]] = {}
        self._pid_counter = 5000
        self.running = False

    def lock(self, lock_file: Path | None = None):
        """Context manager enforcing single-instance execution via supervisor.lock."""
        target = lock_file or (self.root / "state" / "supervisor.lock")
        return single_instance(target)

    def spawn(self, name: str, cmd: list[str], env: dict[str, str] | None = None) -> Any:
        """Spawn a managed child worker process."""
        worker_env = dict(self.env)
        if env:
            worker_env.update(env)

        if self.spawn_fn is not None:
            handle = self.spawn_fn(name, cmd, worker_env)
        else:
            self._pid_counter += 1
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            proc = subprocess.Popen(
                cmd,
                env=worker_env,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            handle = ProcessHandle(proc.pid, cmd, worker_env, proc)

        self.workers[name] = handle
        return handle

    def setup_default_workers(
        self,
        config: Settings | None = None,
        python_bin: str = sys.executable,
    ) -> list[str]:
        """Spawn the standard set of 4 managed worker sub-services."""
        cfg = config
        is_live = cfg.is_live if cfg is not None else (self.env.get("LIVE_ENABLED", "").lower() == "true")

        if is_live:
            if cfg is None or not (cfg.is_live and cfg.allow_broker_writes):
                raise RuntimeError("Live supervisor entry point not integrated or certified")
            # 1. Dashboard worker
            self.spawn("dashboard", [python_bin, "-m", "swift_autotrader", "paper-dashboard"])
            # 2. Telegram listener worker
            self.spawn("telegram", [python_bin, "-m", "swift_autotrader", "telegram-listen"])
            # 3. Live dispatch worker
            self.spawn(
                "live-dispatch",
                [python_bin, "-m", "swift_autotrader", "live-dispatch", "--root", str(self.root)],
            )
        else:
            # 1. Dashboard worker
            self.spawn("dashboard", [python_bin, "-m", "swift_autotrader", "paper-dashboard"])
            # 2. Telegram listener worker
            self.spawn("telegram", [python_bin, "-m", "swift_autotrader", "telegram-listen"])
            # 3. Paper execution dispatcher worker
            self.spawn(
                "paper-dispatch",
                [python_bin, "-m", "swift_autotrader", "paper-dispatch-service", "--root", str(self.root)],
            )

        # Mobile service is optional for MVP; require a configured paper-mode bot.
        if (cfg is not None and cfg.kill_switch_enabled
                and cfg.kill_switch_telegram_bot_token and cfg.kill_switch_telegram_chat_id):
            self.spawn("kill-switch", [python_bin, "-m", "swift_autotrader", "kill-switch-listener"])

        return list(self.workers.keys())

    def check_workers(
        self,
        now_ts: float | None = None,
        *,
        max_restarts: int | None = None,
        restart_window_seconds: float | None = None,
    ) -> list[str]:
        """Check status of all workers, restarting dead processes with exponential backoff."""
        now = self.now_fn() if now_ts is None else now_ts
        max_r = max_restarts if max_restarts is not None else self.max_restarts
        window = restart_window_seconds if restart_window_seconds is not None else self.restart_window_seconds

        restarted: list[str] = []
        for name, proc in list(self.workers.items()):
            code = proc.poll()
            if code is None:
                continue

            # Voluntary clean exit (code == 0)
            if code == 0:
                continue

            # Non-zero crash
            crashes = self.crash_times.setdefault(name, [])
            crashes = [t for t in crashes if now - t < window]
            crashes.append(now)
            self.crash_times[name] = crashes

            failure_count = self.failures.get(name, 0) + 1
            self.failures[name] = failure_count

            if len(crashes) > max_r:
                self.circuit_tripped[name] = True
                raise RuntimeError(f"Circuit breaker tripped: Repeated worker failure for {name}")

            delay = min(30.0, 2.0 ** min(failure_count, 4))
            self.backoff_delays.setdefault(name, []).append(delay)

            # Respawn worker
            self.workers[name] = self.spawn(name, proc.cmd, proc.env)
            restarted.append(name)

        return restarted

    def write_health_heartbeat(self, state: str = "HEALTHY", as_of_utc: str | None = None) -> None:
        """Atomically write health heartbeat JSON document."""
        # Do not overwrite the installed receiver's authoritative health file.
        health_file = self.root / "state" / "supervisor-health.json"
        health_file.parent.mkdir(parents=True, exist_ok=True)
        if state == "HEALTHY" and (not self.workers or any(p.poll() is not None for p in self.workers.values())):
            state = "DEGRADED"
        doc = {
            "status": state,
            "as_of_utc": as_of_utc or datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "workers": {
                name: (proc.poll() is None) for name, proc in self.workers.items()
            },
            "restart_counts": {
                name: self.failures.get(name, 0) for name in self.workers
            },
        }
        tmp = health_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(health_file)

    def stop_all(self) -> None:
        """Terminate all running worker processes."""
        for proc in self.workers.values():
            if proc.poll() is None:
                proc.terminate()
        self.running = False


def run_supervisor(
    root: Path,
    config: Settings | None = None,
    *,
    once: bool = False,
    poll_interval: float = 1.0,
    supervisor_factory: Callable[..., ServiceSupervisor] | None = None,
) -> None:
    """Run production daemon supervisor under single-instance lock."""
    target_root = Path(root).resolve()
    (target_root / "state").mkdir(parents=True, exist_ok=True)
    lock_file = target_root / "state" / "supervisor.lock"

    with single_instance(lock_file):
        if supervisor_factory is not None:
            supervisor = supervisor_factory(target_root)
        else:
            supervisor = ServiceSupervisor(target_root)

        # Keep a persistent local stop independent of the optional mobile bot.
        if any(os.path.lexists(target_root / "state" / marker)
               for marker in ("stop.request", "SWIFT_REVIEWED_STOP")):
            raise RuntimeError("Persistent local stop is active")
        try:
            if config is None:
                try:
                    config = Settings.from_env()
                except Exception:
                    config = None
            supervisor.setup_default_workers(config=config)
            supervisor.write_health_heartbeat(state="STARTING")
        except Exception:
            supervisor.stop_all()
            supervisor.write_health_heartbeat(state="STOPPED")
            raise

        def handle_stop(signum: int, frame: Any) -> None:
            logger.info("Termination signal %s received. Stopping all workers...", signum)
            supervisor.stop_all()
            supervisor.write_health_heartbeat(state="STOPPED")

        try:
            signal.signal(signal.SIGINT, handle_stop)
            signal.signal(signal.SIGTERM, handle_stop)
        except (ValueError, AttributeError):
            # When running on non-main threads or platforms where signal registration is limited
            pass

        if once:
            try:
                supervisor.check_workers()
            finally:
                supervisor.stop_all()
                supervisor.write_health_heartbeat(state="STOPPED")
            return

        supervisor.running = True
        while supervisor.running:
            try:
                if any(os.path.lexists(target_root / "state" / marker)
                       for marker in ("stop.request", "SWIFT_REVIEWED_STOP")):
                    supervisor.stop_all()
                    supervisor.write_health_heartbeat(state="STOPPED")
                    break
                supervisor.check_workers()
                supervisor.write_health_heartbeat(state="HEALTHY")
            except RuntimeError as exc:
                logger.error("Supervisor circuit breaker tripped: %s", exc)
                supervisor.write_health_heartbeat(state="CRITICAL")
                supervisor.stop_all()
                raise
            time.sleep(poll_interval)

