"""Fail-closed, continuously repeatable Telegram -> PAPER trading orchestration.

Only verified read-only observations can authorize entry or quote-based fills.
The durable message store, SignalMatcher, PaperOrderEngine and PaperBroker are
reused unchanged apart from the engine's explicit `allow_entries` switch.
No real brokerage order/cancel interface is ever imported by this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol

from swift_autotrader.broker.paper import PaperBroker, PaperMarketQuote, as_utc, iso
from swift_autotrader.notifications.alerts import NotificationCenter
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitQuote
from swift_autotrader.orders.risk import PaperPreflight, Preflight, RiskCeilings
from swift_autotrader.parser.rules import ParsedAlert, SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog, resolve_entry


class MarketEvidenceUnavailable(RuntimeError):
    """Missing, stale, ambiguous, unsupported or disconnected read-only market feed."""


@dataclass(frozen=True, slots=True)
class VerifiedQuote:
    quote_id: str
    contract: OptionContract
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    observed_at_utc: datetime
    source: str  # SYNTHETIC_TEST or MCP_READONLY; never user-relabelled in production
    evidence_reference: str = ""

    def validate(self, now: datetime, *, max_age_seconds: int = 15) -> None:
        if not self.quote_id or len(self.quote_id) > 180:
            raise MarketEvidenceUnavailable("Quote identity unavailable")
        if self.source not in {"SYNTHETIC_TEST", "MCP_READONLY"}:
            raise MarketEvidenceUnavailable("Unrecognized market provenance")
        if not self.evidence_reference or len(self.evidence_reference) > 256:
            raise MarketEvidenceUnavailable("Source evidence reference absent")
        if type(self.bid_size) is not int or type(self.ask_size) is not int or (
            self.bid_size < 0 or self.ask_size < 0
        ):
            raise MarketEvidenceUnavailable("Market depth unavailable")
        if any(not v.is_finite() or v <= 0 for v in (self.bid, self.ask)) or (
            self.bid > self.ask
        ):
            raise MarketEvidenceUnavailable("Invalid bid/ask")
        age = (as_utc(now) - as_utc(self.observed_at_utc)).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise MarketEvidenceUnavailable("Quote stale or future-dated")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A full, atomic snapshot from one independently authorized read-only provider.

    A missing instrument (or market status) means NOT READY, not an absent
    contract that the parser may permanently label REVIEW_REQUIRED.
    """

    instruments: Mapping[OptionContract, Preflight]
    quotes: Mapping[OptionContract, VerifiedQuote]
    source: str
    as_of_utc: datetime

    def validate(self, now: datetime, *, max_age_seconds: int = 15) -> None:
        if self.source not in {"SYNTHETIC_TEST", "MCP_READONLY"}:
            raise MarketEvidenceUnavailable("Unknown instrument data source")
        age = (as_utc(now) - as_utc(self.as_of_utc)).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise MarketEvidenceUnavailable("Instrument snapshot is stale")
        for contract, check in self.instruments.items():
            if not isinstance(contract, OptionContract) or check.contract != contract:
                raise MarketEvidenceUnavailable("Incorrect contract preflight")
            if not check.instrument_verified or not check.options_permission:
                raise MarketEvidenceUnavailable("Contract or account unverified")
            if (not check.premium_tick.is_finite() or check.premium_tick <= 0 or
                not check.available_buying_power_usd.is_finite() or
                check.available_buying_power_usd < 0):
                raise MarketEvidenceUnavailable("Tick or buying power unknown")
        for contract, quote in self.quotes.items():
            if quote.contract != contract or contract not in self.instruments:
                raise MarketEvidenceUnavailable("Quote/contract mismatch")
            if quote.source != self.source:
                raise MarketEvidenceUnavailable("Quote provenance inconsistent")
            quote.validate(now, max_age_seconds=max_age_seconds)


class ReadOnlyOptionData(Protocol):
    """The implementation must never connect to a broker write endpoint."""

    async def snapshot(
        self, entries: Sequence[ParsedAlert], active: Sequence[OptionContract],
        now: datetime,
    ) -> MarketSnapshot: ...


@dataclass(frozen=True, slots=True)
class DispatchResult:
    status: str
    parsed_events: int
    entry_orders: int
    exit_orders: int
    simulated_fills: int
    cancellations: int
    broker_writes: int = 0


