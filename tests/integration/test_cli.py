import json

from swift_autotrader.app import main


def test_dry_run_is_side_effect_free(monkeypatch, capsys, tmp_path):
    db = tmp_path / "should_not_exist.sqlite3"
    monkeypatch.setenv("DB_PATH", str(db))
    for name in (
        "PAPER_ONLY", "LIVE_ENABLED", "ALLOW_BROKER_WRITES", "TELEGRAM_ENABLED", "BROKER_BACKEND"
    ):
        monkeypatch.delenv(name, raising=False)
    assert main(["dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["orders_submitted"] == 0
    assert result["broker_writes_attempted"] == 0
    assert result["parser_implemented"]
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
    assert json.loads(capsys.readouterr().out)["applied_versions"] == [
        "0001_initial.sql", "0002_telegram_ingest.sql", "0003_signal_linkage.sql",
    ]
    assert main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["applied_versions"] == []


def test_cli_rejects_broker_write_flag(monkeypatch, capsys):
    monkeypatch.setenv("ALLOW_BROKER_WRITES", "true")
    assert main(["dry-run"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"


def test_health_needs_local_id(monkeypatch, capsys):
    monkeypatch.delenv("SWIFT_TELEGRAM_CHANNEL_ID", raising=False)
    assert main(["telegram-health"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"


def test_health_does_not_create_db(monkeypatch, capsys, tmp_path):
    path = tmp_path / "absent.sqlite3"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("SWIFT_TELEGRAM_CHANNEL_ID", "-1001234567890")
    assert main(["telegram-health"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_INITIALIZED"
    assert not path.exists()


def test_telegram_listen_cannot_run_without_auth(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    assert main(["telegram-listen"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"


def test_parser_health_does_not_connect(monkeypatch, capsys, tmp_path):
    db = tmp_path / "phase2.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SWIFT_TELEGRAM_CHANNEL_ID", "-1001234567890")
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["parse-health"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decisions"]["PENDING"] == 0
    assert result["status"] == "PARSER_HEALTH"


def test_parser_replay_never_connects_or_trades(monkeypatch, capsys, tmp_path):
    db = tmp_path / "phase2.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SWIFT_TELEGRAM_CHANNEL_ID", "-1001234567890")
    assert main(["migrate"]) == 0
    capsys.readouterr()
    assert main(["parse-replay", "--limit", "15"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PARSER_REPLAY_COMPLETE"
    assert result["orders_submitted"] == 0
    assert result["broker_writes_attempted"] == 0
    assert result["catalog_validation"] == "UNAVAILABLE_REQUIRES_PHASE4"


def test_parser_replay_requires_channel(monkeypatch, capsys):
    monkeypatch.delenv("SWIFT_TELEGRAM_CHANNEL_ID", raising=False)
    assert main(["parse-replay"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"
