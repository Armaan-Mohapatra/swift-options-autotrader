"""Independent regression: release ZIP must contain only committed source.

Synthetic temporary Git repositories only. No network, accounts, or deployment.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

BUILDER_PATH = Path(__file__).resolve().parents[2] / "scripts/build_windows_package.py"
spec = importlib.util.spec_from_file_location("swift_release_builder", BUILDER_PATH)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
    ).strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "checkout"
    root.mkdir()
    required = (
        *builder.ALLOWED_TOP_FILES,
        *builder.ALLOWED_DOCS,
        *builder.ALLOWED_SCRIPT,
        "src/swift_autotrader/core.py",
        "deployment/windows/setup.ps1",
        "deployment/windows/common.ps1",
        "deployment/windows/manage.ps1",
        "deployment/windows/service.ps1",
        "deployment/windows/secrets.ps1",
        "deployment/windows/healthcheck.ps1",
        "deployment/windows/extract_approved_package.py",
    )
    for name in required:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Use exact LF bytes; Windows text-mode writes would silently add CRLF.
        target.write_bytes((
            '[project]\nversion = "0.1.0"\n'
            if name == "pyproject.toml" else "reviewed code\n"
        ).encode("utf-8"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "audit@example.test")
    _git(root, "config", "user.name", "Independent Release Audit")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "approved baseline")
    return root, _git(root, "rev-parse", "HEAD")


def test_clean_reviewed_checkout_creates_bound_release(tmp_path: Path):
    root, sha = _fixture_repo(tmp_path)
    manifest = builder.create_package(root, tmp_path / "release.zip", expected_commit=sha)
    assert manifest["source_commit"] == sha
    assert manifest["broker_writes_enabled"] is False


def test_clean_windows_crlf_checkout_packages_exact_reviewed_git_blobs(tmp_path: Path):
    import zipfile

    root, sha = _fixture_repo(tmp_path)
    _git(root, "config", "core.autocrlf", "true")
    # The worktree differs byte-for-byte, but Git's clean filter maps it
    # back to the exact reviewed LF blob. Do not package the CRLF variant.
    file = root / ".python-version"
    file.write_bytes(b"reviewed code\r\n")
    # Refresh the index as a real clean checkout would. Without this, Git
    # notices the manually written worktree file as a cached modification.
    _git(root, "reset", "--hard", "HEAD")
    assert file.read_bytes() == b"reviewed code\r\n"
    assert _git(root, "status", "--porcelain") == ""
    archive = tmp_path / "windows-clean.zip"
    manifest = builder.create_package(root, archive, expected_commit=sha)
    assert manifest["source_commit"] == sha
    with zipfile.ZipFile(archive) as result:
        assert result.read(".python-version") == b"reviewed code\n"


def test_dirty_tracked_code_cannot_claim_reviewed_commit(tmp_path: Path):
    root, sha = _fixture_repo(tmp_path)
    (root / "src/swift_autotrader/core.py").write_text("uncommitted code\n")
    with pytest.raises(RuntimeError, match="dirty|uncommitted"):
        builder.create_package(root, tmp_path / "release.zip", expected_commit=sha)
    assert not (tmp_path / "release.zip").exists()


def test_untracked_extra_code_cannot_claim_reviewed_commit(tmp_path: Path):
    root, sha = _fixture_repo(tmp_path)
    (root / "src/swift_autotrader/new.py").write_text("untracked code\n")
    with pytest.raises(RuntimeError, match="dirty|uncommitted"):
        builder.create_package(root, tmp_path / "release.zip", expected_commit=sha)
    assert not (tmp_path / "release.zip").exists()


def test_gitignored_code_cannot_be_smuggled_into_release(tmp_path: Path):
    root, _ = _fixture_repo(tmp_path)
    (root / ".gitignore").write_text("src/swift_autotrader/ignored.py\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "reviewed ignore policy")
    sha = _git(root, "rev-parse", "HEAD")
    (root / "src/swift_autotrader/ignored.py").write_text("untracked ignored code\n")
    with pytest.raises(RuntimeError, match="untracked|ignored"):
        builder.create_package(root, tmp_path / "release.zip", expected_commit=sha)
    assert not (tmp_path / "release.zip").exists()


def test_reviewed_zip_round_trips_through_independent_extractor(tmp_path: Path):
    """A real builder output should satisfy the separate installer verifier."""
    import hashlib

    extractor_path = Path(__file__).resolve().parents[2] / (
        "deployment/windows/extract_approved_package.py"
    )
    spec = importlib.util.spec_from_file_location("swift_release_extractor", extractor_path)
    assert spec is not None and spec.loader is not None
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)

    root, sha = _fixture_repo(tmp_path)
    archive = tmp_path / "candidate.zip"
    manifest = builder.create_package(root, archive, expected_commit=sha)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = tmp_path / "extracted"
    assert extractor.extract_approved_package(
        archive, target, expected_sha256=checksum, expected_commit=sha,
    ) == len(manifest["files"])
    assert (target / "src/swift_autotrader/core.py").read_text() == "reviewed code\n"
