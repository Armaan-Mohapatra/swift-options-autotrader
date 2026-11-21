"""Final synthetic-only system acceptance: actual replay uses TelegramStore ->
SignalMatcher -> PaperOrderEngine -> PaperBroker -> PaperAnalytics.
No paid data, live transport, external credentials, or Robinhood writes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.analytics.replay import HistoricalReplay
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.broker.robinhood_mcp import (
    BrokerWritesDisabledError,
    RobinhoodMCPBroker,
)
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight
from swift_autotrader.signals.identity import OptionContract, OptionRight

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures/swift_phase5_replay_synthetic.json"


def fixture():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def run(data):
    result = HistoricalReplay(data).run()
    assert result["data_quality"] == "UNVERIFIED_ESTIMATE"
    assert result["instrument_catalog_quality"] == "USER_SUPPLIED_NOT_BROKER_VERIFIED"
    return result


@pytest.mark.parametrize("label", ["STANDARD", "LOTTO"])
def test_original_alert_limit_through_full_paper_lifecycle(label):
    data = fixture()
    data["events"][0]["text"] = (
        "SPY 680C 11/13 @ 1.20" if label == "STANDARD"
        else "SPY LOTTO 680C 11/13 @ 1.20"
    )
    result = run(data)
    p = result["report"]["positions"]
    assert len(p) == 1
    assert p[0]["source_limit"] == "1.20"
    assert p[0]["actual_average_fill"] == "1.15"
    assert p[0]["swift_reported_average"] == "0.95"
    assert p[0]["filled_buy_contracts"] == 2
    assert p[0]["filled_sell_contracts"] == 2
    assert p[0]["open_contracts"] == 0
    assert result["report"]["realized_gross_usd"] == "42.00"
    assert result["report"]["realized_net_usd"] == "39.40"


@pytest.mark.parametrize("alert", [
    "BUY 20 SHARES SPY", "BUY BTC NOW", "TSLA shares @ 250",
])
def test_non_option_alert_does_not_open_a_position(alert):
    data = fixture()
    data["events"] = [{"kind": "message", "at_utc": "2026-11-12T15:00:00Z",
                       "message_id": 1, "text": alert}]
    data["end_at_utc"] = "2026-11-12T15:03:00Z"
    result = run(data)
    assert not result["report"]["positions"]
    assert not result["report"]["orders_by_state"]


def test_market_price_distance_is_not_a_chase_filter():
    data = fixture()
    for e in data["events"]:
        if e["kind"] == "quote":
            e["bid"], e["ask"] = "2.40", "2.50"
    result = run(data)
    assert result["quote_based_fill_contracts"] == 0
    assert result["report"]["positions"][0]["source_limit"] == "1.20"
    assert result["report"]["positions"][0]["filled_buy_contracts"] == 0


@pytest.mark.parametrize("text", [
    "SPY SOLD HALF", "SPY SOLD 1/3", "SPY TRIM", "SPY RUNNERS OUT", "SPY ALL OUT"
])
def test_explicit_partial_or_full_exit_closes_entire_position(text):
    data = fixture()
    data["events"][4]["text"] = text
    result = run(data)
    positions = result["report"]["positions"]
    assert positions[0]["filled_buy_contracts"] == 2
    assert positions[0]["filled_sell_contracts"] == 2
    assert positions[0]["open_contracts"] == 0
    assert result["report"]["completed_positions"] == 1


def test_adding_and_average_never_expand_or_reprice_entry():
    result = run(fixture())
    p = result["report"]["positions"][0]
    assert p["source_limit"] == "1.20"
    assert p["swift_reported_average"] == "0.95"
    assert p["actual_average_fill"] == "1.15"
    assert result["report"]["orders_by_state"] == {"FILLED": 2}


@pytest.mark.parametrize("seconds,expected_cancel", [(3599, 0), (3600, 1), (3601, 1)])
def test_exact_elapsed_acceptance_deadline(seconds, expected_cancel):
    data = fixture()
    data["events"] = [data["events"][0]]
    data["end_at_utc"] = (
        datetime(2026, 11, 12, 15, tzinfo=UTC) + timedelta(seconds=seconds)
    ).isoformat()
    result = run(data)
    assert result["report"]["cancelled_orders"] == expected_cancel
    assert result["report"]["positions"][0]["filled_buy_contracts"] == 0
    assert len(result["report"]["pending_orders"]) == (0 if expected_cancel else 1)


def test_duplicate_entry_or_message_edit_never_reprices_or_doubles_buy():
    data = fixture()
    data["events"].insert(1, {"kind": "message", "at_utc": "2026-11-12T15:00:02Z",
                               "message_id": 1, "text": "SPY LOTTO 680C 11/13 @ 1.20"})
    data["events"].insert(2, {"kind": "edit", "at_utc": "2026-11-12T15:00:03Z",
                               "message_id": 1, "text": "SPY LOTTO 680C 11/13 @ 1.80"})
    result = run(data)
    assert result["report"]["positions"][0]["source_limit"] == "1.20"
    assert result["report"]["positions"][0]["filled_buy_contracts"] == 2
    assert result["report"]["orders_by_state"] == {"FILLED": 2}


def test_missing_quote_never_creates_fill_or_pnl():
    data = fixture()
    data["events"] = [e for e in data["events"] if e["kind"] != "quote"]
    result = run(data)
    assert result["quote_based_fill_contracts"] == 0
    assert result["report"]["completed_positions"] == 0
    assert result["report"]["win_rate"] is None


def test_no_fills_scenario_does_not_claim_profitable_trades():
    data = fixture()
    result = HistoricalReplay(data).run(scenario="no_fills")
    assert result["quote_based_fill_contracts"] == 0
    assert result["report"]["winning_completed_positions"] == 0
    assert result["report"]["win_rate"] is None


def test_offline_rollback_of_uncertain_broker_does_not_enable_live_writes(tmp_path):
    from datetime import date
    contract = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13))
    intent = OrderIntent("id:entry", "stable-key", contract,
                         OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"))
    actual = RobinhoodMCPBroker()
    with pytest.raises(BrokerWritesDisabledError):
        actual.submit_order(intent)
    with pytest.raises(BrokerWritesDisabledError):
        actual.cancel_order("id:entry")
    db = tmp_path / "paper.sqlite3"
    apply_migrations(db)
    broker = PaperBroker(db)
    assert broker.submit_order(intent).filled_quantity == 0
    # Engine cannot accept any external transport, even with PAPER_ONLY env true.
    with pytest.raises(TypeError):
        PaperOrderEngine(db, actual, preflight=PaperPreflight())


def test_unsupported_ambiguous_expiry_and_no_catalog_never_guess():
    data = fixture()
    data["catalog"] = []
    # Quotes cannot be trusted or matched without their catalog contract.
    with pytest.raises(ValueError, match="absent from explicit catalog"):
        run(data)


def test_data_provenance_is_never_promoted_to_verified_performance():
    data = fixture()
    data["source_type"] = "HISTORICAL_UNVERIFIED"
    result = run(data)
    assert result["report"]["positions"][0]["quote_provenance"] == "EXTERNAL_UNVERIFIED"
    assert result["data_quality"] == "UNVERIFIED_ESTIMATE"


def test_identical_underlying_separate_strike_and_expiration_never_cross_close():
    data = fixture()
    data["catalog"].append({
        "symbol": "SPY", "right": "CALL", "strike": "681",
        "expiration": "2026-12-18", "tick": "0.01",
        "options_permission": True, "market_session_open": True,
        "buying_power_usd": "10000",
    })
    data["events"].insert(1, {
        "kind": "message", "at_utc": "2026-11-12T15:00:01Z", "message_id": 2,
        "text": "SPY 681C 12/18 @ .90",
    })
    data["events"][3]["message_id"] = 3
    data["events"][5]["message_id"] = 4
    data["events"].insert(3, {
        "kind": "quote", "at_utc": "2026-11-12T15:00:31Z", "quote_id": "different-expiry",
        "contract": {"symbol": "SPY", "right": "CALL", "strike": "681",
                     "expiration": "2026-12-18"},
        "bid": "0.83", "ask": "0.85", "bid_size": 4, "ask_size": 4,
    })
    result = run(data)
    positions = {p["contract"]: p for p in result["report"]["positions"]}
    assert len(positions) == 2
    assert positions["SPY 2026-11-13 680C"]["open_contracts"] == 0
    assert positions["SPY 2026-12-18 681C"]["filled_buy_contracts"] == 2
    assert positions["SPY 2026-12-18 681C"]["filled_sell_contracts"] == 0
    assert positions["SPY 2026-12-18 681C"]["open_contracts"] == 2


def test_duplicate_exit_event_does_not_create_two_sell_orders():
    data = fixture()
    data["events"].insert(5, {"kind": "message", "at_utc": "2026-11-12T15:03:01Z",
                               "message_id": 3, "text": "SPY SOLD HALF",
                               "reply_to_message_id": 1})
    result = run(data)
    assert result["report"]["positions"][0]["filled_sell_contracts"] == 2
    assert result["report"]["orders_by_state"] == {"FILLED": 2}


def test_restart_preserves_paper_broker_order_and_fill_identity(tmp_path):
    from datetime import date

    path = tmp_path / "restart.sqlite3"
    apply_migrations(path)
    contract = OptionContract("SPY", OptionRight.PUT, Decimal("675"),
                              date(2026, 11, 13))
    intent = OrderIntent("e2e:entry", "restart-stable-key", contract,
                         OrderSide.BUY_TO_OPEN, 2, Decimal("1.20"))
    instant = datetime(2026, 11, 12, 15, tzinfo=UTC)
    broker = PaperBroker(path, clock=lambda: instant)
    first = broker.submit_order(intent)
    assert first.filled_quantity == 0
    broker.manual_fill(intent.logical_order_id, fill_id="synthetic-one", quantity=1,
                       price=Decimal("1.10"), at=instant)
    restarted = PaperBroker(path, clock=lambda: instant)
    replayed = restarted.submit_order(intent)
    assert replayed.filled_quantity == 1
    assert restarted.get_order(intent.logical_order_id).fills[0].fill_id == "synthetic-one"
    with pytest.raises(ValueError):
        restarted.submit_order(OrderIntent(
            intent.logical_order_id, intent.idempotency_key, contract,
            OrderSide.BUY_TO_OPEN, 2, Decimal("1.30"),
        ))


def test_paper_emergency_stop_persists_across_engine_restart(tmp_path):
    path = tmp_path / "stopped.sqlite3"
    apply_migrations(path)
    engine = PaperOrderEngine(path, PaperBroker(path), preflight=PaperPreflight())
    engine.emergency_stop()
    after_restart = PaperOrderEngine(path, PaperBroker(path),
                                     preflight=PaperPreflight())
    assert after_restart.health()["emergency_stop"] is True
    assert after_restart.health()["paused"] is True
    with pytest.raises(PermissionError):
        after_restart.reset_emergency_stop()
