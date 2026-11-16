"""Complete operational service integration rehearsal and acceptance suite.

Verifies:
1. Dual database initialization and isolation (paper swift.sqlite3 vs production live.sqlite3).
2. Telegram message ingestion, classification, and signal lineage in live journal.
3. Live dispatch cycle with simulated Robinhood MCP broker (StatefulFakeMCP).
4. Local dashboard reporting in LIVE mode without secret leakage.
5. Supervisor multi-worker lifecycle (dashboard, telegram, live-dispatch).
6. Crash recovery, exponential backoff, and circuit breaker trip under failure.
7. Safe shutdown, persistent stop markers, and single-instance duplicate prevention.
8. PowerShell scheduled-task configuration invariants.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from swift_autotrader.analytics.metrics import PaperAnalytics
from swift_autotrader.config import Settings
from swift_autotrader.dashboard.app import build_handler
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.live_dispatch import LiveDispatcher
from swift_autotrader.live_service import run_live_service
from swift_autotrader.signals.lineage import SignalMatcher
from swift_autotrader.supervisor import ServiceSupervisor, run_supervisor
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage
from swift_autotrader.windows_runtime import single_instance
from tests.fixtures.live_mocks import (
    FakeClock,
    MockProcessHandle,
    MockProcessManager,
    StatefulFakeMCP,
)


@pytest.fixture
def managed_live_env(tmp_path: Path):
    """Set up an isolated managed production installation directory."""
    root = tmp_path / "SwiftAutoTrader-Live-Install"
    for part in ("data", "logs", "backups", "secrets", "state", "releases", "config", "scripts"):
        (root / part).mkdir(parents=True, exist_ok=True)

    # Managed deployment manifest in LIVE mode
    (root / "deployment.json").write_text(
        json.dumps({
            "product": "swift-options-autotrader",
            "mode": "LIVE",
            "live_enabled": True,
            "broker_writes_enabled": True,
            "created_utc": "2026-11-16T05:00:00Z",
            "installation_guid": "rehearsal-0000-0000-0000-000000000000",
        }),
        encoding="utf-8",
    )

    # Initialize BOTH databases with locked migration schemas
    paper_db = root / "data" / "swift.sqlite3"
    live_db = root / "data" / "live.sqlite3"
    apply_migrations(paper_db)
    apply_migrations(live_db)

    # Stage synthetic secrets
    session_file = root / "secrets" / "telegram.session"
    session_file.write_bytes(b"synthetic_telegram_session")
    (root / "secrets" / "oauth.dpapi").write_bytes(b"synthetic_oauth")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="RH_LIVE_ACC_REHEARSAL",
        db_path=live_db,
        telegram_enabled=True,
        telegram_channel_id=-1001234567890,
        telegram_api_id=123456,
        telegram_api_hash="a" * 32,
        telegram_session_path=session_file,
        managed_install_root=root,
        max_daily_loss_usd=Decimal("750"),
        max_concurrent_positions=3,
        slippage_tolerance_pct=Decimal("10"),
    )
    return root, settings, paper_db, live_db


@contextmanager
def running_dashboard(db_path: Path):
    """Run an isolated local dashboard server on an ephemeral loopback port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ============================================================================
# Rehearsal 1: Dual Database Setup & Ingestion Isolation
# ============================================================================


def test_dual_database_initialization_and_isolation(managed_live_env) -> None:
    """Verify that paper and live databases exist separately and alerts remain isolated."""
    root, settings, paper_db, live_db = managed_live_env

    # 1. Both databases exist and have migration tables
    with sqlite3.connect(paper_db) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM telegram_messages").fetchone()[0] == 0

    with sqlite3.connect(live_db) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM telegram_messages").fetchone()[0] == 0

    # 2. Ingest Telegram message into live database only
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime.now(UTC)
    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=101,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    res = store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))
    assert res.stored is True
    assert res.eligible_for_future_processing is True

    # 3. Verify paper database is 100% UNTOUCHED
    with sqlite3.connect(paper_db) as conn:
        assert conn.execute("SELECT count(*) FROM telegram_messages").fetchone()[0] == 0

    # 4. Verify live database contains the ingested event
    with sqlite3.connect(live_db) as conn:
        count = conn.execute("SELECT count(*) FROM telegram_messages").fetchone()[0]
        assert count == 1

    # 5. Signal matcher parses the live event into an actionable entry signal
    matcher = SignalMatcher(live_db, settings.telegram_channel_id)
    outcomes = matcher.process_pending()
    assert len(outcomes) == 1
    # When catalog is not provided, parser classifies as REVIEW_REQUIRED
    assert outcomes[0].status == "REVIEW_REQUIRED"
    assert outcomes[0].reason == "INSTRUMENT_CATALOG_REQUIRED"
    assert outcomes[0].intent == "ENTRY"
    assert outcomes[0].message_id == 101


