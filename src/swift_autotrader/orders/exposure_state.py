"""Shared journal-derived exposure checks; terminal order != flat position."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

# Keep unresolved buy/exit intents visible even if the signal label is stale.
_NONTERMINAL = "('FILLED','CANCELED','REJECTED')"
_NET = (
    "SUM(CASE WHEN side='BUY_TO_OPEN' THEN filled_quantity "
    "WHEN side='SELL_TO_CLOSE' THEN -filled_quantity ELSE 0 END)"
)


def active_signal_ids(connection: sqlite3.Connection) -> set[str]:
    """Include residual exposure and any unresolved order, regardless of signal label."""
    rows = connection.execute(
        "SELECT signal_id FROM order_intents GROUP BY signal_id "
        f"HAVING {_NET} != 0 OR "
        f"MAX(CASE WHEN state NOT IN {_NONTERMINAL} THEN 1 ELSE 0 END) > 0"
    ).fetchall()
    return {str(row[0]) for row in rows}


def has_active_exposure(db_path: Path) -> bool:
    """False only when every journaled intent is terminal and net fills are zero."""
    with closing(sqlite3.connect(str(db_path))) as connection:
        return bool(active_signal_ids(connection))


def has_owned_or_pending_entry(connection: sqlite3.Connection, signal_id: str) -> bool:
    """A delayed exit may only refer to a prior owned position or pending buy."""
    row = connection.execute(
        f"SELECT COALESCE({_NET},0), "
        f"COALESCE(MAX(CASE WHEN side='BUY_TO_OPEN' AND state NOT IN {_NONTERMINAL} "
        "THEN 1 ELSE 0 END),0) FROM order_intents WHERE signal_id=?",
        (signal_id,),
    ).fetchone()
    return bool(row and (row[0] > 0 or row[1] > 0))
