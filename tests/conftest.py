"""Shared test fixtures for Swift Options AutoTrader live unattended trading."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for test fixture imports
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP


@pytest.fixture
def fake_clock() -> FakeClock:
    """Deterministic UTC test clock."""
    return FakeClock(datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC))


@pytest.fixture
def mock_mcp() -> StatefulFakeMCP:
    """In-memory stateful MCP double with default chains and instruments."""
    return StatefulFakeMCP(
        account_number="synthetic-account",
        buying_power=Decimal("10000.00"),
    )


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    """Isolated SQLite database with all schema migrations applied."""
    db_file = tmp_path / "data" / "swift.sqlite3"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(db_file)
    return db_file


@pytest.fixture
def live_settings(live_db: Path, tmp_path: Path) -> Settings:
    """Valid live trading Settings instance configured for offline testing."""
    session_file = tmp_path / "secrets" / "protected.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"mock_session_content")
    if hasattr(os, "chmod"):
        os.chmod(session_file, 0o600)

    return Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="synthetic-account",
        db_path=live_db,
        max_daily_loss_usd=Decimal("500"),
        max_concurrent_positions=3,
        kill_switch_enabled=True,
        kill_switch_telegram_bot_token="test_bot_token_secret",
        kill_switch_telegram_chat_id=123456789,
        telegram_enabled=True,
        telegram_channel_id=-1001234567890,
        telegram_api_id=12345,
        telegram_api_hash="a" * 32,
        telegram_session_path=session_file,
    )


@pytest.fixture
def safe_process_spawner() -> Generator[Any, None, None]:
    """Process spawner fixture ensuring zero orphaned processes upon teardown."""
    spawned: list[subprocess.Popen[Any]] = []

    def spawn(cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        if os.name == "nt":
            creationflags = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = creationflags
        else:
            kwargs.setdefault("start_new_session", True)
        proc = subprocess.Popen(cmd, **kwargs)
        spawned.append(proc)
        return proc

    try:
        yield spawn
    finally:
        for p in spawned:
            if p.poll() is None:
                try:
                    if os.name == "nt":
                        p.terminate()
                    else:
                        os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=1.0)
                except Exception:
                    try:
                        if os.name == "nt":
                            p.kill()
                        else:
                            os.killpg(p.pid, signal.SIGKILL)
                        p.wait(timeout=1.0)
                    except Exception:
                        pass
