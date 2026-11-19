"""Pure SQLite safety interlock; no broker transport, credentials or real orders."""
import sqlite3
from contextlib import closing

import pytest

from swift_autotrader.orders.exposure_state import unresolved_exit_signal_ids


def journal(db):
    db.executescript(
        "CREATE TABLE order_intents(signal_id TEXT, side TEXT, filled_quantity INTEGER, state TEXT);"
        "CREATE TABLE signal_close_requests(signal_id TEXT);"
    )


@pytest.mark.parametrize(("orders", "requested", "held"), [
    ([], False, False),
    ([], True, False),
    ([("BUY_TO_OPEN", 1, "FILLED")], False, False),
    ([("BUY_TO_OPEN", 1, "FILLED")], True, True),
    ([("BUY_TO_OPEN", 0, "ACCEPTED")], True, True),
    ([("BUY_TO_OPEN", 0, "CANCELED")], True, False),
    ([("BUY_TO_OPEN", 1, "CANCELED")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 0, "REJECTED")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 0, "REJECTED")], False, True),
    ([("BUY_TO_OPEN", 2, "FILLED"), ("SELL_TO_CLOSE", 1, "CANCELED")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 0, "INTENT_RECORDED")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 0, "UNKNOWN")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 0, "ACCEPTED")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 1, "CANCEL_PENDING")], True, True),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 1, "FILLED")], True, False),
    ([("BUY_TO_OPEN", 1, "FILLED"), ("SELL_TO_CLOSE", 2, "FILLED")], True, True),
])
def test_exit_hold_matrix(orders, requested, held):
    with closing(sqlite3.connect(":memory:")) as db:
        journal(db)
        db.executemany("INSERT INTO order_intents VALUES('target',?,?,?)", orders)
        if requested:
            db.execute("INSERT INTO signal_close_requests VALUES('target')")
        assert unresolved_exit_signal_ids(db) == ({"target"} if held else set())


def test_durable_hold_survives_reopen_and_only_flat_reconciliation_clears(tmp_path):
    path = tmp_path / "journal.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        journal(db)
        db.executemany("INSERT INTO order_intents VALUES(?,?,?,?)", [
            ("target", "BUY_TO_OPEN", 1, "FILLED"),
            ("target", "SELL_TO_CLOSE", 0, "REJECTED"),
            ("other", "BUY_TO_OPEN", 1, "FILLED"),
        ])
        db.execute("INSERT INTO signal_close_requests VALUES('target')")
    with closing(sqlite3.connect(path)) as db, db:
        assert unresolved_exit_signal_ids(db) == {"target"}
        # Synthetic ledger update represents independently reconciled closure.
        db.execute("INSERT INTO order_intents VALUES('target','SELL_TO_CLOSE',1,'FILLED')")
        assert unresolved_exit_signal_ids(db) == set()
