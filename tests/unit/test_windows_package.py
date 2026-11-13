"""File integrity tests require no Telegram, Robinhood, Windows or network."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
source = ROOT / "scripts" / "build_windows_package.py"
spec = importlib.util.spec_from_file_location("windows_pkg", source)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def test_package_rejects_unverified_origin_and_missing_sources(tmp_path):
    with pytest.raises((RuntimeError, FileNotFoundError)):
        module.create_package(tmp_path, tmp_path / "bad.zip", expected_commit="0" * 40)


def test_package_excludes_secrets_and_verifies_hashes(tmp_path):
    checkout = tmp_path / "source"
    checkout.mkdir()
    for file in (*module.ALLOWED_TOP_FILES, *module.ALLOWED_DOCS, *module.ALLOWED_SCRIPT):
        p = checkout / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("synthetic test file", encoding="utf-8")
    (checkout / "src/swift_autotrader").mkdir(parents=True)
    (checkout / "src/swift_autotrader/__init__.py").write_text("# safe")
    (checkout / "deployment/windows").mkdir(parents=True)
    (checkout / "deployment/windows/setup.ps1").write_text("# safe")
    (checkout / ".env").write_text("TOKEN=SECRET_DO_NOT_COPY")
    (checkout / "data").mkdir()
    (checkout / "data/exchange.sqlite3").write_bytes(b"SENSITIVE")
    (checkout / "src/swift_autotrader/live.session").write_bytes(b"SECRET")
    target = tmp_path / "package.zip"
    result = module.create_package(checkout, target, allow_local_test=True)
    assert result["source_commit"] == "LOCAL_UNVERIFIED_TEST"
    with zipfile.ZipFile(target) as archive:
        assert "src/swift_autotrader/__init__.py" in archive.namelist()
        assert ".env" not in archive.namelist()
        assert "data/exchange.sqlite3" not in archive.namelist()
        assert "src/swift_autotrader/live.session" not in archive.namelist()
    assert module.verify_package(target) == result


def test_package_cannot_mix_local_test_and_production_sha(tmp_path):
    with pytest.raises(ValueError):
        module.create_package(tmp_path, tmp_path / "bad.zip",
                              expected_commit="0" * 40, allow_local_test=True)


def test_archive_detects_unmanifested_extra_content(tmp_path):
    file = tmp_path / "fake.zip"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("PACKAGE_MANIFEST.json", json.dumps({
            "mode": "PAPER_ONLY", "live_enabled": False,
            "broker_writes_enabled": False, "files": {},
        }))
        archive.writestr("secrets/telegram.session", "secret")
    with pytest.raises(RuntimeError):
        module.verify_package(file)
