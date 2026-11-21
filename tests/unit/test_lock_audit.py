"""Offline lock provenance tests; no dependency installation or network calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("swift_lock_audit", ROOT / "scripts/audit_lock.py")
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_reviewed_market_lock_is_fully_hashed():
    assert AUDIT.validate_lock(ROOT / "uv.lock") >= 711


@pytest.mark.parametrize(
    "old,new",
    [
        ('name = "mcp"\nversion = "2.3.0"', 'name = "mcp"\nversion = "2.3.1"'),
        ("https://files.pythonhosted.org/", "https://untrusted.example.invalid/"),
        ('source = { editable = "." }', 'source = { editable = "../production" }'),
    ],
)
def test_changed_registry_version_or_local_source_fails_closed(tmp_path, old, new):
    original = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert old in original
    edited = tmp_path / "uv.lock"
    edited.write_text(original.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError):
        AUDIT.validate_lock(edited)


def test_invalid_artifact_digest_fails_closed(tmp_path):
    original = (ROOT / "uv.lock").read_text(encoding="utf-8")
    pos = original.index('hash = "sha256:')
    start = pos + len('hash = "')
    edited = tmp_path / "uv.lock"
    edited.write_text(original[:start] + "sha256:short" + original[start + 71 :], encoding="utf-8")
    with pytest.raises(ValueError):
        AUDIT.validate_lock(edited)
