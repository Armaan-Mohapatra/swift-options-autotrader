"""Fail-closed audit of the reviewed universal uv dependency lock, offline.

A locked transitive dependency is permitted only with its reviewed version,
PyPI-only source, immutable artifact SHA-256 and trusted HTTPS artifact host.
The two numpy versions are intentionally distinct resolver branches for
Python 3.11 versus Python 3.12+; uv lock --check must also pass on each host.
"""
from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

# Frozen from the independently Windows-resolved candidate uv.lock.
# New dependency upgrades require manual review and an explicit inventory update.
EXPECTED: dict[str, tuple[str, ...]] = {
    'annotated-types': ('0.8.0', ),
    'anyio': ('4.16.0', ),
    'attrs': ('26.1.0', ),
    'cffi': ('2.1.1', ),
    'click': ('8.5.1', ),
    'colorama': ('0.4.6', ),
    'cryptography': ('50.0.2', ),
    'exchange-calendars': ('4.13.2', ),
    'h11': ('0.16.0', ),
    'httpcore2': ('2.16.0', ),
    'httpx2': ('2.16.0', ),
    'httpx2-jsfetch': ('1.0', ),
    'idna': ('3.20', ),
    'iniconfig': ('2.3.1', ),
    'jsonschema': ('4.26.0', ),
    'jsonschema-specifications': ('2025.9.1', ),
    'korean-lunar-calendar': ('0.4.0', ),
    'mcp': ('2.3.0', ),
    'mcp-types': ('2.3.0', ),
    'numpy': ('2.4.6', '2.5.4', ),
    'opentelemetry-api': ('1.45.2', ),
    'packaging': ('26.3', ),
    'pandas': ('3.1.1', ),
    'pluggy': ('1.7.0', ),
    'pyaes': ('1.6.1', ),
    'pyasn1': ('0.6.5', ),
    'pycparser': ('3.12', ),
    'pydantic': ('2.14.3', ),
    'pydantic-core': ('2.50.4', ),
    'pygments': ('2.21.0', ),
    'pyjwt': ('2.15.1', ),
    'pyluach': ('2.3.0', ),
    'pytest': ('9.2.0', ),
    'python-dateutil': ('4.0.0', ),
    'python-multipart': ('0.0.33', ),
    'pywin32': ('312', ),
    'referencing': ('0.37.0', ),
    'rpds-py': ('2026.9.1', ),
    'rsa': ('4.9.1', ),
    'ruff': ('0.16.8', ),
    'sse-starlette': ('3.7.0', ),
    'starlette': ('1.10.0', ),
    'swift-options-autotrader': ('0.1.0', ),
    'telethon': ('1.45.0', ),
    'toolz': ('1.2.0', ),
    'truststore': ('0.10.5', ),
    'typing-extensions': ('4.16.0', ),
    'typing-inspection': ('0.4.5', ),
    'tzdata': ('2026.6', ),
    'uvicorn': ('0.54.0', ),
}


def validate_lock(path: Path = Path("uv.lock")) -> int:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    if lock.get("version") != 1 or lock.get("requires-python") != ">=3.11":
        raise ValueError("Unexpected lockfile version or Python range")
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("Dependency inventory is missing")

    seen: dict[str, set[str]] = defaultdict(set)
    count = 0
    for package in packages:
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("Malformed locked package identity")
        if version in seen[name]:
            raise ValueError("Duplicate locked package and version")
        seen[name].add(version)
        if name == "swift-options-autotrader":
            if package.get("source") != {"editable": "."}:
                raise ValueError("Local package is not editable checkout")
            continue
        if package.get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Unexpected Python package registry")
        artifacts = ([package["sdist"]] if "sdist" in package else []) + package.get(
            "wheels", []
        )
        if not artifacts:
            raise ValueError("Locked dependency has no hashed artifacts")
        urls: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("Malformed artifact record")
            digest = artifact.get("hash", "")
            raw_url = artifact.get("url", "")
            if not isinstance(digest, str) or not re.fullmatch(
                r"sha256:[a-f0-9]{64}", digest
            ):
                raise ValueError("Missing SHA-256 artifact digest")
            if not isinstance(raw_url, str):
                raise ValueError("Invalid artifact location")
            url = urlparse(raw_url)
            if (
                url.scheme != "https" or url.hostname != "files.pythonhosted.org"
                or url.port is not None or url.username is not None
                or url.password is not None or url.query or url.fragment
                or raw_url in urls
            ):
                raise ValueError("Untrusted or duplicate artifact URL")
            if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0:
                raise ValueError("Missing or invalid artifact size")
            urls.add(raw_url)
            count += 1

    actual = {name: tuple(sorted(versions)) for name, versions in seen.items()}
    expected = {name: tuple(sorted(versions)) for name, versions in EXPECTED.items()}
    if actual != expected or len(packages) != sum(len(v) for v in EXPECTED.values()):
        raise ValueError("Unknown, missing, duplicate or updated locked dependency")
    # Resolver branches with the same name must be explicitly bounded.
    if len(seen["numpy"]) != 2 or not all(
        package.get("resolution-markers")
        for package in packages if package.get("name") == "numpy"
    ):
        raise ValueError("Missing explicit platform-specific numpy branches")
    if count < 11:
        raise ValueError("Unexpectedly incomplete artifact inventory")
    return count


if __name__ == "__main__":
    print(f"Lock audit passed: {len(EXPECTED)} packages and {validate_lock()} hashed artifacts")