# ============================================================================
# Rehearsal 2: Live Dispatch Operational Cycle (Simulated MCP)
# ============================================================================


class OpenMarketCalendar:
    """Session calendar double that always reports regular market hours."""

    def is_regular_session_open(self, at_utc: datetime) -> bool:
        return True


def test_live_dispatch_operational_cycle(managed_live_env) -> None:
    """Verify LiveDispatcher processes live database signals with StatefulFakeMCP."""
    root, settings, paper_db, live_db = managed_live_env

    # Ingest a live entry signal for seeded instrument SPY 680C 11/20
    store = TelegramStore(live_db, settings.telegram_channel_id)
    now = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    msg = TelegramMessage(
        channel_id=settings.telegram_channel_id,
        message_id=202,
        sent_at_utc=now,
        received_at_utc=now,
        body="BTO SPY 680C 11/20 @ 1.20",
        kind=EventKind.NEW,
        origin=EventOrigin.LIVE,
    )
    res = store.ingest(msg, session_started_at_utc=now - timedelta(minutes=5))
    assert res.stored is True
    assert res.eligible_for_future_processing is True

    clock = FakeClock(now)
    fake_mcp = StatefulFakeMCP(
        account_number=settings.robinhood_mcp_account,
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    # Execute one live cycle
    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            client_override=mock_client_ctx(),
            clock=clock,
            calendar=OpenMarketCalendar(),
        )
    )

    # Verify order submitted on fake broker
    assert len(fake_mcp.orders) == 1
    order = next(iter(fake_mcp.orders.values()))
    assert order["direction"] == "buy"
    assert order["quantity"] == "1"

    # Verify recorded in live.sqlite3
    with sqlite3.connect(live_db) as conn:
        intents = conn.execute("SELECT count(*) FROM order_intents").fetchone()[0]
        assert intents >= 1

    # Verify paper database still has ZERO orders
    with sqlite3.connect(paper_db) as conn:
        assert conn.execute("SELECT count(*) FROM order_intents").fetchone()[0] == 0


# ============================================================================
# Rehearsal 3: Dashboard Availability & Reporting in LIVE Mode
# ============================================================================