class PaperDispatcher:
    """One safe event/quote cycle. A Windows supervisor runs it repeatedly.

    Missing market evidence never consumes unparsed source revisions or opens a
    simulated position; watchdog and cancellation reconciliation still proceed.
    """

    def __init__(
        self, db_path: Path, channel_id: int, provider: ReadOnlyOptionData,
        *, limits: RiskCeilings, clock: Callable[[], datetime] | None = None,
        quote_max_age_seconds: int = 15,
    ) -> None:
        if provider is None:
            raise ValueError("Explicit read-only provider required")
        if not 1 <= quote_max_age_seconds <= 30:
            raise ValueError("Quote freshness limit outside bounds")
        self.db_path = Path(db_path)
        self.channel_id = channel_id
        self.provider = provider
        self.limits = limits
        self.clock = clock or (lambda: datetime.now(UTC))
        self.quote_max_age_seconds = quote_max_age_seconds
        self.broker = PaperBroker(self.db_path, clock=self.clock)
        self.preflight = PaperPreflight()
        self.engine = PaperOrderEngine(
            self.db_path, self.broker, preflight=self.preflight,
            limits=self.limits, clock=self.clock,
        )
        self.notifications = NotificationCenter(self.db_path)

    def _pending_and_active(self) -> tuple[list[ParsedAlert], list[OptionContract]]:
        with sqlite3.connect(self.db_path) as db:
            pending = db.execute(
                "SELECT e.body,r.eligible_for_future_processing "
                "FROM telegram_message_revisions r "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                "WHERE r.channel_id=? AND p.revision_id IS NULL "
                "ORDER BY r.revision_id LIMIT 500",
                (str(self.channel_id),),
            ).fetchall()
            active_rows = db.execute(
                "SELECT DISTINCT s.underlying,s.option_right,s.strike,s.expiration_date "
                "FROM option_signals s JOIN order_intents o ON o.signal_id=s.signal_id "
                "WHERE s.state NOT IN ('CLOSED','REJECTED')"
            ).fetchall()
        from datetime import date
        from swift_autotrader.signals.identity import OptionRight
        active = [OptionContract(r[0], OptionRight(r[1]), Decimal(r[2]),
                                 date.fromisoformat(r[3])) for r in active_rows]
        entries = []
        for body, eligible in pending:
            alert = parse_message(body)
            if eligible and alert.intent is SignalIntent.ENTRY:
                entries.append(alert)
        return entries, active

    def _record_quote(self, quote: VerifiedQuote) -> None:
        evidence_hash = hashlib.sha256(quote.evidence_reference.encode("utf-8")).hexdigest()
        provenance = "SYNTHETIC" if quote.source == "SYNTHETIC_TEST" else "EXTERNAL_UNVERIFIED"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO paper_market_observations("
                "quote_id,underlying,option_right,strike,expiration_date,bid,ask,"
                "bid_size,ask_size,observed_at_utc,provenance,source_reference)"
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (quote.quote_id, quote.contract.underlying, quote.contract.right.value,
                 str(quote.contract.strike), quote.contract.expiration.isoformat(),
                 str(quote.bid), str(quote.ask), quote.bid_size, quote.ask_size,
                 iso(quote.observed_at_utc), provenance, evidence_hash),
            )

    async def run_once(self) -> DispatchResult:
        now = as_utc(self.clock())
        entries, active = self._pending_and_active()
        try:
            snapshot = await asyncio.wait_for(
                self.provider.snapshot(entries, active, now), timeout=12,
            )
            snapshot.validate(now, max_age_seconds=self.quote_max_age_seconds)
            catalog = StaticInstrumentCatalog(snapshot.instruments)
            for alert in entries:
                contract, reason = resolve_entry(alert, catalog, as_of=now.date())
                if contract is None or reason is not None:
                    raise MarketEvidenceUnavailable("Entry contract unresolved; preserve source")
                if contract not in snapshot.quotes:
                    raise MarketEvidenceUnavailable("Fresh quoted market data required for entry")
        except (MarketEvidenceUnavailable, OSError, TimeoutError, RuntimeError, ValueError):
            self.notifications.record(
                "APPLICATION", "MARKET_DATA_UNAVAILABLE", source_key="paper-dispatch",
            )
            # Reconcile existing orders and 3,600-second cancellations WITHOUT
            # consuming source or opening a new entry on empty preflight data.
            cycle = self.engine.run_cycle(allow_entries=False)
            return DispatchResult("PAUSED_DATA_UNAVAILABLE", 0, 0, 0, 0,
                                  cycle.cancellation_requests)

        self.preflight.instruments.clear()
        self.preflight.instruments.update(snapshot.instruments)
        matcher = SignalMatcher(self.db_path, self.channel_id, catalog=catalog)
        decisions = matcher.process_pending(limit=500)
        exit_quotes: dict[OptionContract, ExitQuote] = {}
        fill_count = 0
        for contract, quote in snapshot.quotes.items():
            self._record_quote(quote)
            check = snapshot.instruments[contract]
            if not check.market_session_open:
                continue
            # A quote observed before paper order acceptance cannot retro-fill.
            fill_count += self.broker.observe_quote(
                PaperMarketQuote(quote.quote_id, contract, quote.bid, quote.ask,
                                 quote.bid_size, quote.ask_size, quote.observed_at_utc),
                max_age_seconds=self.quote_max_age_seconds,
            )
            exit_quotes[contract] = ExitQuote(
                contract, quote.bid, check.premium_tick, quote.observed_at_utc,
            )
        # Reconcile previous fills, issue exits, then create newly eligible BUYs.
        cycle = self.engine.run_cycle(quotes=exit_quotes)
        return DispatchResult("PAPER_ONLY", len(decisions), cycle.entry_submissions,
                              cycle.close_submissions, fill_count, cycle.cancellation_requests)


async def supervise_dispatch(
    dispatcher: PaperDispatcher, *, interval_seconds: float = 5.0,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Bounded no-busy-loop worker; process crash recovery uses durable SQLite."""
    if not 1 <= interval_seconds <= 60:
        raise ValueError("Dispatch interval outside supported bounds")
    while stop is None or not stop():
        await dispatcher.run_once()
        await asyncio.sleep(interval_seconds)
