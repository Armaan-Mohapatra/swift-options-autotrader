"""Phase 5 commands operate offline and never invoke Robinhood or Telegram."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swift_autotrader.app import main
from swift_autotrader.database.migrate import apply_migrations

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/swift_phase5_replay_synthetic.json"


def test_report_is_read_only_and_allows_missing_prices(monkeypatch, capsys, tmp_path):
    db = tmp_path / "paper.sqlite3"
    apply_migrations(db)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PAPER_ONLY", "true")
    monkeypatch.setenv("LIVE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "false")
    assert main(["paper-report"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["mode"] == "PAPER_ONLY"
    assert r["real_broker_writes_enabled"] is False
    assert r["win_rate"] is None
    assert r["realized_net_usd"] is None


def test_historical_replay_cli_isolated_and_annotated(monkeypatch, capsys, tmp_path):
    db = tmp_path / "paper.sqlite3"  # never created or modified by replay
    monkeypatch.setenv("DB_PATH", str(db))
    assert main(["paper-replay", "--input", str(FIXTURE), "--compare"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["mode"] == "PAPER_ONLY"
    assert [a["scenario"] for a in r["assumptions"]] == [
        "touch", "through", "no_fills"
    ]
    assert not db.exists()


def test_local_notification_scan_cli(monkeypatch, capsys, tmp_path):
    db = tmp_path / "paper.sqlite3"
    apply_migrations(db)
    monkeypatch.setenv("DB_PATH", str(db))
    assert main(["paper-notify-scan"]) == 0
    assert json.loads(capsys.readouterr().out)["notifications_new"] == 0


def test_dashboard_cli_cannot_bind_publicly(monkeypatch, capsys, tmp_path):
    db = tmp_path / "paper.sqlite3"
    apply_migrations(db)
    monkeypatch.setenv("DB_PATH", str(db))
    assert main(["paper-dashboard", "--host", "0.0.0.0"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_dashboard_cli_handler_is_not_called_for_report(monkeypatch, tmp_path):
    db = tmp_path / "paper.sqlite3"
    apply_migrations(db)
    monkeypatch.setenv("DB_PATH", str(db))
    import swift_autotrader.dashboard.app as dashboard
    monkeypatch.setattr(dashboard, "serve_local", lambda *_args, **_kwargs: pytest.fail(
        "report must never start HTTP server"
    ))
    assert main(["paper-report"]) == 0
