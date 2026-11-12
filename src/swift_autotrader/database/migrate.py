"""Atomic, ordered, idempotent SQLite migrations with WAL enabled."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


class MigrationError(RuntimeError):
    """Schema migration failed or was altered after application."""


def apply_migrations(db_path: Path) -> list[str]:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    applied: list[str] = []
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at_utc TEXT NOT NULL)"
        )
        import hashlib

        directory = resources.files("swift_autotrader.database").joinpath("migrations")
        for migration in sorted(p for p in directory.iterdir() if p.name.endswith(".sql")):
            sql = migration.read_text(encoding="utf-8")
            fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = connection.execute(
                "SELECT sha256 FROM schema_migrations WHERE version = ?", (migration.name,)
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise MigrationError("An applied migration was changed")
                continue
            # No network, broker, or Telegram calls from schema migrations.
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations(version, sha256, applied_at_utc) "
                    + "VALUES ('"
                    + migration.name
                    + "', '"
                    + fingerprint
                    + "', strftime('%Y-%m-%dT%H:%M:%fZ','now'));\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                connection.rollback()
                raise MigrationError("SQLite migration failed") from exc
            applied.append(migration.name)
    finally:
        connection.close()
    return applied
