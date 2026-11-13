"""FIFO accounting, provenance, missing-price and fee behavior."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from swift_autotrader.analytics.metrics import CostModel, PaperAnalytics
from swift_autotrader.broker.paper import iso
from swift_autotrader.database.migrate import apply_migrations

NOW = datetime(2026, 11, 12, 15, 4, tzinfo=UTC)


def prepared(tmp_path):
    db_path = tmp_path / "analytics.sqlite3"
    apply_migrations(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO option_signals(signal_id,underlying,option_right,strike,"
                   "expiration_date,original_limit,swift_reported_avg,"
                   "confirmed_avg_fill_price,state,created_at_utc)"
                   "VALUES('sig','SPY','CALL','680','2026-11-13','1.20','0.95','1.25',"
                   "'PARTIAL_CLOSE',?)", (iso(NOW - timedelta(minutes=5)),))
        for suffix, side, quantity, price in (
            ("entry", "BUY_TO_OPEN", 2, "1.20"),
            ("close", "SELL_TO_CLOSE", 1, "1.80"),
        ):
            db.execute("INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,"
                       "side,requested_quantity,filled_quantity,limit_price,state)"
                       "VALUES(?,?,?,?,?,?,?,'FILLED')",
                       (f"sig:{suffix}", "sig", f"key:{suffix}", side,
                        quantity, price if suffix == "entry" else "1.00", price))
        for fill_id, oid, quantity, price in (
            ("b1", "sig:entry", 1, "1.00"),
            ("b2", "sig:entry", 1, "1.50"),
            ("s1", "sig:close", 1, "2.00"),
        ):
            db.execute("INSERT INTO order_fills(fill_id,logical_order_id,"
                       "filled_quantity,filled_price,filled_at_utc) VALUES(?,?,?,?,?)",
                       (fill_id, oid, quantity, price, iso(NOW - timedelta(minutes=1))))
        db.execute("INSERT INTO paper_market_observations(quote_id,underlying,"
                   "option_right,strike,expiration_date,bid,ask,bid_size,ask_size,"
                   "observed_at_utc,provenance) VALUES('mark','SPY','CALL','680',"
                   "'2026-11-13','1.40','1.45',100,100,?,'SYNTHETIC')", (iso(NOW),))
    return db_path


def test_fifo_realized_and_unrealized_are_independent_from_swift_average(tmp_path):
    db_path = prepared(tmp_path)
    result = PaperAnalytics(db_path, costs=CostModel(
        fee_per_contract_side_usd=Decimal("0.65")
    )).snapshot(as_of=NOW)
    pos = result["positions"][0]
    assert pos["swift_reported_average"] == "0.95"
    assert pos["actual_average_fill"] == "1.25"
    assert pos["actual_average_exit"] == "2.00"
    assert pos["open_contracts"] == 1
    assert result["realized_gross_usd"] == "100.00"
    assert result["realized_net_usd"] == "98.70"
    assert result["unrealized_gross_usd"] == "-10.00"
    assert result["unrealized_net_usd"] == "-11.30"
    assert result["fees_paid_usd"] == "1.95"
    assert result["win_rate"] is None  # partially closed ≠ completed trade
    assert result["completed_positions"] == 0
    assert result["mean_observed_bid_ask_spread_premium"] == "0.05"


def test_missing_or_stale_bid_is_unavailable_not_zero(tmp_path):
    path = prepared(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM paper_market_observations")
    r = PaperAnalytics(path).snapshot(as_of=NOW)
    assert r["unrealized_gross_usd"] is None
    assert r["positions"][0]["last_bid"] is None
    assert r["positions"][0]["mark_status"] == "UNAVAILABLE_NO_FRESH_BID"
    assert r["realized_net_usd"] is None  # missing fee schedule
    assert r["realized_gross_usd"] == "100.00"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO paper_market_observations(quote_id,underlying,"
                   "option_right,strike,expiration_date,bid,ask,bid_size,ask_size,"
                   "observed_at_utc,provenance) VALUES('old','SPY','CALL','680',"
                   "'2026-11-13','1.40','1.45',10,10,?,'SYNTHETIC')",
                   (iso(NOW - timedelta(hours=1)),))
    result = PaperAnalytics(path, costs=CostModel(
        mark_max_age_seconds=30
    )).snapshot(as_of=NOW)
    assert result["unrealized_gross_usd"] is None


def test_future_quote_is_not_looked_ahead(tmp_path):
    path = prepared(tmp_path)
    result = PaperAnalytics(path).snapshot(as_of=NOW - timedelta(seconds=15))
    assert result["positions"][0]["last_bid"] is None
    assert result["unrealized_gross_usd"] is None


def test_zero_fill_still_cannot_be_called_profitable(tmp_path):
    path = tmp_path / "empty.sqlite3"
    apply_migrations(path)
    r = PaperAnalytics(path).snapshot(as_of=NOW)
    assert r["realized_gross_usd"] == "0"
    assert r["win_rate"] is None
    assert r["completed_positions"] == 0
    assert r["positions"] == []
    assert r["telegram"]["status"] == "NOT_STARTED"


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_invalid_fee_assumptions_rejected(bad):
    with pytest.raises(ValueError):
        CostModel(fee_per_contract_side_usd=bad)
    with pytest.raises(ValueError):
        CostModel(slippage_per_contract_premium=bad)


def test_net_metrics_do_not_invent_fees(tmp_path):
    path = prepared(tmp_path)
    r = PaperAnalytics(path).snapshot(as_of=NOW)
    assert r["fees_paid_usd"] is None
    assert r["realized_net_usd"] is None
    assert r["realized_gross_usd"] == "100.00"
    assert r["win_rate"] is None
