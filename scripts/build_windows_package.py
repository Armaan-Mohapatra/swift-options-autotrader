"""Build and independently verify a secrets-free, reproducible deployment ZIP.

Never include git metadata, local logs, source histories, paid chat content,
credential files, runtime databases, build caches or arbitrary repo contents.
A PACKAGE is not a production release without successful CI and owner review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ALLOWED_TOP_FILES = (
    "pyproject.toml", "uv.lock", "README.md", "AGENTS.md", "CHANGELOG.md", ".python-version",
)
ALLOWED_DOCS = (
    "docs/PROJECT_SPEC.md", "docs/SOURCE_SPRINT_PLAN.md", "docs/ARCHITECTURE.md",
    "docs/WINDOWS_RUNBOOK.md", "docs/OPERATIONS.md", "docs/ROLLBACK.md",
    "docs/SECURITY.md", "docs/CONFIGURATION.md",
    "docs/PAPER_EXECUTION_PHASE3.md", "docs/PAPER_ANALYTICS_PHASE5.md",
    "docs/PHASE_REPORTS/PHASE_6.md", "docs/PHASE_REPORTS/PHASE_7.md",
    "docs/PHASE_REPORTS/PHASE_8.md", "docs/PAPER_DISPATCH_PHASE8.md",
    "docs/PRE_CODEX_HANDOFF.md", "docs/IMPLEMENTATION_STATUS.md",
)
ALLOWED_SCRIPT = (
    "scripts/audit_lock.py", "scripts/scan_secrets.py",
    "scripts/build_windows_package.py", "scripts/phase7_verify.py",
)


def collect_sources(source: Path) -> dict[str, bytes]:
    source = source.resolve()
    selected = [*ALLOWED_TOP_FILES, *ALLOWED_DOCS, *ALLOWED_SCRIPT]
    for directory, suffixes in (
        ("src", {".py", ".sql"}), ("deployment/windows", {".ps1", ".md", ".py"}),
    ):
        folder = source / directory
        if not folder.is_dir():
            raise RuntimeError(f"Missing deployment source directory: {directory}")
        selected.extend(str(p.relative_to(source)).replace("\\", "/")
                        for p in sorted(folder.rglob("*"))
                        if p.is_file() and p.suffix in suffixes)
    package: dict[str, bytes] = {}
    for relative in sorted(set(selected)):
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Missing or redirected deployment source file")
        if not path.resolve().is_relative_to(source):
            raise RuntimeError("Source file escapes checkout")
        if path.stat().st_size > 2_000_000:
            raise RuntimeError("Unexpectedly large deployment source")
        package[relative] = path.read_bytes()
    return package


def _commit(source: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Source is not a verified Git checkout") from None
    if not re.fullmatch(r"[0-9a-f]{40}", result):
        raise RuntimeError("Cannot verify full Git source commit")
    try:
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cannot inspect source checkout cleanliness") from exc
    if status.strip():
        raise RuntimeError("Source checkout is dirty or has uncommitted files")
    return result


def create_package(source: Path, output: Path, *, expected_commit: str | None = None,
                   allow_local_test: bool = False) -> dict[str, object]:
    source = Path(source).resolve()
    if allow_local_test:
        if expected_commit is not None:
            raise ValueError("Local uncommitted packages cannot claim a reviewed SHA")
        commit = "LOCAL_UNVERIFIED_TEST"
    else:
        commit = _commit(source)
        if not expected_commit or commit != expected_commit:
            raise RuntimeError("Checkout differs from expected, reviewed full SHA")
    sources = collect_sources(source)
    if not allow_local_test:
        # An ignored .py/.sql/.ps1 may be picked up by collect_sources even
        # when Git considers the worktree clean. Only HEAD-tracked payloads
        # may claim the approved source SHA.
        try:
            indexed = subprocess.run(
                ["git", "-C", str(source), "ls-files", "--cached", "-z"],
                capture_output=True, check=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Cannot verify tracked release source files") from exc
        tracked = {p.decode("utf-8") for p in indexed.split(b"\0") if p}
        if set(sources) - tracked:
            raise RuntimeError("Release source includes untracked or ignored files")
        # Git's clean status is based on normalized content. On Windows a
        # CRLF checkout can be clean while its bytes differ from HEAD blobs.
        # A release claiming a reviewed commit MUST contain exactly its blobs.
        for relative, payload in sources.items():
            try:
                committed = subprocess.run(
                    ["git", "-C", str(source), "show", f"HEAD:{relative}"],
                    capture_output=True, check=True, timeout=10,
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("Cannot verify committed release blob") from exc
            if payload != committed:
                raise RuntimeError(
                    f"Release source bytes differ from committed Git blob: {relative}"
                )
    version = tomllib.loads(sources["pyproject.toml"].decode("utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise RuntimeError("Unsupported release package version")
    manifest: dict[str, object] = {
        "project": "swift-options-autotrader", "phase": 6,
        "mode": "PAPER_ONLY", "live_enabled": False,
        "broker_writes_enabled": False, "source_commit": commit,
        "package_version": version,
        "files": {p: hashlib.sha256(raw).hexdigest() for p, raw in sources.items()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as bundle:
        for name, raw in sorted(sources.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, raw)
        info = zipfile.ZipInfo("PACKAGE_MANIFEST.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        bundle.writestr(info, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verify_package(output)
    return manifest


def verify_package(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as bundle:
        members = bundle.namelist()
        if len(set(members)) != len(members):
            raise RuntimeError("Archive contains duplicate member paths")
        for name in members:
            key = PurePosixPath(name)
            if key.is_absolute() or ".." in key.parts or "\\" in name:
                raise RuntimeError("Unsafe archive member name")
        if "PACKAGE_MANIFEST.json" not in members:
            raise RuntimeError("Package manifest is absent")
        manifest = json.loads(bundle.read("PACKAGE_MANIFEST.json"))
        if manifest.get("mode") != "PAPER_ONLY" or manifest.get("live_enabled") is not False:
            raise RuntimeError("Unsafe package trading mode")
        if manifest.get("broker_writes_enabled") is not False:
            raise RuntimeError("Unsafe brokerage package")
        hashes = manifest.get("files")
        if "pyproject.toml" not in members:
            raise RuntimeError("Release has no versioned pyproject.toml")
        if not isinstance(hashes, dict) or set(members) != set(hashes) | {
            "PACKAGE_MANIFEST.json"
        }:
            raise RuntimeError("Unmanifested or missing package file")
        for name, expected in hashes.items():
            if hashlib.sha256(bundle.read(name)).hexdigest() != expected:
                raise RuntimeError("Package file hash mismatch")
        try:
            version = (
                tomllib.loads(bundle.read("pyproject.toml").decode("utf-8"))
                ["project"]["version"]
            )
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise RuntimeError("Release has unreadable package version") from exc
        if manifest.get("package_version") != version:
            raise RuntimeError("Release manifest package version mismatch")
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--local-test-only", action="store_true")
    args = parser.parse_args()
    manifest = create_package(
        args.source, args.output, expected_commit=args.expected_commit,
        allow_local_test=args.local_test_only,
    )
    print(json.dumps({"status": "PACKAGE_CREATED", "source_commit": manifest["source_commit"],
                      "file_count": len(manifest["files"]), "mode": "PAPER_ONLY"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
