"""Offline, hermetic checks; no brokerage transport or credentials."""
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from swift_autotrader.orders.acceptance_scope import AcceptanceScope, load_acceptance_scope
from swift_autotrader.orders.exposure_state import (
    active_signal_ids,
    has_active_exposure,
    has_owned_or_pending_entry,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract("SYNTH", OptionRight.CALL, Decimal("100"), date(2026, 11, 20))
SCOPE = AcceptanceScope("SYNTHETIC", -1001, 12, 15, CONTRACT, 1, Decimal("1.25"), NOW + timedelta(minutes=1))


def intent(**updates):
    base = dict(account="SYNTHETIC", channel_id=-1001, message_id=12, revision_id=15,
                contract=CONTRACT, quantity=1, limit=Decimal("1.25"), now=NOW)
    base.update(updates)
    return base


def test_exact_authorization():
    assert SCOPE.permits(**intent())
    assert SCOPE.permits(**intent(limit=Decimal("1.20")))


@pytest.mark.parametrize("changes", [
    {"account": "OTHER"}, {"channel_id": -1002}, {"message_id": 13}, {"revision_id": 16},
    {"quantity": 2}, {"quantity": True}, {"limit": Decimal("1.26")},
    {"limit": Decimal("NaN")}, {"limit": Decimal("0")}, {"limit": 1.2},
    {"contract": replace(CONTRACT, strike=Decimal("101"))},
    {"contract": replace(CONTRACT, right=OptionRight.PUT)},
    {"contract": replace(CONTRACT, expiration=date(2026, 11, 27))},
    {"contract": replace(CONTRACT, underlying="OTHER")},
    {"now": SCOPE.expires_at}, {"now": NOW.replace(tzinfo=None)},
])
def test_scope_rejects_mismatch(changes):
    assert not SCOPE.permits(**intent(**changes))


@pytest.mark.parametrize("changes", [
    {"quantity": True}, {"quantity": 2}, {"channel_id": 0}, {"message_id": 0},
    {"revision_id": -1}, {"maximum_limit": Decimal("Infinity")},
    {"expires_at": NOW.replace(tzinfo=None)},
])
def test_invalid_scope_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(SCOPE, **changes)


def receipt():
    return dict(version=1, account_sha256=hashlib.sha256(b"SYNTHETIC").hexdigest(),
                channel_id=-1001, message_id=12, revision_id=15, underlying="SYNTH",
                right="CALL", strike="100", expiration="2026-11-20", quantity=1,
                maximum_limit="1.25", expires_at="2026-11-16T15:01:00Z")


def test_receipt_roundtrip(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt()), encoding="utf-8")
    assert load_acceptance_scope(path, account="SYNTHETIC") == SCOPE
    with pytest.raises(ValueError, match="account binding"):
        load_acceptance_scope(path, account="OTHER")


@pytest.mark.parametrize("changes", [
    {"version": True}, {"extra": 1}, {"maximum_limit": 1.25}, {"quantity": 1.0},
    {"right": "UNKNOWN"}, {"revision_id": True}, {"expires_at": "2026-11-16T15:01:00"},
])
def test_receipt_rejects_malformed_fields(tmp_path, changes):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({**receipt(), **changes}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_acceptance_scope(path, account="SYNTHETIC")


def test_receipt_rejects_duplicate_and_oversize(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt())[:-1] + ',"quantity":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_acceptance_scope(path, account="SYNTHETIC")
    path.write_bytes(b" " * 8193)
    with pytest.raises(ValueError, match="too large"):
        load_acceptance_scope(path, account="SYNTHETIC")


@pytest.mark.parametrize("orders,active,owned", [
    ([], False, False),
    ([("BUY_TO_OPEN",0,"REJECTED")], False, False),
    ([("BUY_TO_OPEN",0,"CANCELED")], False, False),
    ([("BUY_TO_OPEN",0,"INTENT_RECORDED")], True, True),
    ([("BUY_TO_OPEN",0,"UNKNOWN")], True, True),
    ([("BUY_TO_OPEN",0,"SUBMITTED")], True, True),
    ([("BUY_TO_OPEN",0,"ACCEPTED")], True, True),
    ([("BUY_TO_OPEN",1,"CANCEL_PENDING")], True, True),
    ([("BUY_TO_OPEN",1,"CANCELED")], True, True),
    ([("BUY_TO_OPEN",2,"FILLED"),("SELL_TO_CLOSE",1,"CANCELED")], True, True),
    ([("BUY_TO_OPEN",1,"FILLED"),("SELL_TO_CLOSE",1,"FILLED")], False, False),
    ([("BUY_TO_OPEN",1,"FILLED"),("SELL_TO_CLOSE",1,"CANCEL_PENDING")], True, False),
    ([("BUY_TO_OPEN",0,"CANCELED"),("SELL_TO_CLOSE",1,"FILLED")], True, False),
])
def test_exposure_from_net_fills(tmp_path, orders, active, owned):
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE order_intents(signal_id TEXT, side TEXT, filled_quantity INTEGER, state TEXT)")
        db.executemany("INSERT INTO order_intents VALUES('one',?,?,?)", orders)
        assert active_signal_ids(db) == ({"one"} if active else set())
        assert has_owned_or_pending_entry(db, "one") is owned
        assert not has_owned_or_pending_entry(db, "unrelated")
    assert has_active_exposure(path) is active
