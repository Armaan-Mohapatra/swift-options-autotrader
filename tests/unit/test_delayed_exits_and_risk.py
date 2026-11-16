"""Unit tests for delayed exit handling, entry staleness, and engine risk safeguards."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

from swift_autotrader.broker.base import BrokerOrderState
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog


CHANNEL = -1001234000001
SENT = "2026-11-16T15:30:00.000000Z"

CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "safe.sqlite3"
    apply_migrations(path)
    return path


@pytest.fixture
def catalog() -> StaticInstrumentCatalog:
    return StaticInstrumentCatalog([
        CONTRACT,
        OptionContract("QQQ", OptionRight.CALL, Decimal("500"), date(2026, 11, 20)),
    ])


def seed_msg(
    db: Path, mid: int, body: str, *, reply: int | None = None,
    eligible: bool = True, sent: str = SENT,
) -> int:
    with sqlite3.connect(db) as con:
        rev = con.execute(
            "SELECT COALESCE(MAX(revision),-1)+1 FROM telegram_message_revisions "
            "WHERE channel_id=? AND message_id=?", (str(CHANNEL), mid),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
            "received_at_utc,sent_at_utc,body) VALUES(?,?,?,?,?,?)",
            (str(CHANNEL), str(mid), rev, sent, sent, body),
        )
        rawid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,"
            "revision,event_kind,origin,telegram_sent_at_utc,telegram_edited_at_utc,"
            "received_at_utc,reply_to_message_id,content_sha256,eligible_for_future_processing) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rawid, str(CHANNEL), mid, rev, "new", "live", sent, None, sent,
                reply, f"fixture-{CHANNEL}-{mid}-{rev}", int(eligible),
            ),
        )
        rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO telegram_messages(channel_id,message_id,latest_revision_id,"
            "latest_effective_at_utc,first_seen_at_utc) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id,message_id) DO UPDATE SET "
            "latest_revision_id=excluded.latest_revision_id,"
            "latest_effective_at_utc=excluded.latest_effective_at_utc",
            (str(CHANNEL), mid, rid, sent, sent),
        )
    return rid


def test_delayed_exit_with_active_open_position_is_recorded(db: Path, catalog: StaticInstrumentCatalog):
    """When an active open position exists, a delayed exit (eligible=False) must NOT be stranded."""
    # 1. Entry message processed normally
    seed_msg(db, 1, "SPY 680C 11/20 @ 1.20", eligible=True)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    res1 = matcher.process_pending()
    assert len(res1) == 1
    assert res1[0].status == "RECORDED"
    sid = res1[0].signal_id

    # 2. Simulate position opened (FILLED in order_intents)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,side,requested_quantity,filled_quantity,limit_price,state) "
            "VALUES('ord-1', ?, 'idemp-1', 'BUY_TO_OPEN', 1, 1, '1.20', 'FILLED')",
            (sid,),
        )

    # 3. Delayed exit message arrives with eligible=False (e.g. historical reconnect or network lag)
    seed_msg(db, 2, "SPY SOLD HALF", eligible=False)
    res2 = matcher.process_pending()
    assert len(res2) == 1
    # Because position is open, delayed exit must be RECORDED rather than stranded/skipped
    assert res2[0].status == "RECORDED"
    with sqlite3.connect(db) as conn:
        closes = conn.execute("SELECT COUNT(*) FROM signal_close_requests WHERE signal_id=?", (sid,)).fetchone()[0]
    assert closes == 1


def test_delayed_exit_without_active_position_is_skipped(db: Path, catalog: StaticInstrumentCatalog):
    """When no position is held, a delayed exit (eligible=False) is skipped as non-actionable."""
    seed_msg(db, 10, "SPY 680C 11/20 @ 1.20", eligible=True)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    matcher.process_pending()

    # No order placed in order_intents!
    seed_msg(db, 11, "SPY SOLD HALF", eligible=False)
    res = matcher.process_pending()
    assert len(res) == 1
    assert res[0].status == "SKIPPED"
    assert res[0].reason == "NON_ACTIONABLE_TELEGRAM_HISTORY"


def test_stale_entry_message_is_skipped(db: Path, catalog: StaticInstrumentCatalog):
    """Stale entry alert with eligible=False is never promoted to an order signal."""
    seed_msg(db, 20, "SPY 680C 11/20 @ 1.20", eligible=False)
    matcher = SignalMatcher(db, CHANNEL, catalog=catalog)
    res = matcher.process_pending()
    assert len(res) == 1
    assert res[0].status == "SKIPPED"
    assert res[0].reason == "NON_ACTIONABLE_TELEGRAM_HISTORY"
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM option_signals").fetchone()[0]
    assert count == 0


def test_duplicate_contract_position_blocked_by_engine(db: Path):
    """Engine must reject entry if another position/order for the exact same contract is open."""
    broker = PaperBroker(db)
    engine = PaperOrderEngine(db, broker, preflight=PaperPreflight(), live_mode=False)

    now = datetime(2026, 11, 16, 15, 30, 0, tzinfo=UTC)

    # First signal and order intent
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO option_signals(signal_id,source_event_id,underlying,option_right,strike,expiration_date,original_limit,created_at_utc,state) "
            "VALUES('sig-a', 1, 'SPY', 'CALL', '680', '2026-11-20', '1.20', '2026-11-16T15:29:00Z', 'SEEN')"
        )
        conn.execute(
            "INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,side,requested_quantity,filled_quantity,limit_price,state) "
            "VALUES('ord-a', 'sig-a', 'idemp-a', 'BUY_TO_OPEN', 1, 1, '1.20', 'FILLED')"
        )

        # Second signal for the exact same contract
        conn.execute(
            "INSERT INTO option_signals(signal_id,source_event_id,underlying,option_right,strike,expiration_date,original_limit,created_at_utc,state) "
            "VALUES('sig-b', 2, 'SPY', 'CALL', '680', '2026-11-20', '1.25', '2026-11-16T15:29:30Z', 'SEEN')"
        )
        conn.row_factory = sqlite3.Row
        signal_b = conn.execute("SELECT * FROM option_signals WHERE signal_id='sig-b'").fetchone()

    # Attempt to open second entry
    result = engine._open_entry(signal_b, now)
    assert result is False

    with sqlite3.connect(db) as conn:
        blocked = conn.execute(
            "SELECT outcome, reason FROM paper_engine_decisions WHERE signal_id='sig-b'"
        ).fetchone()
    assert blocked[0] == "BLOCKED"
    assert blocked[1] == "DUPLICATE_CONTRACT_POSITION"


def test_outstanding_risk_accounts_for_partially_filled_canceled_order(db: Path):
    """Partially filled buy order with canceled remainder must count only actual filled exposure."""
    broker = PaperBroker(db)
    engine = PaperOrderEngine(db, broker, preflight=PaperPreflight(), live_mode=False)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO option_signals(signal_id,source_event_id,underlying,option_right,strike,expiration_date,original_limit,created_at_utc,state) "
            "VALUES('sig-c', 3, 'SPY', 'CALL', '680', '2026-11-20', '2.00', '2026-11-16T15:00:00Z', 'PROCESSED')"
        )
        # 3 requested, 1 filled, remainder canceled -> state is CANCELED
        conn.execute(
            "INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,side,requested_quantity,filled_quantity,limit_price,state) "
            "VALUES('ord-c', 'sig-c', 'idemp-c', 'BUY_TO_OPEN', 3, 1, '2.00', 'CANCELED')"
        )
        # 1 fill of 1 contract @ $2.00 ($200 exposure)
        conn.execute(
            "INSERT INTO order_fills(fill_id,logical_order_id,filled_quantity,filled_price,filled_at_utc) "
            "VALUES('fill-c-1', 'ord-c', 1, '2.00', '2026-11-16T15:01:00Z')"
        )

    outstanding_usd, active_count = engine._outstanding_risk()
    # 1 contract @ 2.00 * 100 = $200.00
    assert outstanding_usd == Decimal("200.00")
    assert active_count == 1
