"""Verify a SHA-pinned, commit-bound PAPER release ZIP before safe extraction.

Only invoked by an explicitly approved local Windows installer, never by CI with
real credentials. Trust anchor is the expected SHA-256 provided out-of-band by
its operator, not a package self-claim. No network, account or broker access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 250
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 35_000_000


def extract_approved_package(
    archive: Path, target: Path, *, expected_sha256: str, expected_commit: str,
) -> int:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Expected archive SHA-256 must be exactly 64 lowercase hex digits")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("Expected source commit must be a full lowercase Git SHA")
    if not archive.is_file() or target.exists():
        raise RuntimeError("Approved archive missing or extraction target already exists")
    if archive.stat().st_size > MAX_TOTAL_BYTES:
        raise RuntimeError("Release archive exceeds safe size")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise RuntimeError("Release ZIP digest does not match the owner-approved SHA-256")
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if (len(infos) > MAX_FILES or
            len({f.filename.casefold() for f in infos}) != len(infos)):
            raise RuntimeError("Release contains too many or duplicate files")
        total = 0
        for info in infos:
            relative = PurePosixPath(info.filename)
            parts = relative.parts
            device = {"CON", "PRN", "AUX", "NUL"}
            device.update(f"COM{i}" for i in range(1, 10))
            device.update(f"LPT{i}" for i in range(1, 10))
            invalid_part = any(
                (not part or part.endswith((" ", ".")) or ":" in part
                 or part.split(".", 1)[0].upper() in device)
                for part in parts
            )
            if (info.is_dir() or relative.is_absolute() or ".." in parts
                or "\\" in info.filename or not parts or invalid_part
                or info.file_size > MAX_FILE_BYTES
                or ((info.external_attr >> 16) & 0o170000) not in {0, 0o100000}):
                raise RuntimeError("Unsafe release archive entry")
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError("Expanded release exceeds safe size")
        if "PACKAGE_MANIFEST.json" not in bundle.namelist():
            raise RuntimeError("Release manifest missing")
        manifest = json.loads(bundle.read("PACKAGE_MANIFEST.json"))
        if (manifest.get("source_commit") != expected_commit
            or manifest.get("project") != "swift-options-autotrader"
            or (manifest.get("mode"), manifest.get("live_enabled"),
                manifest.get("broker_writes_enabled")) not in {
                ("PAPER_ONLY", False, False), ("LIVE", True, True)}):
            raise RuntimeError("Unapproved source commit or unsafe release flags")
        entries = manifest.get("files")
        if not isinstance(entries, dict) or set(entries) != set(bundle.namelist()) - {
            "PACKAGE_MANIFEST.json"
        }:
            raise RuntimeError("Release manifest does not cover every file")
        for info in infos:
            if info.filename == "PACKAGE_MANIFEST.json":
                continue
            expected = entries.get(info.filename)
            if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or hashlib.sha256(bundle.read(info.filename)).hexdigest() != expected):
                raise RuntimeError("Release file integrity verification failed")
        try:
            version = (
                tomllib.loads(bundle.read("pyproject.toml").decode("utf-8"))
                ["project"]["version"]
            )
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Release package version is unreadable") from exc
        if manifest.get("package_version") != version:
            raise RuntimeError("Release package version mismatch")
        required = {"pyproject.toml", "uv.lock", "deployment/windows/setup.ps1",
                    "deployment/windows/common.ps1", "deployment/windows/manage.ps1",
                    "deployment/windows/service.ps1", "deployment/windows/secrets.ps1",
                    "deployment/windows/healthcheck.ps1",
                    "deployment/windows/extract_approved_package.py"}
        if not required <= set(entries):
            raise RuntimeError("Release missing required installation scripts")
        # Only after full verification create output; no ZIP extractall traversal.
        target.mkdir(parents=True, exist_ok=False)
        for info in infos:
            destination = target.joinpath(*PurePosixPath(info.filename).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    number = extract_approved_package(
        args.archive, args.target, expected_sha256=args.sha256,
        expected_commit=args.commit,
    )
    print(json.dumps({"status": "VERIFIED_PAPER_RELEASE", "files": number,
                      "source_commit": args.commit}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
