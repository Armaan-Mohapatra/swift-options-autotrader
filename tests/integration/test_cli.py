import json

from swift_autotrader.app import main


def test_dry_run_is_side_effect_free(monkeypatch, capsys, tmp_path):
    db = tmp_path / "should_not_exist.sqlite3"
    monkeypatch.setenv("DB_PATH", str(db))
    for name in ("PAPER_ONLY", "LIVE_ENABLED", "ALLOW_BROKER_WRITES", "TELEGRAM_ENABLED", "BROKER_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    assert main(["dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["orders_submitted"] == 0
    assert result["broker_writes_attempted"] == 0
    assert not result["parser_implemented"]
    assert not db.exists()


def test_smoke(monkeypatch, capsys):
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    assert main(["smoke"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "PAPER_ONLY"


def test_migrate_is_explicit(monkeypatch, capsys, tmp_path):
    db = tmp_path / "data" / "app.sqlite3"
    monkeypatch.setenv("DB_PATH", str(db))
    assert main(["migrate"]) == 0
    assert db.is_file()
    assert json.loads(capsys.readouterr().out)["applied_versions"] == ["0001_initial.sql"]
    assert main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["applied_versions"] == []


def test_cli_rejects_broker_write_flag(monkeypatch, capsys):
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "true")
    assert main(["dry-run"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"
