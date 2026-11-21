"""Synthetic-only full Telegram -> parser -> SQLite -> broker -> report replay."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from swift_autotrader.analytics.replay import HistoricalReplay, replay_from_file

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/swift_phase5_replay_synthetic.json"


def sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_full_pipeline_exact_price_average_full_exit_and_fees():
    report = HistoricalReplay(sample()).run()
    assert report["scenario"] == "touch"
    assert report["source_type"] == "SYNTHETIC"
    assert report["data_quality"] == "UNVERIFIED_ESTIMATE"
    assert report["quote_based_fill_contracts"] == 4
    r = report["report"]
    assert r["realized_gross_usd"] == "42.00"
    assert r["realized_net_usd"] == "39.40"
    assert r["fees_paid_usd"] == "2.60"
    assert r["winning_completed_positions"] == 1
    assert r["completed_positions"] == 1
    assert r["win_rate"] == "1"
    assert r["pending_orders"] == []
    assert r["positions"][0]["open_contracts"] == 0
    assert r["positions"][0]["source_limit"] == "1.20"
    assert r["positions"][0]["swift_reported_average"] == "0.95"
    assert r["positions"][0]["actual_average_fill"] == "1.15"
    assert r["positions"][0]["actual_average_exit"] == "1.36"
    assert r["positions"][0]["filled_sell_contracts"] == 2
    assert r["positions"][0]["quote_provenance"] == "SYNTHETIC"


def test_compare_assumptions_in_independent_databases():
    results = HistoricalReplay(sample()).compare()
    assert results["mode"] == "PAPER_ONLY"
    assert [s["scenario"] for s in results["assumptions"]] == [
        "touch", "through", "no_fills"
    ]
    assert results["assumptions"][0]["report"]["realized_gross_usd"] == "42.00"
    assert results["assumptions"][2]["quote_based_fill_contracts"] == 0
    assert results["assumptions"][2]["report"]["realized_net_usd"] == "0"


def test_replay_is_deterministic_and_does_not_write_input(tmp_path):
    path = tmp_path / "synthetic_input.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_bytes()
    one = replay_from_file(path)
    two = replay_from_file(path)
    assert one == two
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["synthetic_input.json"]


def test_no_bid_ask_means_no_fill_not_fabricated():
    data = sample()
    data["events"] = [e for e in data["events"] if e["kind"] != "quote"]
    r = HistoricalReplay(data).run()
    assert r["not_evaluable_without_quotes"] is True
    assert r["quote_based_fill_contracts"] == 0
    assert r["report"]["completed_positions"] == 0
    assert r["report"]["realized_gross_usd"] == "0"
    assert r["report"]["win_rate"] is None


def test_quote_before_order_is_not_lookahead_fill():
    data = sample()
    data["events"] = [data["events"][1], data["events"][0]]
    data["events"][0]["at_utc"] = "2026-11-12T14:59:59Z"
    data["end_at_utc"] = "2026-11-12T15:00:30Z"
    r = HistoricalReplay(data).run()
    assert r["quote_based_fill_contracts"] == 0
    assert r["report"]["positions"][0]["filled_buy_contracts"] == 0


def test_high_ask_never_chases_exact_swift_limit():
    data = sample()
    for event in data["events"]:
        if event["kind"] == "quote":
            event["bid"] = "1.25"
            event["ask"] = "1.30"
    r = HistoricalReplay(data).run()
    assert r["quote_based_fill_contracts"] == 0
    assert r["report"]["positions"][0]["source_limit"] == "1.20"


def test_slippage_can_prevent_fill_without_repricing_original_limit():
    data = sample()
    data["costs"]["slippage_premium"] = "0.10"
    r = HistoricalReplay(data).run()
    assert r["report"]["positions"][0]["source_limit"] == "1.20"
    assert r["quote_based_fill_contracts"] == 0


def test_missing_fee_keeps_net_unknown_instead_of_assuming_zero():
    data = sample()
    data["costs"].pop("fee_per_contract_side_usd")
    r = HistoricalReplay(data).run()["report"]
    assert r["realized_gross_usd"] == "42.00"
    assert r["realized_net_usd"] is None
    assert r["fees_paid_usd"] is None


def test_partial_entry_fill_and_pending_unfilled_remainder():
    data = sample()
    data["events"][1]["ask_size"] = 1
    data["events"] = data["events"][:2]
    data["end_at_utc"] = "2026-11-12T15:00:40Z"
    r = HistoricalReplay(data).run()["report"]
    assert r["positions"][0]["filled_buy_contracts"] == 1
    assert r["positions"][0]["open_contracts"] == 1
    assert len(r["pending_orders"]) == 1
    assert r["pending_orders"][0]["remaining_contracts"] == 1
    assert r["pending_orders"][0]["countdown_seconds"] == 3560


def test_acceptance_plus_exact_3600_triggers_cancel():
    data = sample()
    data["events"] = [data["events"][0]]
    data["end_at_utc"] = "2026-11-12T16:00:00Z"
    r = HistoricalReplay(data).run()["report"]
    assert r["cancelled_orders"] == 1
    assert r["pending_orders"] == []
    assert r["positions"][0]["filled_buy_contracts"] == 0


def test_invalid_timestamp_or_out_of_order_is_rejected():
    data = sample()
    data["events"][1]["at_utc"] = "2026-11-12T14:00:00Z"
    with pytest.raises(ValueError, match="sorted"):
        HistoricalReplay(data)
    data = sample()
    data["events"][0]["at_utc"] = "2026-11-12T15:00:00"
    with pytest.raises(ValueError, match="timestamp"):
        HistoricalReplay(data)


@pytest.mark.parametrize("change", [
    lambda d: d.update({"channel_id": 1}),
    lambda d: d["risk"].update({"contracts_per_signal": 0}),
    lambda d: d["catalog"][0].update({"strike": "NaN"}),
    lambda d: d["catalog"][0].update({"buying_power_usd": -1}),
    lambda d: d["events"][1].update({"ask_size": "4"}),
    lambda d: d["events"][1].update({"bid": "1.50"}),
    lambda d: d["events"][1].update({"kind": "account_transfer"}),
    lambda d: d.update({"source_type": "VERIFIED_LIVE"}),
    lambda d: d["events"][0].update({"message_id": -9}),
])
def test_replay_rejects_unsupported_or_corrupted_input(change):
    data = copy.deepcopy(sample())
    change(data)
    with pytest.raises((ValueError, KeyError, TypeError)):
        HistoricalReplay(data).run()


def test_unverified_historical_sources_are_labeled_not_promoted():
    data = sample()
    data["source_type"] = "HISTORICAL_UNVERIFIED"
    r = HistoricalReplay(data).run()
    assert r["data_quality"] == "UNVERIFIED_ESTIMATE"
    assert r["report"]["positions"][0]["quote_provenance"] == "EXTERNAL_UNVERIFIED"


def test_session_close_cancels_and_does_not_reopen():
    data = sample()
    data["events"] = [data["events"][0], {
        "kind": "session_close", "at_utc": "2026-11-12T15:00:10Z"
    }, data["events"][1]]
    data["end_at_utc"] = "2026-11-12T15:00:40Z"
    r = HistoricalReplay(data).run()["report"]
    assert r["cancelled_orders"] == 1
    assert r["positions"][0]["filled_buy_contracts"] == 0


def test_no_extra_buy_after_adding_more():
    r = HistoricalReplay(sample()).run()
    pos = r["report"]["positions"]
    assert len(pos) == 1
    assert pos[0]["filled_buy_contracts"] == 2
    assert r["report"]["orders_by_state"] == {"FILLED": 2}


def test_touch_vs_through_assumptions_diverge_at_exact_limit():
    data = sample()
    data["events"][1]["bid"] = "1.18"
    data["events"][1]["ask"] = "1.20"
    x = HistoricalReplay(data).compare()["assumptions"]
    assert x[0]["quote_based_fill_contracts"] == 4
    assert x[1]["quote_based_fill_contracts"] == 0
    assert x[2]["quote_based_fill_contracts"] == 0


def test_ambiguous_ticker_only_exit_cannot_close_two_contracts():
    data = sample()
    additional = {
        "symbol": "SPY", "right": "CALL", "strike": "681",
        "expiration": "2026-11-13", "tick": "0.01",
        "options_permission": True, "market_session_open": True,
        "buying_power_usd": "10000",
    }
    data["catalog"].append(additional)
    data["events"] = [
        data["events"][0],
        {"kind": "message", "at_utc": "2026-11-12T15:00:01Z",
         "message_id": 2, "text": "SPY 681C 11/13 @ .90"},
        data["events"][1],
        {"kind": "quote", "at_utc": "2026-11-12T15:00:35Z", "quote_id": "q-other",
         "contract": {"symbol": "SPY", "right": "CALL",
                      "strike": "681", "expiration": "2026-11-13"},
         "bid": "0.80", "ask": "0.85", "bid_size": 10, "ask_size": 3},
        {"kind": "message", "at_utc": "2026-11-12T15:01:00Z",
         "message_id": 3, "text": "SPY SOLD HALF"},
    ]
    data["end_at_utc"] = "2026-11-12T15:01:01Z"
    report = HistoricalReplay(data).run()["report"]
    assert len(report["positions"]) == 2
    assert sum(p["open_contracts"] for p in report["positions"]) == 4
    assert report["parser_by_status"]["REVIEW_REQUIRED"] == 1
    assert "FILLED" in report["orders_by_state"]
    assert all(p["filled_sell_contracts"] == 0 for p in report["positions"])


def test_explicit_utc_offset_replays_equivalent_instants():
    data = sample()
    for event in data["events"]:
        event["at_utc"] = event["at_utc"].replace("15:", "07:").replace("Z", "-08:00")
    data["end_at_utc"] = data["end_at_utc"].replace("15:", "07:").replace(
        "Z", "-08:00"
    )
    r = HistoricalReplay(data).run()["report"]
    assert r["realized_gross_usd"] == "42.00"


def test_unfilled_orders_report_coverage_without_inventing_missed_market_fills():
    data = sample()
    data["events"] = [data["events"][0]]
    data["end_at_utc"] = "2026-11-12T16:00:00Z"
    result = HistoricalReplay(data).run()
    assert result["unfilled_entry_contracts"] == 2
    assert result["entry_orders_without_quote_coverage"] == 1
    assert "NOT_PROOF" in result["missed_fill_interpretation"]
    data = sample()
    for e in data["events"]:
        if e["kind"] == "quote":
            e["bid"] = "1.25"
            e["ask"] = "1.30"
    result = HistoricalReplay(data).run()
    assert result["unfilled_entry_contracts"] == 2
    assert result["entry_orders_without_quote_coverage"] == 0
