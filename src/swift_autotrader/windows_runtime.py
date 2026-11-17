"""Phase 6 Windows PAPER-ONLY process supervisor and SQLite recovery tools.

Does not parse/replay historical alerts into live decisions or call any brokerage.
The service only runs the local read-only dashboard, optional authorized
receive-only Telegram listener, and local notification-outbox scanning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from swift_autotrader.config import Settings

MANIFEST_NAME = "deployment.json"
HEARTBEAT_MAX_SECONDS = 45
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 600


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def manifest_mode(document: dict) -> str | None:
    """PAPER_ONLY (no live, no writes) or LIVE (live and writes); any mix is invalid."""
    if document.get("product") != "swift-options-autotrader":
        return None
    live, writes = document.get("live_enabled"), document.get("broker_writes_enabled")
    if document.get("mode") == "PAPER_ONLY" and live is False and writes is False:
        return "PAPER_ONLY"
    if document.get("mode") == "LIVE" and live is True and writes is True:
        return "LIVE"
    return None


def install_mode(root: Path) -> str:
    return manifest_mode(json.loads((Path(root) / MANIFEST_NAME).read_text(encoding="utf-8"))) \
        or "PAPER_ONLY"


def validate_install_root(root: Path) -> Path:
    """Reject arbitrary paths: commands may affect only manifest-owned install."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("Swift install directory not present")
    marker = root / MANIFEST_NAME
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError("Managed installation marker is missing")
    document = json.loads(marker.read_text(encoding="utf-8"))
    if manifest_mode(document) is None:
        raise RuntimeError("Unsafe or unknown installation manifest")
    for part in ("data", "logs", "backups", "secrets", "state", "releases"):
        location = root / part
        if location.is_symlink() or not location.is_dir():
            raise RuntimeError("Missing or redirected managed directory")
    return root


