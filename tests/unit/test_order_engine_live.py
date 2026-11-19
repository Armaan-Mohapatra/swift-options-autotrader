"""Unit test suite for PaperOrderEngine in Live Mode (R2 / Feature 3 & 4).

Covers:
- Live mode accepts only the canonical, enabled LiveBroker; paper mode only PaperBroker.
- Exact alert-premium entry limits regardless of the current ask.
- Cumulative realized daily loss calculation via FIFO matching of closed positions.
- Daily loss limit enforcement triggering DAILY_LOSS_CEILING_REACHED rejection.
- Isolation of previous days' realized losses from today's ceiling.
- Partial close loss calculations.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.broker.paper import PaperBroker, iso
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.paper_dispatch import VerifiedQuote
from swift_autotrader.signals.identity import OptionContract, OptionRight
from tests.fixtures.live_mocks import StatefulFakeMCP

NOW = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
CONTRACT = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 20))
OTHER = OptionContract("SPY", OptionRight.PUT, Decimal("670"), date(2026, 11, 20))


class FakeClock:
    def __init__(self, current: datetime = NOW):
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def insert_test_signal(
    db_path: Path,
    *,
    mid: int = 1,
    contract: OptionContract = CONTRACT,
    price: str = "1.20",
    sent: datetime = NOW,
) -> str:
    channel = "-1001234567890"
    sid = hashlib.sha256(f"swift:{channel}:{mid}".encode()).hexdigest()[:32]
    with sqlite3.connect(str(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO raw_events(channel_id, telegram_message_id, edit_version, "
            "received_at_utc, sent_at_utc, body) VALUES(?, ?, 0, ?, ?, ?)",
            (channel, str(mid), iso(sent), iso(sent), "SWIFT BUY ALERT"),
        )
        raw_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id, channel_id, message_id, "
            "revision, event_kind, origin, telegram_sent_at_utc, received_at_utc, "
            "content_sha256, eligible_for_future_processing) "
            "VALUES(?, ?, ?, 0, 'new', 'live', ?, ?, ?, 1)",
            (raw_id, channel, mid, iso(sent), iso(sent), f"hash-{mid}"),
        )
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO option_signals(signal_id, source_event_id, underlying, option_right, "
            "strike, expiration_date, original_limit, created_at_utc, state) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'SEEN')",
            (sid, raw_id, contract.underlying, contract.right.value,
             str(contract.strike), contract.expiration.isoformat(), price, iso(sent)),
        )
        db.execute(
            "INSERT INTO signal_metadata(signal_id, channel_id, source_message_id, category, "
            "first_revision_id) VALUES(?, ?, ?, 'STANDARD', ?)",
            (sid, channel, mid, rid),
        )
        db.execute(
            "INSERT INTO signal_parser_decisions(revision_id, channel_id, message_id, "
            "intent, status, reason, signal_id, processed_at_utc) "
            "VALUES(?, ?, ?, 'ENTRY', 'RECORDED', 'ENTRY_VALIDATED_NOT_EXECUTED', ?, ?)",
            (rid, channel, mid, sid, iso(sent)),
        )
    return sid


def simulate_closed_trade(
    db_path: Path,
    broker: object,
    mid: int,
    contract: OptionContract,
    buy_price: Decimal,
    sell_price: Decimal,
    closed_at: datetime,
    quantity: int = 1,
) -> str:
    sid = insert_test_signal(db_path, mid=mid, contract=contract, price=str(buy_price), sent=closed_at)
    entry_id = f"{sid}:entry"
    close_id = f"{sid}:close"
    at_str = iso(closed_at)
    with sqlite3.connect(str(db_path)) as db:
        db.execute(
            "INSERT INTO order_intents(logical_order_id, signal_id, idempotency_key, side, "
            "requested_quantity, filled_quantity, limit_price, state, accepted_at_utc) "
            "VALUES(?, ?, ?, 'BUY_TO_OPEN', ?, ?, ?, 'FILLED', ?)",
            (entry_id, sid, f"k_buy_{mid}", quantity, quantity, str(buy_price), at_str),
        )
        db.execute(
            "INSERT INTO order_fills(fill_id, logical_order_id, filled_quantity, filled_price, filled_at_utc) "
            "VALUES(?, ?, ?, ?, ?)",
            (f"fill_b_{mid}", entry_id, quantity, str(buy_price), at_str),
        )
        db.execute(
            "INSERT INTO order_intents(logical_order_id, signal_id, idempotency_key, side, "
            "requested_quantity, filled_quantity, limit_price, state, accepted_at_utc) "
            "VALUES(?, ?, ?, 'SELL_TO_CLOSE', ?, ?, ?, 'FILLED', ?)",
            (close_id, sid, f"k_close_{mid}", quantity, quantity, str(sell_price), at_str),
        )
        db.execute(
            "INSERT INTO order_fills(fill_id, logical_order_id, filled_quantity, filled_price, filled_at_utc) "
            "VALUES(?, ?, ?, ?, ?)",
            (f"fill_s_{mid}", close_id, quantity, str(sell_price), at_str),
        )
        db.execute("UPDATE option_signals SET state='CLOSED' WHERE signal_id=?", (sid,))

    return sid


def make_quote(contract: OptionContract, ask: str, bid: str = "1.00", at: datetime = NOW) -> VerifiedQuote:
    return VerifiedQuote(
        quote_id=f"quote-{contract.underlying}-{int(at.timestamp())}",
        contract=contract,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
        observed_at_utc=at,
        source="MCP_READONLY",
    )


def _preflight() -> PaperPreflight:
    return PaperPreflight({
        CONTRACT: Preflight(
            instrument_verified=True,
            options_permission=True,
            market_session_open=True,
            available_buying_power_usd=Decimal("10000"),
            premium_tick=Decimal("0.01"),
            contract=CONTRACT,
        ),
        OTHER: Preflight(
            instrument_verified=True,
            options_permission=True,
            market_session_open=True,
            available_buying_power_usd=Decimal("10000"),
            premium_tick=Decimal("0.05"),
            contract=OTHER,
        ),
    })


LIMITS = RiskCeilings(
    contracts_per_signal=1,
    premium_per_signal_usd=Decimal("999"),
    entry_max_age_seconds=120,
    daily_net_loss_usd=Decimal("500"),
)


@pytest.fixture
def engine_rig(tmp_path: Path):
    """Live engine on the canonical LiveBroker and the offline real-shape MCP fake."""
    db_path = tmp_path / "swift.sqlite3"
    apply_migrations(db_path)
    clock = FakeClock(NOW)
    mcp = StatefulFakeMCP(clock=clock)
    journal = tmp_path / "live_journal.sqlite3"
    LiveBroker.create_journal(journal)
    broker = LiveBroker(journal, mcp, account_number=mcp.account_number, writes_enabled=True,
                        kill_switch=lambda: False, signal_gate=lambda _signal: "ENTRY",
                        clock=clock)
    preflight = _preflight()
    engine = PaperOrderEngine(db_path, broker, preflight=preflight, limits=LIMITS,
                              clock=clock, live_mode=True)
    yield db_path, clock, mcp, engine, preflight, LIMITS
    broker.close()


@pytest.fixture
def ledger_rig(tmp_path: Path):
    """Engine daily-loss ledger (shared by paper and live) over synthetic closed trades."""
    db_path = tmp_path / "swift.sqlite3"
    apply_migrations(db_path)
    clock = FakeClock(NOW)
    broker = PaperBroker(db_path, clock=clock)
    engine = PaperOrderEngine(db_path, broker, preflight=_preflight(), limits=LIMITS, clock=clock)
    return db_path, clock, broker, engine, None, LIMITS


def test_live_mode_broker_validation(tmp_path: Path):
    """One canonical live writer: live mode accepts only an enabled LiveBroker."""
    db_path = tmp_path / "test.sqlite3"
    apply_migrations(db_path)
    preflight = PaperPreflight()
    journal = tmp_path / "live_journal.sqlite3"
    LiveBroker.create_journal(journal)
    mcp = StatefulFakeMCP()

    class LookalikeBroker:  # protocol-shaped, but not the canonical writer
        def submit_order(self, intent): raise AssertionError
        def get_order(self, logical_order_id): raise AssertionError
        def cancel_order(self, logical_order_id): raise AssertionError

    with pytest.raises(TypeError, match="ONLY the concrete PaperBroker"):
        PaperOrderEngine(db_path, LookalikeBroker(), preflight=preflight, live_mode=False)
    for candidate in (LookalikeBroker(), PaperBroker(db_path),
                      LiveBroker(journal, mcp, account_number=mcp.account_number)):
        with pytest.raises(TypeError, match="only an enabled LiveBroker"):
            PaperOrderEngine(db_path, candidate, preflight=preflight, live_mode=True)
    enabled = LiveBroker(journal, mcp, account_number=mcp.account_number, writes_enabled=True)
    try:
        engine = PaperOrderEngine(db_path, enabled, preflight=preflight, live_mode=True)
        assert engine.live_mode is True
    finally:
        enabled.close()


@pytest.mark.parametrize(("ask", "within_tolerance"),
                         [("1.10", True), ("1.25", True), ("1.32", True),
                          ("1.33", False), ("9.99", False)])
def test_live_engine_entry_limit_is_exact_alert_premium_for_any_ask(engine_rig, ask, within_tolerance):
    """The quote never moves the entry: the limit is the original alert premium.

    An ask above 110% of the alert is a REJECTION guard at LiveBroker, never a reason to
    raise the limit: the refused entry is recorded unsent and nothing reaches Robinhood.
    """
    db_path, clock, mcp, engine, _, _ = engine_rig
    sid = insert_test_signal(db_path, mid=101, contract=CONTRACT, price="1.20", sent=clock())
    mcp.set_quote("opt-spy-680c", "1.00", ask)

    quote = make_quote(CONTRACT, ask=ask, at=clock())
    result = engine.run_cycle(entry_quotes={CONTRACT: quote})

    placed = [args for name, args in mcp.calls if name == "place_option_order"]
    with sqlite3.connect(str(db_path)) as db:
        row = db.execute("SELECT limit_price, state FROM order_intents WHERE signal_id=?",
                         (sid,)).fetchone()
        alerts = {r[0] for r in db.execute(
            "SELECT code FROM paper_engine_alerts WHERE signal_id=?", (sid,))}
    if within_tolerance:
        assert result.entry_submissions == 1 and result.blocked_entries == 0
        assert len(placed) == 1 and Decimal(placed[0]["price"]) == Decimal("1.20")
        assert row == ("1.20", "ACCEPTED")
        assert alerts == set()
    else:
        assert result.entry_submissions == 0
        assert placed == []
        assert row == ("1.20", "REJECTED")
        assert alerts == {"SUBMISSION_REFUSED_PRE_PLACEMENT"}


def test_live_engine_off_tick_alert_premium_is_rejected_not_rounded(engine_rig):
    """An alert premium off the tick grid is refused; it is never rounded up or down."""
    db_path, clock, broker, engine, _, _ = engine_rig
    sid = insert_test_signal(db_path, mid=102, contract=CONTRACT, price="1.203", sent=clock())

    quote = make_quote(CONTRACT, ask="1.21", at=clock())
    result = engine.run_cycle(entry_quotes={CONTRACT: quote})

    assert result.entry_submissions == 0
    assert not any(name == "place_option_order" for name, _ in broker.calls)
    with sqlite3.connect(str(db_path)) as db:
        decision = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions WHERE signal_id=?", (sid,),
        ).fetchone()
    assert decision == ("BLOCKED", "INVALID_PREMIUM_TICK")


def test_live_engine_cumulative_daily_loss_calculation(ledger_rig):
    db_path, clock, broker, engine, _, _ = ledger_rig
    now = clock()
    yesterday = now - timedelta(days=1)

    # Trade 1 (today): Buy 1 @ 2.00, Sell 1 @ 1.50 -> Loss = $50.00
    simulate_closed_trade(db_path, broker, 301, CONTRACT, Decimal("2.00"), Decimal("1.50"), now)

    # Trade 2 (today): Buy 1 @ 5.00, Sell 1 @ 3.00 -> Loss = $200.00
    simulate_closed_trade(db_path, broker, 302, CONTRACT, Decimal("5.00"), Decimal("3.00"), now)

    # Trade 3 (yesterday): Buy 1 @ 3.00, Sell 1 @ 1.00 -> Loss = $200.00 (yesterday)
    simulate_closed_trade(db_path, broker, 303, CONTRACT, Decimal("3.00"), Decimal("1.00"), yesterday)

    # Today's realized loss: $50 + $200 = $250. Yesterday's $200 is excluded.
    today_loss = engine.cumulative_realized_daily_loss(now)
    assert today_loss == Decimal("250.00")


def test_live_engine_daily_loss_limit_blocks_new_entry(ledger_rig):
    db_path, clock, broker, engine, _, _ = ledger_rig
    now = clock()

    # Closed trade today realizing $550 loss (Buy 8.00, Sell 2.50 = -$550.00)
    simulate_closed_trade(db_path, broker, 401, CONTRACT, Decimal("8.00"), Decimal("2.50"), now)

    # Check cumulative loss
    assert engine.cumulative_realized_daily_loss(now) == Decimal("550.00")

    # Ingest a new valid signal
    sid = insert_test_signal(db_path, mid=402, contract=CONTRACT, price="1.20", sent=now)

    # Run cycle
    result = engine.run_cycle()
    assert result.entry_submissions == 0
    assert result.blocked_entries == 1

    # Verify decision in SQLite is DAILY_LOSS_CEILING_REACHED
    with sqlite3.connect(str(db_path)) as db:
        decision = db.execute(
            "SELECT outcome, reason FROM paper_engine_decisions WHERE signal_id=?",
            (sid,),
        ).fetchone()
        assert decision[0] == "BLOCKED"
        assert decision[1] == "DAILY_LOSS_CEILING_REACHED"


def test_live_engine_profitable_day_returns_zero_loss(ledger_rig):
    db_path, clock, broker, engine, _, _ = ledger_rig
    now = clock()

    # Winning trade: Buy 1 @ 1.00, Sell 1 @ 2.50 -> Gain = +$150.00
    simulate_closed_trade(db_path, broker, 501, CONTRACT, Decimal("1.00"), Decimal("2.50"), now)

    assert engine.cumulative_realized_daily_loss(now) == Decimal("0")


def test_mvp_net_realized_loss_offsets_winners(ledger_rig):
    db_path, clock, broker, engine, _, _ = ledger_rig
    now = clock()
    for mid, buy, sell in ((601,"1","5"),(602,"4","0.50"),(603,"4","0.50")):
        simulate_closed_trade(db_path, broker, mid, CONTRACT, Decimal(buy), Decimal(sell), now)
    assert engine.cumulative_realized_daily_loss(now) == Decimal("300")