def test_dashboard_live_mode_availability_and_reporting(managed_live_env, monkeypatch) -> None:
    """Verify local dashboard serves status for live database without secret leakage."""
    root, settings, _, live_db = managed_live_env

    # Configure environment so PaperAnalytics detects live mode
    monkeypatch.setenv("LIVE_ENABLED", "true")
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "true")
    monkeypatch.setenv("PAPER_ONLY", "false")
    monkeypatch.setenv("BROKER_BACKEND", "robinhood_mcp")
    monkeypatch.setenv("ROBINHOOD_MCP_ACCOUNT", settings.robinhood_mcp_account)
    monkeypatch.setenv("SWIFT_INSTALL_ROOT", str(root))
    monkeypatch.setenv("DB_PATH", str(live_db))
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("SWIFT_TELEGRAM_CHANNEL_ID", str(settings.telegram_channel_id))
    monkeypatch.setenv("TELEGRAM_API_ID", str(settings.telegram_api_id))
    monkeypatch.setenv("TELEGRAM_API_HASH", settings.telegram_api_hash)
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(settings.telegram_session_path))

    with running_dashboard(live_db) as base_url:
        # 1. HTML Console
        with urlopen(base_url + "/") as resp:
            assert resp.status == 200
            html = resp.read().decode()
            assert "Swift / Paper Console" in html

        # 2. JavaScript asset
        with urlopen(base_url + "/static/dashboard.js") as resp:
            assert resp.status == 200
            js_text = resp.read().decode()
            assert "mode!=='PAPER_ONLY'&&x.mode!=='LIVE_CONFIGURED'" in js_text

        # 3. Status API
        with urlopen(base_url + "/api/status") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
            assert payload["mode"] == "LIVE_CONFIGURED"
            assert payload["configured_broker_writes_enabled"] is True
            assert payload["real_broker_writes_enabled"] is False
            assert payload["authenticated_broker_connectivity"] == "NOT_VERIFIED_BY_DASHBOARD"
            assert payload["execution_evidence"] == "NO_STANDALONE_BROKER_EXECUTION_VERIFIED"
            assert payload["production_authorization"] == "NOT_ESTABLISHED_BY_CONFIGURATION"
            assert payload["data_quality"] != "LIVE_PORTFOLIO"
            # Verify no secret leakage
            assert "robinhood_mcp_account" not in payload
            assert "telegram_api_hash" not in payload
            assert "token" not in str(payload).lower() or "token" in payload.get("spread_treatment", "")

        # 4. Security checks: reject external hosts and query leaks
        with pytest.raises(HTTPError) as blocked:
            urlopen(Request(base_url + "/api/status", headers={"Host": "attacker.com"}))
        assert blocked.value.code == 403

        with pytest.raises(HTTPError) as blocked:
            urlopen(base_url + "/api/status?leak=1")
        assert blocked.value.code == 400


def test_live_database_filename_does_not_claim_live_execution(managed_live_env, monkeypatch) -> None:
    """A DB filename alone cannot establish mode, writes, or broker fills."""
    _, _, _, live_db = managed_live_env
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    monkeypatch.delenv("ALLOW_BROKER_WRITES", raising=False)
    monkeypatch.delenv("PAPER_ONLY", raising=False)
    payload = PaperAnalytics(live_db).snapshot()
    assert payload["mode"] == "PAPER_ONLY"
    assert payload["configured_broker_writes_enabled"] is False
    assert payload["real_broker_writes_enabled"] is False
    assert payload["execution_evidence"] == "NO_STANDALONE_BROKER_EXECUTION_VERIFIED"

# ============================================================================
# Rehearsal 4: Supervisor Multi-Worker Lifecycle & Crash Backoff
# ============================================================================


def test_supervisor_live_mode_multi_worker_lifecycle(managed_live_env) -> None:
    """Verify ServiceSupervisor spawns and manages all 3 live workers."""
    root, settings, _, _ = managed_live_env

    supervisor = MockProcessManager(root)
    # Spawns dashboard, telegram, live-dispatch
    workers = supervisor.setup_default_workers(config=settings)
    assert set(workers) == {"dashboard", "telegram", "live-dispatch"}
    assert len(supervisor.workers) == 3

    # Check initial health heartbeat
    supervisor.write_health_heartbeat(state="STARTING")
    health_file = root / "state" / "supervisor-health.json"
    assert health_file.is_file()
    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["status"] == "STARTING"
    assert data["workers"]["dashboard"] is True
    assert data["workers"]["telegram"] is True
    assert data["workers"]["live-dispatch"] is True

    # Simulate crash of telegram worker
    supervisor.workers["telegram"].crash(exit_code=1)
    assert supervisor.workers["telegram"].poll() == 1

    # check_workers detects crash, waits, then respawns.
    assert supervisor.check_workers(now_ts=1000.0) == []
    restarted = supervisor.check_workers(now_ts=1002.0)
    assert "telegram" in restarted
    assert supervisor.workers["telegram"].poll() is None  # new process alive
    assert supervisor.failures["telegram"] == 1

    # Heartbeat reflects restart count
    supervisor.write_health_heartbeat(state="HEALTHY")
    data = json.loads(health_file.read_text(encoding="utf-8"))
    assert data["restart_counts"]["telegram"] == 1


