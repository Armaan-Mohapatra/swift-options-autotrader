"""Fail-closed, continuously repeatable Telegram -> PAPER trading orchestration.

Only verified read-only observations can authorize entry or quote-based fills.
The durable message store, SignalMatcher, PaperOrderEngine and PaperBroker are
reused unchanged apart from the engine's explicit `allow_entries` switch.
No real brokerage order/cancel interface is ever imported by this module.
"""
from __future__ import annotations

from contextlib import closing
import asyncio
import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, date
from decimal import Decimal
from pathlib import Path
from time import monotonic
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
        self._pending_rows: list[tuple[int, ParsedAlert, bool, datetime, int | None]] = []

    def _pending_and_active(self) -> tuple[list[ParsedAlert], list[OptionContract]]:
        """Select recent source events fairly; never use canceled zero-fill legs.

        Descending index selection prevents an ancient, unverifiable source
        backlog from starving fresh exits and entries. Decisions are later
        committed in original revision order for replay/idempotency.
        """
        with closing(sqlite3.connect(self.db_path)) as db, db:
            pending = db.execute(
                "SELECT r.revision_id,e.body,r.eligible_for_future_processing,"
                "r.telegram_sent_at_utc,r.reply_to_message_id "
                "FROM telegram_message_revisions r "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                "WHERE r.channel_id=? AND p.revision_id IS NULL "
                "ORDER BY r.revision_id DESC LIMIT 2000",
                (str(self.channel_id),),
            ).fetchall()
            active_rows = db.execute(
                "SELECT DISTINCT s.underlying,s.option_right,s.strike,s.expiration_date "
                "FROM option_signals s JOIN order_intents o ON o.signal_id=s.signal_id "
                "WHERE o.side='BUY_TO_OPEN' AND ("
                "o.state IN ('ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING') "
                "OR (SELECT COALESCE(SUM(f.filled_quantity),0) FROM order_fills f "
                "WHERE f.logical_order_id=o.logical_order_id) > "
                "(SELECT COALESCE(SUM(f2.filled_quantity),0) FROM order_fills f2 "
                "JOIN order_intents o2 ON f2.logical_order_id=o2.logical_order_id "
                "WHERE o2.signal_id=o.signal_id AND o2.side='SELL_TO_CLOSE')) "
            ).fetchall()
        from swift_autotrader.signals.identity import OptionRight
        active = [OptionContract(row[0], OptionRight(row[1]), Decimal(row[2]),
                                 date.fromisoformat(row[3])) for row in active_rows]
        self._pending_rows = [
            (int(rid), parse_message(body), bool(eligible),
             datetime.fromisoformat(str(sent).replace("Z", "+00:00")),
             int(reply) if reply is not None else None)
            for rid, body, eligible, sent, reply in reversed(pending)
        ]
        now = as_utc(self.clock())
        entries = [alert for _rid, alert, eligible, sent, _reply in self._pending_rows
                   if eligible and alert.intent is SignalIntent.ENTRY
                   and 0 <= (now - as_utc(sent)).total_seconds()
                   <= self.limits.entry_max_age_seconds]
        return entries, active

    def _recoverable_entries(self, now: datetime) -> list[tuple[str, ParsedAlert, OptionContract]]:
        """Revalidate durable, previously parsed unexecuted entries after a crash.

        No persisted parser decision alone grants permission to execute. Each
        recovered signal must receive a fresh independent contract/quote review.
        """
        with closing(sqlite3.connect(self.db_path)) as db, db:
            rows = db.execute(
                "SELECT s.signal_id,s.underlying,s.option_right,s.strike,"
                "s.expiration_date,e.body,r.telegram_sent_at_utc "
                "FROM option_signals s "
                "JOIN signal_metadata m ON m.signal_id=s.signal_id "
                "JOIN telegram_message_revisions r ON r.revision_id=m.first_revision_id "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "JOIN signal_parser_decisions p ON p.revision_id=m.first_revision_id "
                "WHERE r.channel_id=? AND r.eligible_for_future_processing=1 "
                "AND p.status='RECORDED' AND p.intent='ENTRY' "
                "AND NOT EXISTS(SELECT 1 FROM order_intents o WHERE o.signal_id=s.signal_id) "
                "AND NOT EXISTS(SELECT 1 FROM paper_engine_decisions d "
                "WHERE d.signal_id=s.signal_id) "
                "AND NOT EXISTS(SELECT 1 FROM signal_close_requests c "
                "WHERE c.signal_id=s.signal_id) "
                "ORDER BY m.first_revision_id DESC LIMIT 48",
                (str(self.channel_id),),
            ).fetchall()
        from swift_autotrader.signals.identity import OptionRight
        eligible: list[tuple[str, ParsedAlert, OptionContract]] = []
        for sid, symbol, right, strike, expiry, body, sent_at in reversed(rows):
            sent = datetime.fromisoformat(str(sent_at).replace('Z', '+00:00'))
            if not 0 <= (now - as_utc(sent)).total_seconds() <= self.limits.entry_max_age_seconds:
                continue
            contract = OptionContract(symbol, OptionRight(right), Decimal(strike),
                                      date.fromisoformat(expiry))
            alert = parse_message(body)
            if alert.intent is SignalIntent.ENTRY:
                eligible.append((sid, alert, contract))
        return eligible

    def _record_quote(self, quote: VerifiedQuote) -> None:
        evidence_hash = hashlib.sha256(quote.evidence_reference.encode("utf-8")).hexdigest()
        provenance = "SYNTHETIC" if quote.source == "SYNTHETIC_TEST" else "EXTERNAL_UNVERIFIED"
        with closing(sqlite3.connect(self.db_path)) as db, db:
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
        """Process exits independently, and isolate unverifiable entry quotes.

        No missing entry data may block a known exit, fresh newer entry, or
        cancellation watchdog. A source entry is checkpointed only after its
        precise contract and matching quote pass verification.
        """
        now = as_utc(self.clock())
        # Run the local order watchdog BEFORE optional external read-only data.
        # A long quote lookup must never postpone a due entry cancellation.
        watchdog = self.engine.run_cycle(allow_entries=False, process_closes=False)
        _all_entries, active = self._pending_and_active()
        rows = self._pending_rows
        matcher = SignalMatcher(self.db_path, self.channel_id)
        # A follow-up replying to an unprocessed BUY must wait for that BUY
        # to be verified first. An already linked position is actionable.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            unlinked = {
                int(row[0]) for row in db.execute(
                    "SELECT r.message_id FROM telegram_message_revisions r "
                    "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                    "WHERE r.channel_id=? AND p.revision_id IS NULL "
                    "AND r.event_kind='new'",
                    (str(self.channel_id),),
                )
            }
        pending_entry_messages = {
            int(message) for message in unlinked
        }
        # Keep exit and status interpretation independent of the option feed.
        followup_ids = [
            rid for rid, alert, _eligible, _sent, reply in rows
            if alert.intent is not SignalIntent.ENTRY
            and (reply is None or reply not in pending_entry_messages)
        ][:500]
        decisions = matcher.process_pending(revision_ids=followup_ids)
        # Establish active-contract quote evidence separately so failure in a
        # new BUY candidate cannot prevent an existing matching exit.
        snapshots: list[MarketSnapshot] = []
        failed = False

        # A provider outage must not produce an unbounded binary-search storm.
        # Both RPC count and elapsed wall time bound each poll, even if every
        # candidate is unverifiable and the recursive isolation keeps splitting.
        cycle_deadline = monotonic() + 20.0
        provider_calls = 0

        async def fetch(batch: Sequence[ParsedAlert], related: Sequence[OptionContract]):
            nonlocal provider_calls
            remaining = cycle_deadline - monotonic()
            if provider_calls >= 24 or remaining <= 0:
                raise MarketEvidenceUnavailable("Read-only market provider poll budget exhausted")
            provider_calls += 1
            item = await asyncio.wait_for(
                self.provider.snapshot(batch, related, now),
                timeout=min(12.0, remaining),
            )
            item.validate(now, max_age_seconds=self.quote_max_age_seconds)
            return item

        if active:
            try:
                snapshots.append(await fetch([], active))
            except (MarketEvidenceUnavailable, OSError, TimeoutError, RuntimeError, ValueError):
                failed = True
        eligible_rows = [
            (rid, alert) for rid, alert, eligible, sent, _reply in rows
            if eligible and alert.intent is SignalIntent.ENTRY
            and 0 <= (now - as_utc(sent)).total_seconds()
            <= self.limits.entry_max_age_seconds
        ]
        # Bound per-cycle network work. Unprocessed events remain in the
        # durable store and will be revisited next cycle, not falsely skipped.
        recovered = self._recoverable_entries(now)
        # Give previously durable, unsubmitted source signals the same fresh
        # read-only review as new signals, without exceeding the network cap.
        recovery_candidates = [(None, alert, sid, contract)
                               for sid, alert, contract in recovered]
        available_slots = max(0, 48 - len(recovery_candidates))
        eligible_rows = eligible_rows[:available_slots]
        candidates = recovery_candidates + [(rid, alert, None, None)
                                             for rid, alert in eligible_rows]
        verified_ids: list[int] = []
        verified_recovery_ids: set[str] = set()
        # Batch and bisect to isolate one bad contract, without hammering a
        # provider for every ordinary BUY. A group never exceeds the cap of 12.
        async def review(group: list[tuple[int | None, ParsedAlert, str | None,
                                            OptionContract | None]]) -> None:
            nonlocal failed
            if not group:
                return
            try:
                snap = await fetch([alert for _rid, alert, _sid, _expected in group], [])
                catalog = StaticInstrumentCatalog(snap.instruments)
                for _rid, alert, _sid, expected in group:
                    contract, reason = resolve_entry(alert, catalog, as_of=now.date())
                    if (contract is None or reason is not None
                        or contract not in snap.quotes
                        or (expected is not None and contract != expected)):
                        raise MarketEvidenceUnavailable("Unverified exact fresh entry quote")
                snapshots.append(snap)
                verified_ids.extend(rid for rid, _alert, _sid, _expected in group
                                    if rid is not None)
                verified_recovery_ids.update(sid for _rid, _alert, sid, _expected in group
                                             if sid is not None)
            except (MarketEvidenceUnavailable, OSError, TimeoutError, RuntimeError, ValueError):
                failed = True
                if len(group) > 1:
                    half = len(group) // 2
                    await review(group[:half])
                    await review(group[half:])

        for index in range(0, len(candidates), 12):
            await review(candidates[index:index + 12])

        instruments: dict[OptionContract, Preflight] = {}
        quotes: dict[OptionContract, VerifiedQuote] = {}
        for snap in snapshots:
            if snap.source not in {"SYNTHETIC_TEST", "MCP_READONLY"}:
                failed = True
                continue
            instruments.update(snap.instruments)
            for contract, quote in snap.quotes.items():
                prev = quotes.get(contract)
                if prev is None or quote.observed_at_utc >= prev.observed_at_utc:
                    quotes[contract] = quote
        if verified_ids:
            catalog = StaticInstrumentCatalog(instruments)
            # The parser admits at most 500 explicit revision IDs per call.
            # Keep this bound even if the per-cycle network cap is raised.
            for offset in range(0, len(verified_ids), 500):
                decisions.extend(SignalMatcher(self.db_path, self.channel_id,
                                               catalog=catalog).process_pending(
                                                   revision_ids=verified_ids[offset:offset + 500],
                                               ))
        # A pending reply to an entry can now be resolved in the same cycle.
        if verified_ids:
            remaining = [rid for rid, alert, _eligible, _sent, _reply in rows
                         if alert.intent is not SignalIntent.ENTRY
                         and rid not in followup_ids]
            decisions.extend(SignalMatcher(self.db_path, self.channel_id,
                                           catalog=StaticInstrumentCatalog(instruments)
                                           ).process_pending(revision_ids=remaining[:500]))
        self.preflight.instruments.clear()
        self.preflight.instruments.update(instruments)
        exit_quotes: dict[OptionContract, ExitQuote] = {}
        fill_count = 0
        for contract, quote in quotes.items():
            self._record_quote(quote)
            check = instruments[contract]
            if not check.market_session_open:
                continue
            fill_count += self.broker.observe_quote(
                PaperMarketQuote(quote.quote_id, contract, quote.bid, quote.ask,
                                 quote.bid_size, quote.ask_size, quote.observed_at_utc),
                max_age_seconds=self.quote_max_age_seconds,
            )
            exit_quotes[contract] = ExitQuote(
                contract, quote.bid, check.premium_tick, quote.observed_at_utc,
            )
        if failed:
            self.notifications.record("APPLICATION", "MARKET_DATA_UNAVAILABLE",
                                      source_key="paper-dispatch")
        verified_signal_ids = set(verified_recovery_ids)
        verified_signal_ids.update(
            str(decision.signal_id) for decision in decisions
            if decision.intent == 'ENTRY' and decision.status == 'RECORDED'
            and decision.signal_id is not None
        )
        cycle = self.engine.run_cycle(
            quotes=exit_quotes, allow_entries=bool(verified_signal_ids),
            allowed_signal_ids=frozenset(verified_signal_ids),
        )
        status = (
            "PAPER_ONLY" if verified_signal_ids or (not failed and snapshots) or decisions
            else ("PAUSED_DATA_UNAVAILABLE" if failed else "PAPER_ONLY")
        )
        return DispatchResult(status, len(decisions), cycle.entry_submissions,
                              cycle.close_submissions, fill_count,
                              watchdog.cancellation_requests + cycle.cancellation_requests)


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
