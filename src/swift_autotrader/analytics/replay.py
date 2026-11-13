"""Offline, provenance-aware replay through the existing parser and paper engine.

Every run gets a NEW, temporary SQLite database. Historical source messages are
re-enveloped as live ONLY inside this sealed simulator, never reintroduced to the
real Telegram checkpoint or operator's paper account. Market quotes are required
for fills, and observed timestamps must never lie in the future.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from swift_autotrader.analytics.metrics import CostModel, PaperAnalytics
from swift_autotrader.broker.paper import PaperBroker, PaperMarketQuote, as_utc, dt, iso
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitPolicy, ExitQuote
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog
from swift_autotrader.telegram.events import EventKind, EventOrigin, TelegramMessage

MAX_EVENTS = 20_000
MAX_CONTRACTS = 500


def _instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("UTC timestamp must be a string")
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid or timezone-naive timestamp") from exc


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("input premiums must be exact decimal strings")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid market decimal") from exc
    if not result.is_finite() or (positive and result <= 0) or result < 0:
        raise ValueError("non-finite or out-of-range market decimal")
    return result


def _quantity(value: Any, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError("invalid integral quantity")
    return value


def _contract(data: dict[str, Any]) -> OptionContract:
    try:
        if set(data) - {"symbol", "right", "strike", "expiration", "tick",
                        "options_permission", "market_session_open", "buying_power_usd"}:
            raise ValueError("unknown contract fields")
        return OptionContract(
            data["symbol"], OptionRight(str(data["right"]).upper()),
            _decimal(data["strike"], positive=True), date.fromisoformat(data["expiration"]),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid contract identity") from exc


@dataclass(slots=True)
class ReplayClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class HistoricalReplay:
    """Synchronous bounded simulator: no network, external broker or deployment."""

    def __init__(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("replay document must be a JSON object")
        if data.get("source_type") not in {"SYNTHETIC", "HISTORICAL_UNVERIFIED"}:
            raise ValueError("explicit source_type required")
        if type(data.get("channel_id")) is not int or data["channel_id"] >= -1000000000000:
            raise ValueError("explicit synthetic/authorized signed channel ID required")
        contracts = data.get("catalog")
        events = data.get("events")
        if not isinstance(contracts, list) or len(contracts) > MAX_CONTRACTS:
            raise ValueError("catalog must be a bounded list")
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
            raise ValueError("events must be a nonempty bounded list")
        if not isinstance(data.get("risk"), dict):
            raise ValueError("explicit safe risk ceilings required")
        self.data = data
        self.catalog = {_contract(c): c for c in contracts}
        self.canonical_contracts = {c: c for c in self.catalog}
        if len(self.catalog) != len(contracts):
            raise ValueError("duplicate option instrument in replay catalog")
        self.events = events
        prev = None
        for event in events:
            if not isinstance(event, dict) or event.get("kind") not in {
                "message", "edit", "quote", "session_close", "session_open"
            }:
                raise ValueError("unrecognized replay event")
            at = _instant(event.get("at_utc"))
            if prev is not None and at < prev:
                raise ValueError("input events must be sorted; no implicit lookahead")
            prev = at
        self.start = _instant(events[0]["at_utc"])
        self.end = _instant(data.get("end_at_utc", events[-1]["at_utc"]))
        if self.end < prev:
            raise ValueError("end time predates final event")
        self.identity = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest()

    def _limits(self) -> RiskCeilings:
        r = self.data["risk"]
        return RiskCeilings(
            contracts_per_signal=_quantity(r["contracts_per_signal"]),
            premium_per_signal_usd=_decimal(r["premium_per_signal_usd"]),
            total_exposure_usd=_decimal(r["total_exposure_usd"]),
            open_signal_count=_quantity(r["open_signal_count"]),
            entry_max_age_seconds=120,
        )

    def _preflight(self) -> PaperPreflight:
        checks = {}
        for contract, raw in self.catalog.items():
            checks[contract] = Preflight(
                instrument_verified=True, options_permission=raw["options_permission"] is True,
                market_session_open=raw["market_session_open"] is True,
                available_buying_power_usd=_decimal(raw["buying_power_usd"]),
                premium_tick=_decimal(raw["tick"], positive=True), contract=contract,
            )
        return PaperPreflight(checks)

    @staticmethod
    def _input_costs(data: dict[str, Any], scenario: str) -> CostModel:
        opts = data.get("costs", {})
        if not isinstance(opts, dict):
            raise ValueError("costs must be a JSON object")
        fee = opts.get("fee_per_contract_side_usd")
        return CostModel(
            fee_per_contract_side_usd=_decimal(fee) if fee is not None else None,
            slippage_per_contract_premium=_decimal(opts.get("slippage_premium", "0")),
            fill_assumption=scenario,
            mark_max_age_seconds=int(opts.get("mark_max_age_seconds", 60)),
        )

    def run(self, *, scenario: str = "touch") -> dict[str, Any]:
        costs = self._input_costs(self.data, scenario)
        # No writable data path is ever accepted from a replay manifest.
        with TemporaryDirectory(prefix="swift_offline_replay_") as tmp:
            db_path = Path(tmp) / "isolated_replay.sqlite3"
            apply_migrations(db_path)
            clock = ReplayClock(self.start)
            broker = PaperBroker(db_path, clock=clock)
            preflight = self._preflight()
            engine = PaperOrderEngine(
                db_path, broker, preflight=preflight,
                limits=self._limits(), exit_policy=ExitPolicy(max_quote_age_seconds=30),
                clock=clock,
            )
            store = TelegramStore(db_path, int(self.data["channel_id"]))
            matcher = SignalMatcher(
                db_path, int(self.data["channel_id"]),
                catalog=StaticInstrumentCatalog(self.catalog),
            )
            recently_seen: dict[OptionContract, ExitQuote] = {}
            initial_sent: dict[int, datetime] = {}
            processed_messages = observed_quotes = 0
            fills_from_quotes = 0
            session_started = self.start - timedelta(microseconds=1)
            for index, record in enumerate(self.events):
                when = _instant(record["at_utc"])
                clock.current = when
                kind = record["kind"]
                if kind in {"message", "edit"}:
                    mid = _quantity(record["message_id"])
                    body = record["text"]
                    if not isinstance(body, str) or len(body) > 100_000:
                        raise ValueError("invalid message body")
                    if kind == "message":
                        sent = _instant(record.get("sent_at_utc", record["at_utc"]))
                        initial_sent.setdefault(mid, sent)
                    else:
                        sent = initial_sent.get(mid)
                        if sent is None:
                            raise ValueError("edit without a prior source message")
                    reply = record.get("reply_to_message_id")
                    if reply is not None:
                        reply = _quantity(reply)
                    message = TelegramMessage(
                        channel_id=int(self.data["channel_id"]), message_id=mid,
                        sent_at_utc=sent, received_at_utc=when, body=body,
                        kind=EventKind.EDIT if kind == "edit" else EventKind.NEW,
                        origin=EventOrigin.LIVE, edited_at_utc=when if kind == "edit" else None,
                        reply_to_message_id=reply,
                    )
                    store.ingest(message, session_started_at_utc=session_started,
                                 max_live_age_seconds=600)
                    matcher.process_pending()
                    processed_messages += 1
                    # This dispatch is solely in the isolated temporary DB.
                    engine.run_cycle(quotes=recently_seen)
                elif kind == "quote":
                    raw_contract = record.get("contract")
                    if not isinstance(raw_contract, dict):
                        raise ValueError("quote must include exact option contract")
                    contract = _contract(raw_contract)
                    if contract not in self.catalog:
                        raise ValueError("quote instrument absent from explicit catalog")
                    contract = self.canonical_contracts[contract]
                    bid = _decimal(record["bid"], positive=True)
                    ask = _decimal(record["ask"], positive=True)
                    bid_size = _quantity(record["bid_size"], allow_zero=True)
                    ask_size = _quantity(record["ask_size"], allow_zero=True)
                    if bid > ask:
                        raise ValueError("crossed option quote")
                    quote_id = record.get("quote_id", f"evidence-{index}")
                    if not isinstance(quote_id, str) or not quote_id:
                        raise ValueError("quote must have an identity")
                    provenance = self.data.get("source_type")
                    if provenance == "HISTORICAL_UNVERIFIED":
                        provenance = "EXTERNAL_UNVERIFIED"
                    source_reference = record.get("source_reference")
                    if source_reference is not None and (
                        not isinstance(source_reference, str)
                        or not 1 <= len(source_reference) <= 512
                    ):
                        raise ValueError("invalid quote evidence reference")
                    evidence_hash = (hashlib.sha256(source_reference.encode()).hexdigest()
                                     if source_reference else None)
                    with sqlite3.connect(db_path) as db:
                        db.execute(
                            "INSERT INTO paper_market_observations(" 
                            "quote_id,underlying,option_right,strike,expiration_date,bid,ask,"
                            "bid_size,ask_size,observed_at_utc,provenance,source_reference) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (quote_id, contract.underlying, contract.right.value,
                             str(contract.strike), contract.expiration.isoformat(),
                             str(bid), str(ask), bid_size, ask_size, iso(when), provenance,
                             evidence_hash),
                        )
                    observed_quotes += 1
                    # A previously resting order may cross this *new* quote.
                    # An exit created only after this quote cannot retro-fill on it.
                    session_open = preflight.check(contract).market_session_open
                    if scenario != "no_fills" and session_open:
                        min_improvement = (preflight.check(contract).premium_tick
                                           if scenario == "through" else Decimal(0))
                        fills_from_quotes += broker.observe_quote(
                            PaperMarketQuote(quote_id, contract, bid, ask,
                                             bid_size, ask_size, when),
                            slippage=costs.slippage_per_contract_premium,
                            min_improvement=min_improvement,
                        )
                    if session_open:
                        recently_seen[contract] = ExitQuote(
                            contract, bid, preflight.check(contract).premium_tick, when,
                        )
                    engine.run_cycle(quotes=recently_seen)
                else:
                    is_open = kind == "session_open"
                    for contract in self.catalog:
                        preflight.instruments[contract] = replace(
                            preflight.instruments[contract], market_session_open=is_open,
                        )
                    recently_seen.clear()
                    if not is_open:
                        broker.close_session()  # GFD cancellation, no automatic resubmit
                    engine.run_cycle()
            clock.current = self.end
            engine.run_cycle(quotes={})
            report = PaperAnalytics(db_path, costs=costs).snapshot(as_of=self.end)
            with sqlite3.connect(db_path) as db:
                db.row_factory = sqlite3.Row
                entry_orders = db.execute(
                    "SELECT o.requested_quantity,o.filled_quantity,o.state,"
                    "o.accepted_at_utc,o.expires_at_utc,s.underlying,s.option_right,"
                    "s.strike,s.expiration_date FROM order_intents o "
                    "JOIN option_signals s ON s.signal_id=o.signal_id "
                    "WHERE o.side='BUY_TO_OPEN'"
                ).fetchall()
                unfilled_contracts = sum(int(r[0]) - int(r[1]) for r in entry_orders)
                missing_quote_coverage = 0
                for order in entry_orders:
                    accepted = dt(order["accepted_at_utc"])
                    if accepted is None:
                        missing_quote_coverage += 1
                        continue
                    end = min(self.end, dt(order["expires_at_utc"]) or self.end)
                    observed = db.execute(
                        "SELECT 1 FROM paper_market_observations "
                        "WHERE underlying=? AND option_right=? AND strike=? "
                        "AND expiration_date=? AND observed_at_utc>=? "
                        "AND observed_at_utc<=? LIMIT 1",
                        (order["underlying"], order["option_right"], order["strike"],
                         order["expiration_date"], iso(accepted), iso(end)),
                    ).fetchone()
                    if observed is None:
                        missing_quote_coverage += 1
                db.execute(
                    "INSERT INTO replay_runs(run_id,input_sha256,source_type,assumption,"
                    "started_at_utc,end_at_utc,evidence_quality) VALUES(?,?,?,?,?,?,?)",
                    (self.identity[:32] + ":" + scenario, self.identity,
                     self.data["source_type"], scenario, iso(self.start), iso(self.end),
                     "UNVERIFIED_ESTIMATE"),
                )
            return {
                "scenario": scenario, "source_type": self.data["source_type"],
                "input_sha256": self.identity,
                "data_quality": "UNVERIFIED_ESTIMATE",
                "instrument_catalog_quality": "USER_SUPPLIED_NOT_BROKER_VERIFIED",
                "message_events": processed_messages,
                "quote_observations": observed_quotes,
                "quote_based_fill_contracts": fills_from_quotes,
                "unfilled_resting_orders": len(report["pending_orders"]),
                "unfilled_entry_contracts": unfilled_contracts,
                "entry_orders_without_quote_coverage": missing_quote_coverage,
                "missed_fill_interpretation": (
                    "UNFILLED_IS_NOT_PROOF_MARKET_FILL_WAS_POSSIBLE"
                ),
                "not_evaluable_without_quotes": observed_quotes == 0,
                "report": report,
            }

    def compare(self) -> dict[str, Any]:
        return {"input_sha256": self.identity, "mode": "PAPER_ONLY",
                "assumptions": [self.run(scenario=name) for name in
                                ("touch", "through", "no_fills")]}


def replay_from_file(path: Path, *, compare: bool = False) -> dict[str, Any]:
    """Local JSON input only; never read Telegram session, broker or production DB."""
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 4_000_000:
        raise ValueError("replay JSON missing or exceeds local safety budget")
    data = json.loads(source.read_text(encoding="utf-8"))
    job = HistoricalReplay(data)
    return job.compare() if compare else job.run()
