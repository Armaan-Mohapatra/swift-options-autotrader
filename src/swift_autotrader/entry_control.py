"""Operator control of the entry kill switch (paper_engine_control), live or paper.

LiveBroker reads this row before every entry, so a pause takes effect on the next entry
without restarting anything. Exits and cancels of owned orders are never blocked.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _connect(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise RuntimeError("database not found; run migrate first")
    return sqlite3.connect(str(path), timeout=10, isolation_level=None)


def entries_status(db_path: Path) -> dict[str, Any]:
    with closing(_connect(db_path)) as db:
        row = db.execute("SELECT paused, emergency_stop, reason, updated_at_utc "
                         "FROM paper_engine_control WHERE singleton=1").fetchone()
    if row is None:
        raise RuntimeError("engine control row missing")
    paused, stopped = bool(row[0]), bool(row[1])
    return {"entries": "BLOCKED" if paused or stopped else "ALLOWED", "paused": paused,
            "emergency_stop": stopped, "reason": row[2], "updated_at_utc": row[3]}


def pause_entries(db_path: Path, *, reason: str = "OPERATOR_PAUSE") -> dict[str, Any]:
    with closing(_connect(db_path)) as db:
        db.execute("UPDATE paper_engine_control SET paused=1, updated_at_utc=?, reason=? "
                   "WHERE singleton=1", (datetime.now(UTC).isoformat(), reason[:120]))
    return entries_status(db_path)


def resume_entries(db_path: Path) -> dict[str, Any]:
    """Clear a pause. An emergency stop needs the manual reset, never this."""
    if entries_status(db_path)["emergency_stop"]:
        raise RuntimeError("emergency stop is active; reset it manually first")
    with closing(_connect(db_path)) as db:
        db.execute("UPDATE paper_engine_control SET paused=0, updated_at_utc=?, "
                   "reason='OPERATOR_RESUME' WHERE singleton=1",
                   (datetime.now(UTC).isoformat(),))
    return entries_status(db_path)
