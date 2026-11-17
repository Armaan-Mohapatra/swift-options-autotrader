"""Unit tests for standalone live trading service runner and safety gates."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.mcp_local_auth import LocalAuthNotConfigured
from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.live_service import run_live_service, write_live_health
from tests.fixtures.live_mocks import FakeClock, StatefulFakeMCP


@pytest.fixture
def test_env(tmp_path: Path):
    root = tmp_path / "install"
    for part in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / part).mkdir(parents=True, exist_ok=True)

    # Managed installation marker
    (root / "deployment.json").write_text(
        json.dumps({"product": "swift-options-autotrader"}),
        encoding="utf-8",
    )

    db_path = root / "data" / "swift.sqlite3"
    apply_migrations(db_path)

    session_file = root / "secrets" / "telegram.session"
    session_file.write_bytes(b"telegram_session_data")

    settings = Settings(
        paper_only=False,
        live_enabled=True,
        max_premium_per_signal_usd=Decimal("999"),
        max_total_exposure_usd=Decimal("9999"),
        allow_broker_writes=True,
        broker_backend="robinhood_mcp",
        robinhood_mcp_account="live-acc-123",
        db_path=db_path,
        telegram_enabled=True,
        telegram_channel_id=-1001234000001,
        telegram_api_id=12345,
        telegram_api_hash="a" * 32,
        telegram_session_path=session_file,
    )
    return root, settings, db_path


def test_live_service_rejects_paper_only(test_env):
    root, settings, _ = test_env
    bad_settings = dataclasses.replace(settings, paper_only=True)
    with pytest.raises(ConfigurationError):
        asyncio.run(run_live_service(bad_settings, root))


def test_live_service_rejects_disallowed_broker_writes(test_env):
    root, settings, _ = test_env
    bad_settings = dataclasses.replace(settings, allow_broker_writes=False)
    with pytest.raises(ConfigurationError):
        asyncio.run(run_live_service(bad_settings, root))


def test_live_service_rejects_non_robinhood_mcp_backend(test_env):
    root, settings, _ = test_env
    bad_settings = dataclasses.replace(settings, broker_backend="paper")
    with pytest.raises(ConfigurationError, match="Live mode requires broker_backend='robinhood_mcp'"):
        asyncio.run(run_live_service(bad_settings, root))


def test_live_service_rejects_unapproved_managed_root(test_env):
    root, settings, _ = test_env
    (root / "secrets" / "oauth.dpapi").write_bytes(b"synthetic")
    with pytest.raises(LocalAuthNotConfigured, match="not approved for production"):
        asyncio.run(run_live_service(settings, root))
    health = json.loads((root / "state" / "live-health.json").read_text())
    assert health["status"] == "FAILED"
    assert health["broker_writes_enabled"] is False


def test_real_service_rejects_shared_paper_journal(test_env):
    root, settings, _ = test_env
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "LIVE",
        "live_enabled": True, "broker_writes_enabled": True,
    }), encoding="utf-8")
    # Reject before attempting Windows DPAPI, OAuth or brokerage activity.
    with pytest.raises(LocalAuthNotConfigured, match="distinct managed production journal"):
        asyncio.run(run_live_service(settings, root))


def test_live_service_stops_on_stop_marker_at_startup(test_env):
    root, settings, _ = test_env
    (root / "state" / "stop.request").touch()
    with pytest.raises(RuntimeError, match="Persistent stop marker active"):
        asyncio.run(run_live_service(settings, root))


def test_live_service_happy_path_once(test_env):
    root, settings, _ = test_env
    clock = FakeClock(datetime(2026, 11, 16, 15, 30, 0, tzinfo=UTC))
    fake_mcp = StatefulFakeMCP(
        account_number="live-acc-123",
        buying_power=Decimal("10000.00"),
        clock=clock,
    )

    @asynccontextmanager
    async def mock_client_ctx():
        yield fake_mcp

    asyncio.run(
        run_live_service(
            settings,
            root,
            once=True,
            client_override=mock_client_ctx(),
            clock=clock,
        )
    )

    health_file = root / "state" / "live-health.json"
    assert health_file.is_file()
    health_data = json.loads(health_file.read_text(encoding="utf-8"))
    assert health_data["mode"] == "LIVE"
    assert health_data["status"] == "STOPPED"
    assert health_data["live_enabled"] is True
    assert health_data["broker_writes_enabled"] is False
    assert health_data["broker_writes_configured"] is True
    assert health_data["simulation"] is True
    assert health_data["live_certified"] is False


def test_health_does_not_claim_running_before_a_cycle(test_env):
    root, _, _ = test_env
    write_live_health(root, "RUNNING", simulated=True)
    health = json.loads((root / "state" / "live-health.json").read_text())
    assert health["status"] == "STARTING"
    assert health["live_certified"] is False


def test_market_data_failure_is_degraded_not_running(test_env):
    from swift_autotrader.live_dispatch import LiveDispatchResult
    root, _, _ = test_env
    result = LiveDispatchResult(
        status="MARKET_DATA_UNAVAILABLE", parsed_events=0, entry_orders_submitted=0,
        close_orders_submitted=0, cancellations=0, realized_daily_loss_usd=Decimal("0"),
        open_positions_count=0)
    write_live_health(root, "RUNNING", simulated=True, result=result)
    health = json.loads((root / "state" / "live-health.json").read_text())
    assert health["status"] == "DEGRADED"
    assert health["last_dispatch_status"] == "MARKET_DATA_UNAVAILABLE"
    assert health["broker_writes_enabled"] is False


# --- Unrestricted live execution requires an expiring owner receipt -------------

import hashlib  # noqa: E402
from datetime import timedelta  # noqa: E402

from swift_autotrader.orders.unattended_approval import (  # noqa: E402
    UnattendedApprovalError,
    load_unattended_approval,
)

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def _receipt(account: str, **overrides) -> dict:
    doc = {
        "version": 1,
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "issued_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=2)).isoformat(),
        "max_contracts_per_signal": 1,
        "max_premium_per_signal_usd": "999",
        "max_total_exposure_usd": "9999",
        "max_daily_loss_usd": "750",
    }
    doc.update(overrides)
    return doc


def _write(path: Path, doc) -> Path:
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return path


def test_unattended_receipt_accepts_exact_binding(tmp_path, test_env):
    _, settings, _ = test_env
    path = _write(tmp_path / "r.json", _receipt(settings.robinhood_mcp_account))
    approval = load_unattended_approval(path, account=settings.robinhood_mcp_account,
                                        settings=settings, now=NOW)
    assert approval.active(NOW) and not approval.active(NOW + timedelta(days=3))


@pytest.mark.parametrize("change", [
    {"account_sha256": "0" * 64},
    {"version": 2},
    {"expires_at": (NOW - timedelta(seconds=1)).isoformat()},
    {"issued_at": (NOW + timedelta(minutes=5)).isoformat()},
    {"issued_at": (NOW - timedelta(days=1)).isoformat(),
     "expires_at": (NOW + timedelta(days=7)).isoformat()},
    {"expires_at": "2026-11-19T00:00:00"},
    {"max_premium_per_signal_usd": "500"},
    {"max_total_exposure_usd": "1000"},
    {"max_daily_loss_usd": "100"},
    {"max_contracts_per_signal": 0},
    {"max_premium_per_signal_usd": 999},
    {"extra": True},
])
def test_unattended_receipt_rejections(tmp_path, test_env, change):
    _, settings, _ = test_env
    path = _write(tmp_path / "r.json", _receipt(settings.robinhood_mcp_account, **change))
    with pytest.raises(UnattendedApprovalError):
        load_unattended_approval(path, account=settings.robinhood_mcp_account,
                                 settings=settings, now=NOW)


def test_unattended_receipt_rejects_missing_duplicate_and_garbage(tmp_path, test_env):
    _, settings, _ = test_env
    acct = settings.robinhood_mcp_account
    for doc in ('{"version": 1, "version": 1}', "not json", "[]"):
        with pytest.raises(UnattendedApprovalError):
            load_unattended_approval(_write(tmp_path / "r.json", doc), account=acct,
                                     settings=settings, now=NOW)
    with pytest.raises(UnattendedApprovalError, match="not found"):
        load_unattended_approval(tmp_path / "absent.json", account=acct,
                                 settings=settings, now=NOW)


def _production_root(root: Path, settings: Settings, monkeypatch) -> Settings:
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "LIVE",
        "live_enabled": True, "broker_writes_enabled": True,
    }), encoding="utf-8")
    live_db = root / "data" / "live.sqlite3"
    apply_migrations(live_db)
    monkeypatch.setattr("swift_autotrader.live_service.load_pinned_agentic_account",
                        lambda _root: settings.robinhood_mcp_account)
    return dataclasses.replace(settings, db_path=live_db.resolve())


def test_unrestricted_live_refused_without_owner_receipt(test_env, monkeypatch):
    root, settings, _ = test_env
    live = _production_root(root, settings, monkeypatch)
    with pytest.raises(LocalAuthNotConfigured, match="not approved"):
        asyncio.run(run_live_service(live, root, clock=lambda: NOW))
    health = json.loads((root / "state" / "live-health.json").read_text())
    assert health["status"] == "FAILED" and health["broker_writes_enabled"] is False


def test_unrestricted_live_receipt_passes_gate_then_still_needs_oauth(test_env, monkeypatch):
    root, settings, _ = test_env
    live = _production_root(root, settings, monkeypatch)
    _write(root / "secrets" / "unattended-live-approval.json",
           _receipt(settings.robinhood_mcp_account))
    # The approval gate passes; startup still fails closed without enrolled OAuth tokens.
    with pytest.raises(LocalAuthNotConfigured, match="OAuth tokens not found"):
        asyncio.run(run_live_service(live, root, clock=lambda: NOW))


def test_unrestricted_live_receipt_with_looser_config_refused(test_env, monkeypatch):
    root, settings, _ = test_env
    live = dataclasses.replace(_production_root(root, settings, monkeypatch),
                               max_premium_per_signal_usd=Decimal("2000"))
    _write(root / "secrets" / "unattended-live-approval.json",
           _receipt(settings.robinhood_mcp_account))
    with pytest.raises(LocalAuthNotConfigured, match="MAX_PREMIUM_PER_SIGNAL_USD"):
        asyncio.run(run_live_service(live, root, clock=lambda: NOW))