@contextmanager
def single_instance(lock_file: Path) -> Iterator[None]:
    """Platform-native nonblocking lock, released by the OS on process death."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() < 1:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Swift service already running") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        elif os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Swift service already running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        else:
            raise RuntimeError("Unsupported host locking platform")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")


def backup_database(root: Path, *, keep: int = 7, target: Path | None = None) -> Path:
    """Online-consistent snapshot using sqlite3.backup; never copy a live WAL file."""
    root = validate_install_root(root)
    if not 1 <= keep <= 100:
        raise ValueError("Invalid backup retention")
    db = root / "data" / "swift.sqlite3"
    if not db.is_file():
        raise RuntimeError("No initialized SQLite database to back up")
    backup_dir = root / "backups"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = target or backup_dir / f"swift-{stamp}.sqlite3"
    if target.parent.resolve() != backup_dir.resolve() or target.exists():
        raise RuntimeError("Backup target must be a new file in managed backup directory")
    partial = target.with_suffix(".partial")
    try:
        with (
            closing(sqlite3.connect(
                f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10
            )) as source,
            closing(sqlite3.connect(partial, timeout=10)) as destination,
        ):
            source.backup(destination, pages=128, sleep=0.01)
        _integrity(partial)
        os.replace(partial, target)
        _atomic_json(target.with_suffix(".sha256.json"), {
            "sha256": _sha256(target), "created_utc": _now(),
            "format": "SQLite backup API", "paper_only": True,
        })
        candidates = sorted(backup_dir.glob("swift-*.sqlite3"), reverse=True)
        for old in candidates[keep:]:
            old.unlink()
            old.with_suffix(".sha256.json").unlink(missing_ok=True)
        return target
    finally:
        partial.unlink(missing_ok=True)


def restore_database(root: Path, backup: Path, *, confirm_offline: bool = False) -> None:
    """Refuse ambiguous live WAL state; only restore a verified stopped database."""
    root = validate_install_root(root)
    if not confirm_offline:
        raise PermissionError("Restore requires explicit stopped-service confirmation")
    backup = Path(backup).resolve()
    if backup.parent != (root / "backups").resolve() or not backup.is_file():
        raise RuntimeError("Backup is not a file in the managed backup directory")
    if not backup.name.startswith("swift-") or backup.suffix != ".sqlite3":
        raise RuntimeError("Unexpected backup filename")
    evidence = backup.with_suffix(".sha256.json")
    if not evidence.is_file() or json.loads(evidence.read_text(encoding="utf-8")).get(
        "sha256"
    ) != _sha256(backup):
        raise RuntimeError("Backup hash evidence missing or changed")
    _integrity(backup)
    db = root / "data" / "swift.sqlite3"
    # No attempt to silently delete or discard outstanding WAL transactions.
    if any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise RuntimeError("WAL sidecars present; inspect and checkpoint safely offline")
    if (root / "state" / "service.lock").is_symlink():
        raise RuntimeError("Unsafe lock path")
    with single_instance(root / "state" / "service.lock"):
        # Preserve existing database as an extra consistent backup before replacement.
        if db.is_file():
            backup_database(root, keep=100)
        staged = db.with_suffix(".restore.tmp")
        try:
            with (
                closing(sqlite3.connect(
                    f"file:{backup.as_posix()}?mode=ro", uri=True
                )) as source,
                closing(sqlite3.connect(staged)) as dest,
            ):
                source.backup(dest)
            _integrity(staged)
            os.replace(staged, db)
        finally:
            staged.unlink(missing_ok=True)


def _logger(root: Path) -> logging.Logger:
    log = logging.getLogger("swift_autotrader.windows")
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = RotatingFileHandler(
            root / "logs" / "supervisor.log", maxBytes=2 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.propagate = False
    return log


@dataclass(slots=True)
class Child:
    name: str
    args: tuple[str, ...]
    process: subprocess.Popen[bytes] | None = None
    failures: int = 0
    last_restart: float = 0.0


class PaperSupervisor:
    """Watch read-only workers; do not start parser or order-engine cycles.

    A Phase 4 verified, approved contract catalog is not yet wired into the
    local paper pipeline. Starting parse-replay without it could permanently
    classify new source alerts as REVIEW_REQUIRED. Never do that silently.
    """

    def __init__(
        self, root: Path, *, env: dict[str, str] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.root = validate_install_root(root)
        self.env = dict(os.environ if env is None else env)
        settings = Settings.from_env(self.env)
        settings.validate()
        if settings.live_mode != (install_mode(self.root) == "LIVE"):
            raise RuntimeError("Install manifest and settings disagree about live mode")
        if settings.db_path.resolve() != (self.root / "data" / "swift.sqlite3").resolve():
            raise RuntimeError("DB_PATH must point at isolated managed database")
        if not settings.db_path.is_file():
            raise RuntimeError("Database has not been initialized")
        self.now, self.sleep, self.spawn = now, sleep, spawn
        self.log = _logger(self.root)
        self.stop_requested = False
        self.stop_marker = self.root / "state" / "stop.request"
        self.children = [Child("dashboard", (
            "paper-dashboard", "--host", "127.0.0.1", "--port", "8765",
        ))]
        if settings.telegram_enabled:
            self.children.append(Child("telegram", ("telegram-listen",)))
        if self.env.get("PAPER_DISPATCH_ENABLED") == "true":
            if not settings.telegram_enabled or self.env.get(
                "PAPER_MARKET_DATA_SOURCE"
            ) != "robinhood_mcp_readonly":
                raise RuntimeError("PAPER dispatch needs authorized Telegram and read-only MCP")
            self.children.append(Child("paper-dispatch", (
                "paper-dispatch-service", "--root", str(self.root),
            )))
        self._fail_times: list[float] = []
        self._threads: list[threading.Thread] = []
        self._last_backup_at: float | None = None

    def _pump(self, child: Child, process: subprocess.Popen[bytes]) -> None:
        # Never log raw child lines: paid Telegram text or secrets could appear.
        # A crash is reflected by a code, not a raw exception body.
        assert process.stdout is not None
        for _ in process.stdout:
            pass

    def _start(self, child: Child) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        # Never expose status bot tokens to child processes that do not need them.
        child_env = {key: value for key, value in self.env.items()
                     if key not in {"TELEGRAM_STATUS_BOT_TOKEN", "TELEGRAM_STATUS_CHAT_ID"}}
        if child.name != "telegram":
            for key in ("TELEGRAM_API_HASH", "TELEGRAM_API_ID",
                        "TELEGRAM_SESSION_PATH"):
                child_env.pop(key, None)
            # The paper-dispatch worker needs the channel ID only; it does NOT
            # authenticate to Telegram or need MTProto user secrets.
            child_env["TELEGRAM_ENABLED"] = "false"
            if child.name != "paper-dispatch":
                child_env.pop("SWIFT_TELEGRAM_CHANNEL_ID", None)
                child_env.pop("PAPER_DISPATCH_ENABLED", None)
                child_env.pop("PAPER_MARKET_DATA_SOURCE", None)
        child.process = self.spawn(
            [sys.executable, "-m", "swift_autotrader", *child.args],
            env=child_env, cwd=str(self.root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=flags,
        )
        thread = threading.Thread(target=self._pump, args=(child, child.process),
                                  daemon=True)
        thread.start()
        self._threads.append(thread)
        self.log.info("worker_started kind=%s", child.name)

    def _stop_child(self, child: Child) -> None:
        proc = child.process
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)
        self.log.info("worker_stopped kind=%s", child.name)

    def _heartbeat(self, state: str) -> None:
        _atomic_json(self.root / "state" / "health.json", {
            "status": state, "as_of_utc": _now(), "pid": os.getpid(),
            "mode": (mode := install_mode(self.root)), "live_enabled": mode == "LIVE",
            "broker_writes_enabled": mode == "LIVE",
            "automated_paper_orders": False,
            "paper_dispatch_worker_enabled": any(c.name == "paper-dispatch" for c in self.children),
            "workers": {c.name: c.process is not None and c.process.poll() is None
                        for c in self.children},
        })

    def _check_workers(self) -> None:
        for child in self.children:
            proc = child.process
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                self.log.error("worker_crashed kind=%s", child.name)
                self._fail_times = [t for t in self._fail_times
                                    if self.now() - t < RESTART_WINDOW_SECONDS]
                self._fail_times.append(self.now())
                if len(self._fail_times) > MAX_RESTARTS:
                    raise RuntimeError("Repeated worker failure: manual review required")
                child.failures += 1
                self.sleep(min(30, 2**min(child.failures, 4)))
            self._start(child)

    def _daily_backup(self) -> None:
        if self._last_backup_at is not None and self.now() - self._last_backup_at < 86400:
            return
        try:
            backup_database(self.root, keep=7)
            self._last_backup_at = self.now()
            self.log.info("daily_sqlite_backup_complete")
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            self.log.error("daily_sqlite_backup_failed")
            try:
                from swift_autotrader.notifications.alerts import NotificationCenter
                NotificationCenter(self.root / "data" / "swift.sqlite3").record(
                    "APPLICATION", "BACKUP_FAILED", source_key="daily-backup",
                )
            except (OSError, sqlite3.Error):
                pass

    def _notify_scan(self) -> None:
        try:
            scan_env = {key: value for key, value in self.env.items()
                        if key not in {"TELEGRAM_STATUS_BOT_TOKEN",
                                       "TELEGRAM_API_HASH", "TELEGRAM_SESSION_PATH"}}
            result = subprocess.run(
                [sys.executable, "-m", "swift_autotrader", "paper-notify-scan"],
                cwd=self.root, env=scan_env, capture_output=True, timeout=10,
                check=False,
            )
            if result.returncode != 0:
                self.log.error("local_notification_scan_failed")
            if self.env.get("ENABLE_TELEGRAM_STATUS_NOTIFICATIONS") == "true":
                from swift_autotrader.windows_status_notifications import (
                    deliver_status_notifications,
                )
                deliver_status_notifications(self.root, env=self.env)
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            self.log.error("local_notification_scan_or_delivery_unavailable")

    def run(self, *, max_cycles: int | None = None) -> None:
        with single_instance(self.root / "state" / "service.lock"):
            self.stop_marker.unlink(missing_ok=True)
            failed = False
            try:
                for c in self.children:
                    self._start(c)
                cycles = 0
                while not self.stop_requested and not self.stop_marker.exists():
                    self._check_workers()
                    if cycles % 12 == 0:
                        self._notify_scan()
                        self._daily_backup()
                    self._heartbeat("RUNNING")
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    self.sleep(5)
            except BaseException:
                failed = True
                self._heartbeat("FAILED")
                raise
            finally:
                for c in reversed(self.children):
                    self._stop_child(c)
                self._heartbeat("FAILED" if failed else "STOPPED")
                self.stop_marker.unlink(missing_ok=True)


def service_status(root: Path, *, now: datetime | None = None) -> dict[str, object]:
    root = validate_install_root(root)
    health = root / "state" / "health.json"
    if not health.is_file():
        return {"status": "NOT_STARTED", "mode": install_mode(root)}
    record = json.loads(health.read_text(encoding="utf-8"))
    timestamp = datetime.fromisoformat(str(record["as_of_utc"]))
    if timestamp.tzinfo is None:
        raise RuntimeError("Heartbeat timestamp must carry UTC offset")
    current = now or datetime.now(UTC)
    age = (current - timestamp).total_seconds()
    if record.get("status") == "RUNNING" and (age < 0 or age > HEARTBEAT_MAX_SECONDS):
        record["status"] = "STALE"
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swift-windows-runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("service", "status", "backup", "restore"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--root", type=Path, required=True)
        if name == "restore":
            cmd.add_argument("--backup", type=Path, required=True)
            cmd.add_argument("--confirm-offline", action="store_true")
        if name == "backup":
            cmd.add_argument("--keep", type=int, default=7)
    opts = parser.parse_args(argv)
    try:
        if opts.command == "service":
            supervisor = PaperSupervisor(opts.root)
            def request_stop(_signum: int, _frame: Any) -> None:
                supervisor.stop_requested = True
            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            supervisor.run()
            result: dict[str, object] = {"status": "STOPPED"}
        elif opts.command == "backup":
            saved = backup_database(opts.root, keep=opts.keep)
            result = {"status": "BACKED_UP", "filename": saved.name}
        elif opts.command == "restore":
            restore_database(opts.root, opts.backup, confirm_offline=opts.confirm_offline)
            result = {"status": "RESTORED"}
        else:
            result = service_status(opts.root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "ERROR", "reason": type(exc).__name__}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
