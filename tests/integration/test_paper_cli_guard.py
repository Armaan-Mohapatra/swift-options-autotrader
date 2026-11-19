"""Phase 3 standalone CLI must not alter stored eligibility without a catalog."""
from __future__ import annotations

import sqlite3

from swift_autotrader.app import main
from swift_autotrader.database.migrate import apply_migrations


def test_unverified_cli_paper_cycle_fails_without_mutating_decisions(
    monkeypatch, capsys, tmp_path,
):
    db_path = tmp_path / "guard.sqlite3"
    apply_migrations(db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.delenv("TELEGRAM_ENABLED", raising=False)
    assert main(["paper-cycle", "--confirm-simulation"]) == 2
    result = capsys.readouterr()
    assert '"status": "ERROR"' in result.err
    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT COUNT(*) FROM paper_engine_decisions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0


def test_paper_cycle_requires_operator_confirmation(monkeypatch, capsys, tmp_path):
    db_path = tmp_path / "guard.sqlite3"
    apply_migrations(db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    assert main(["paper-cycle"]) == 2
    assert '"status": "ERROR"' in capsys.readouterr().err