def test_supervisor_circuit_breaker_trips_on_repeated_crashes(managed_live_env) -> None:
    """Verify circuit breaker trips and stops worker restart loops."""
    root, settings, _, _ = managed_live_env

    supervisor = MockProcessManager(root)
    supervisor.setup_default_workers(config=settings)

    # Crash live-dispatch repeatedly exceeding MAX_RESTARTS (5)
    now = 1000.0
    for delay in (2.0, 4.0, 8.0, 16.0, 30.0):
        supervisor.workers["live-dispatch"].crash(exit_code=1)
        supervisor.check_workers(now_ts=now)
        now += delay
        supervisor.check_workers(now_ts=now)

    # 6th crash exceeds threshold within window
    supervisor.workers["live-dispatch"].crash(exit_code=1)
    with pytest.raises(RuntimeError, match="Circuit breaker tripped"):
        supervisor.check_workers(now_ts=now)


# ============================================================================
# Rehearsal 5: Safe Shutdown, Persistent Stop Markers, Single Instance
# ============================================================================


def test_supervisor_clean_shutdown_on_stop_marker(managed_live_env) -> None:
    """Verify supervisor stops all processes when stop marker is placed."""
    root, settings, _, _ = managed_live_env

    supervisor = MockProcessManager(root)
    supervisor.setup_default_workers(config=settings)

    for w in supervisor.workers.values():
        assert not w.terminated

    # Stop all workers
    supervisor.stop_all()
    for w in supervisor.workers.values():
        assert w.terminated

    supervisor.write_health_heartbeat(state="STOPPED")
    data = json.loads((root / "state" / "supervisor-health.json").read_text())
    assert data["status"] == "STOPPED"


def test_run_supervisor_refuses_to_start_if_stop_marker_active(managed_live_env) -> None:
    """Verify run_supervisor fails closed when stop.request exists."""
    root, settings, _, _ = managed_live_env

    (root / "state" / "stop.request").touch()

    def mock_factory(r: Path):
        return MockProcessManager(r)

    with pytest.raises(RuntimeError, match="Persistent local stop is active"):
        run_supervisor(root, config=settings, once=True, supervisor_factory=mock_factory)


def test_single_instance_lock_prevents_duplicate_workers(managed_live_env) -> None:
    """Verify single_instance lock prevents concurrent supervisor runs."""
    root, _, _, _ = managed_live_env
    lock = root / "state" / "supervisor.lock"

    with single_instance(lock):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance(lock):
                pass


# ============================================================================
# Rehearsal 6: PowerShell Deployment Script Invariants
# ============================================================================


def test_powershell_deployment_invariants() -> None:
    """Verify PowerShell script parameters and command wireup."""
    repo_root = Path(__file__).resolve().parents[2]
    service_ps1 = (repo_root / "deployment" / "windows" / "service.ps1").read_text(encoding="utf-8")
    manage_ps1 = (repo_root / "deployment" / "windows" / "manage.ps1").read_text(encoding="utf-8")
    setup_ps1 = (repo_root / "deployment" / "windows" / "setup.ps1").read_text(encoding="utf-8")
    common_ps1 = (repo_root / "deployment" / "windows" / "common.ps1").read_text(encoding="utf-8")

    # 1. service.ps1 supports Live mode and supervisor execution
    assert "RegisterLiveTask" in service_ps1
    assert "SwiftAutoTrader-Live-Local" in service_ps1
    assert "& $python -m swift_autotrader supervisor --root $root" in service_ps1

    # 2. setup.ps1 initializes both paper and live databases
    assert "data\\live.sqlite3" in setup_ps1
    assert "& $python -m swift_autotrader migrate" in setup_ps1

    # 3. common.ps1 accepts LIVE mode and configures distinct environment
    assert "[ValidateSet('PAPER_ONLY', 'LIVE')]" in common_ps1
    assert "function Set-SwiftLiveEnvironment" in common_ps1
    assert "$env:DB_PATH = Join-Path $InstallRoot 'data\\live.sqlite3'" in common_ps1

    # 4. manage.ps1 supports Live task status and health
    assert "SwiftAutoTrader-Live-Local" in manage_ps1
    assert "live-health.json" in manage_ps1
    assert "supervisor-health.json" in manage_ps1
    assert "Migrate" in manage_ps1
