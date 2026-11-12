"""Fail-closed check of the phase-scoped universal uv lockfile (no internet calls)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

EXPECTED = {
    "colorama": "0.4.6",
    "iniconfig": "2.3.1",
    "packaging": "26.3",
    "pluggy": "1.7.0",
    "pyaes": "1.6.1",
    "pyasn1": "0.6.5",
    "pygments": "2.21.0",
    "pytest": "9.2.0",
    "rsa": "4.9.1",
    "ruff": "0.16.8",
    "swift-options-autotrader": "0.1.0",
    "telethon": "1.45.0",
}


def validate_lock(path: Path = Path("uv.lock")) -> int:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    if lock.get("version") != 1 or lock.get("requires-python") != ">=3.11":
        raise ValueError("Unexpected lockfile version or Python range")
    packages = lock["package"]
    versions = {p["name"]: p["version"] for p in packages}
    if versions != EXPECTED or len(packages) != len(EXPECTED):
        raise ValueError("Unknown, missing, duplicate or updated locked dependency")
    count = 0
    for package in packages:
        if package["name"] == "swift-options-autotrader":
            if package.get("source", {}).get("editable") != ".":
                raise ValueError("Local package is not editable checkout")
            continue
        if package["source"] != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Unexpected Python package registry")
        artifacts = ([package["sdist"]] if "sdist" in package else []) + package.get(
            "wheels", []
        )
        if not artifacts:
            raise ValueError("Package has no hashed artifacts")
        for artifact in artifacts:
            digest = artifact.get("hash", "")
            url = urlparse(artifact.get("url", ""))
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError("Missing SHA-256 artifact digest")
            if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
                raise ValueError("Untrusted artifact URL")
            count += 1
    if count < 11:
        raise ValueError("Unexpectedly incomplete artifact inventory")
    return count


if __name__ == "__main__":
    print(f"Lock audit passed: {len(EXPECTED)} packages and {validate_lock()} hashed artifacts")
