"""Independent cross-platform source-vs-committed-blob release regression.

No real repository state, accounts, services or network are involved: each
case uses a disposable local Git test repository.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

BUILDER_PATH = Path(__file__).resolve().parents[2] / "scripts/build_windows_package.py"
spec = importlib.util.spec_from_file_location("builder_from_reviewed_github_blob", BUILDER_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    paths = [
        *module.ALLOWED_TOP_FILES, *module.ALLOWED_DOCS, *module.ALLOWED_SCRIPT,
        "src/swift_autotrader/verified.py",
        "deployment/windows/setup.ps1", "deployment/windows/common.ps1",
        "deployment/windows/manage.ps1", "deployment/windows/service.ps1",
        "deployment/windows/secrets.ps1", "deployment/windows/healthcheck.ps1",
        "deployment/windows/extract_approved_package.py",
    ]
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            b'[project]\nversion="0.1.0"\n'
            if relative == "pyproject.toml" else b"approved\n"
        )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Independent Test")
    _git(root, "config", "user.email", "review@example.test")
    # Index normalizes CRLF even when a worktree is considered completely clean.
    _git(root, "config", "core.autocrlf", "true")
    (root / "src/swift_autotrader/verified.py").write_bytes(b"approved\r\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "approved source")
    return root, _git(root, "rev-parse", "HEAD").decode().strip()


def test_clean_crlf_git_checkout_cannot_claim_different_source_bytes(tmp_path: Path):
    root, sha = _repo(tmp_path)
    relative = "src/swift_autotrader/verified.py"
    committed = _git(root, "show", f"HEAD:{relative}")
    checkout = (root / relative).read_bytes()
    assert committed == b"approved\n"
    assert checkout == b"approved\r\n"
    assert _git(root, "status", "--porcelain") == b""
    with pytest.raises(RuntimeError, match="committed Git blob"):
        module.create_package(
            root, tmp_path / "release.zip", expected_commit=sha,
        )
    assert not (tmp_path / "release.zip").exists()
