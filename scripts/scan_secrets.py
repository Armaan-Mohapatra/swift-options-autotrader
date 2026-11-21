"""Offline CI repository hygiene guard; complements GitHub secret scanning, not a replacement."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXTENSIONS = {".session", ".sqlite", ".sqlite3", ".db", ".pem", ".p12"}
FORBIDDEN_NAMES = {".env", "id_rsa", "credentials.json", "token.json"}
CHECK_EXTENSIONS = {".py", ".md", ".toml", ".yml", ".yaml", ".ps1", ".sql"}
# Detect obvious credential value assignments, not field names, example placeholders or tests.
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api_hash|client_secret|access_token|refresh_token)\s*[=:]\s*['\"][a-z0-9_-]{20,}['\"]"),
]


def scan() -> list[str]:
    violations: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS or path.name in FORBIDDEN_NAMES:
            violations.append(f"Forbidden path: {relative}")
            continue
        if path.suffix.lower() not in CHECK_EXTENSIONS:
            continue
        data = path.read_text(encoding="utf-8")
        if any(pattern.search(data) for pattern in PATTERNS):
            # Do not output matching secrets.
            violations.append(f"Possible secret material in {relative}")
    return violations


if __name__ == "__main__":
    errors = scan()
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        raise SystemExit(1)
    print("Secret-hygiene checks passed (heuristic scan only)")
