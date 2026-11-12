"""Deterministic, transactionally durable Phase 2 signal association.

No broker imports or execution paths exist in this module. All ambiguous
references are kept for human review, never heuristically closed.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from swift_autotrader.parser.rules import (
    ExpirationHint,
    ParsedAlert,
    SignalIntent,
    parse_message,
)
from swift_autotrader.signals.identity import OptionContract, OptionRight


class InstrumentCatalog(Protocol):
    """Phase 4 must replace the test catalog with *verified* real instruments."""

    def find(
        self, underlying: str, right: OptionRight, strike: Decimal,
    ) -> Sequence[OptionContract]: ...


class StaticInstrumentCatalog:
    """Finite, supplied instrument set. Synthetic in CI; not market validation."""

    def __init__(self, contracts: Iterable[OptionContract] = ()) -> None:
        self._contracts = frozenset(contracts)

    def find(
        self, underlying: str, right: OptionRight, strike: Decimal,
    ) -> Sequence[OptionContract]:
        return sorted(
            (
                c for c in self._contracts
                if (c.underlying, c.right, c.strike) == (underlying, right, strike)
            ),
            key=lambda c: c.expiration,
        )


def _match_date(hint: ExpirationHint, expiration: date) -> bool:
    return (
        (hint.month, hint.day) == (expiration.month, expiration.day)
        and (hint.year is None or hint.year == expiration.year)
    )


def resolve_entry(
    alert: ParsedAlert, catalog: InstrumentCatalog | None, *, as_of: date,
) -> tuple[OptionContract | None, str | None]:
    """An omitted year is NEVER guessed; exactly one verified listing must match."""
    if not alert.has_contract or alert.premium is None:
        return None, "INCOMPLETE_ENTRY"
    if catalog is None:
        return None, "INSTRUMENT_CATALOG_REQUIRED"
    assert alert.underlying and alert.right and alert.strike and alert.expiry
    matches = [
        contract for contract in catalog.find(
            alert.underlying, alert.right, alert.strike,
        )
        if _match_date(alert.expiry, contract.expiration)
        and contract.expiration >= as_of
    ]
    if len(matches) != 1:
        return None, "CONTRACT_NOT_FOUND" if not matches else "AMBIGUOUS_EXPIRY_YEAR"
    return matches[0], None


@dataclass(frozen=True, slots=True)
class Decision:
    revision_id: int
    message_id: int
    intent: str
    status: str
    reason: str | None = None
    signal_id: str | None = None


def _signal_id(channel_id: str, message_id: int) -> str:
    return hashlib.sha256(f"swift:{channel_id}:{message_id}".encode()).hexdigest()[:32]


class SignalMatcher:
    """Incrementally interprets *persisted* Telegram revisions for one channel.

    Every revision is classified once under BEGIN IMMEDIATE. All inputs come
    from the Phase 1 message store, which has already applied its freshness
    guards. Edits are reviewed or metadata-only; stale events never open or
    close positions. Does not call a broker, Telegram, or any network API.
    """

    def __init__(
        self, db_path: Path, channel_id: int,
        *, catalog: InstrumentCatalog | None = None,
    ) -> None:
        if isinstance(channel_id, bool) or channel_id == 0:
            raise ValueError("channel_id must be a nonzero integer")
        self.db_path = Path(db_path)
        self.channel_id = str(channel_id)
        self.catalog = catalog

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise RuntimeError("Run migrate before parsing")
        con = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='signal_parser_decisions'"
        ).fetchone() is None:
            con.close()
            raise RuntimeError("Phase 2 migration required; run migrate")
        return con

    def process_pending(self, limit: int = 500) -> list[Decision]:
        if not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self._connect() as con:
            revisions = [
                int(row[0]) for row in con.execute(
                    "SELECT r.revision_id FROM telegram_message_revisions r "
                    "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                    "WHERE r.channel_id=? AND p.revision_id IS NULL "
                    "ORDER BY r.revision_id LIMIT ?", (self.channel_id, limit),
                )
            ]
        outcomes: list[Decision] = []
        for revision_id in revisions:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                if con.execute(
                    "SELECT 1 FROM signal_parser_decisions WHERE revision_id=?",
                    (revision_id,),
                ).fetchone() is not None:
                    continue
                row = con.execute(
                    "SELECT r.revision_id,r.channel_id,r.message_id,r.event_kind,"
                    "r.origin,r.telegram_sent_at_utc,r.telegram_edited_at_utc,"
                    "r.reply_to_message_id,r.eligible_for_future_processing,"
                    "r.ignored_reason,e.body,m.latest_revision_id "
                    "FROM telegram_message_revisions r "
                    "JOIN raw_events e ON e.event_id=r.raw_event_id "
                    "JOIN telegram_messages m ON m.channel_id=r.channel_id "
                    "AND m.message_id=r.message_id "
                    "WHERE r.revision_id=? AND r.channel_id=?",
                    (revision_id, self.channel_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Missing Telegram revision for parser")
                outcome = self._interpret(con, row)
                con.execute(
                    "INSERT INTO signal_parser_decisions "
                    "(revision_id,channel_id,message_id,intent,status,reason,signal_id,"
                    "processed_at_utc) VALUES(?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (
                        outcome.revision_id, self.channel_id, outcome.message_id,
                        outcome.intent, outcome.status, outcome.reason, outcome.signal_id,
                    ),
                )
                if outcome.status == "REVIEW_REQUIRED":
                    con.execute(
                        "INSERT INTO signal_review_queue(revision_id,reason) VALUES(?,?)",
                        (revision_id, outcome.reason or "UNSPECIFIED_REVIEW"),
                    )
                outcomes.append(outcome)
        return outcomes

    def _result(
        self, row: sqlite3.Row, alert: ParsedAlert, status: str,
        reason: str | None = None, signal_id: str | None = None,
    ) -> Decision:
        return Decision(int(row["revision_id"]), int(row["message_id"]),
                        alert.intent.value, status, reason, signal_id)

    def _interpret(self, con: sqlite3.Connection, row: sqlite3.Row) -> Decision:
        alert = parse_message(row["body"])
        rid = int(row["revision_id"])
        mid = int(row["message_id"])
        is_edit = row["event_kind"] == "edit"
        # Avoid creating actionable intent from outdated or superseded source.
        if not is_edit and rid != int(row["latest_revision_id"]):
            return self._result(row, alert, "SKIPPED", "SUPERSEDED_REVISION")
        if is_edit:
            return self._handle_edit(con, row, alert)
        if alert.intent == SignalIntent.IGNORE_NON_OPTION:
            return self._result(row, alert, "IGNORED", alert.reason)
        if alert.intent == SignalIntent.REVIEW_REQUIRED:
            return self._result(row, alert, "REVIEW_REQUIRED", alert.reason)
        if not bool(row["eligible_for_future_processing"]):
            return self._result(row, alert, "SKIPPED", "NON_ACTIONABLE_TELEGRAM_HISTORY")
        if alert.intent == SignalIntent.ENTRY:
            if row["reply_to_message_id"] is not None:
                return self._result(row, alert, "REVIEW_REQUIRED", "ENTRY_IS_REPLY")
            as_of = date.fromisoformat(row["telegram_sent_at_utc"][:10])
            contract, failure = resolve_entry(alert, self.catalog, as_of=as_of)
            if contract is None:
                return self._result(row, alert, "REVIEW_REQUIRED", failure)
            sid = _signal_id(self.channel_id, mid)
            con.execute(
                "INSERT INTO option_signals(signal_id,source_event_id,underlying,"
                "option_right,strike,expiration_date,original_limit,"
                "created_at_utc,state) "
                "SELECT ?,r.raw_event_id,?,?,?,?,?,?, 'SEEN' "
                "FROM telegram_message_revisions r WHERE r.revision_id=?",
                (
                    sid, contract.underlying, contract.right.value,
                    str(contract.strike), contract.expiration.isoformat(),
                    str(alert.premium), row["telegram_sent_at_utc"], rid,
                ),
            )
            con.execute(
                "INSERT INTO signal_metadata(signal_id,channel_id,source_message_id,"
                "category,first_revision_id) VALUES(?,?,?,?,?)",
                (sid, self.channel_id, mid, alert.category or "STANDARD", rid),
            )
            self._link(con, mid, sid, rid)
            return self._result(row, alert, "RECORDED", "ENTRY_VALIDATED_NOT_EXECUTED", sid)
        sid, failure = self._find_signal(con, row, alert)
        if failure:
            return self._result(row, alert, "REVIEW_REQUIRED", failure)
        assert sid is not None
        self._link(con, mid, sid, rid)
        return self._apply_followup(con, row, alert, sid)

    def _link(self, con: sqlite3.Connection, mid: int, sid: str, rid: int) -> None:
        con.execute(
            "INSERT INTO signal_message_links(channel_id,message_id,signal_id,source_revision_id) "
            "VALUES(?,?,?,?) ON CONFLICT(channel_id,message_id) DO NOTHING",
            (self.channel_id, mid, sid, rid),
        )
        existing = con.execute(
            "SELECT signal_id FROM signal_message_links WHERE channel_id=? AND message_id=?",
            (self.channel_id, mid),
        ).fetchone()
        if existing is None or existing[0] != sid:
            raise RuntimeError("Conflicting message lineage")

    def _find_signal(
        self, con: sqlite3.Connection, row: sqlite3.Row, alert: ParsedAlert,
    ) -> tuple[str | None, str | None]:
        mid = int(row["message_id"])
        reply = row["reply_to_message_id"]
        parent = None
        if reply is not None:
            parent = con.execute(
                "SELECT signal_id FROM signal_message_links WHERE channel_id=? AND message_id=?",
                (self.channel_id, int(reply)),
            ).fetchone()
            if parent is None:
                return None, "UNKNOWN_REPLY_LINEAGE"
        own = con.execute(
            "SELECT signal_id FROM signal_message_links WHERE channel_id=? AND message_id=?",
            (self.channel_id, mid),
        ).fetchone()
        if own is not None and parent is not None and own[0] != parent[0]:
            return None, "CONFLICTING_REPLY_LINEAGE"
        direct = str(parent[0] if parent is not None else own[0]) if (parent or own) else None
        rows = con.execute(
            "SELECT s.signal_id,s.underlying,s.option_right,s.strike,s.expiration_date "
            "FROM option_signals s JOIN signal_metadata m ON m.signal_id=s.signal_id "
            "WHERE m.channel_id=? AND s.state NOT IN ('CLOSED','REJECTED')",
            (self.channel_id,),
        ).fetchall()
        candidates = []
        for signal in rows:
            if alert.underlying and alert.underlying != signal["underlying"]:
                continue
            if alert.right and alert.right.value != signal["option_right"]:
                continue
            if alert.strike is not None and alert.strike != Decimal(signal["strike"]):
                continue
            if alert.expiry is not None:
                expiration = date.fromisoformat(signal["expiration_date"])
                if not _match_date(alert.expiry, expiration):
                    continue
            candidates.append(str(signal["signal_id"]))
        if direct is not None:
            if direct not in candidates:
                return None, "REPLY_CONTRACT_MISMATCH"
            return direct, None
        if not alert.underlying:
            return None, "MISSING_CONTRACT_OR_LINEAGE"
        if len(candidates) != 1:
            return None, "NO_MATCHING_SIGNAL" if not candidates else "AMBIGUOUS_MATCH"
        return candidates[0], None

    def _handle_edit(
        self, con: sqlite3.Connection, row: sqlite3.Row, alert: ParsedAlert,
    ) -> Decision:
        mid = int(row["message_id"])
        existing = con.execute(
            "SELECT p.intent,p.signal_id FROM signal_parser_decisions p "
            "JOIN telegram_message_revisions r ON r.revision_id=p.revision_id "
            "WHERE p.channel_id=? AND p.message_id=? AND r.event_kind='new' "
            "ORDER BY p.revision_id LIMIT 1",
            (self.channel_id, mid),
        ).fetchone()
        if existing is None or existing["signal_id"] is None:
            return self._result(row, alert, "REVIEW_REQUIRED", "EDIT_WITHOUT_LINKED_ORIGINAL")
        sid = str(existing["signal_id"])
        original_intent = str(existing["intent"])
        if original_intent != alert.intent.value:
            return self._result(row, alert, "REVIEW_REQUIRED", "EDIT_CHANGED_INTENT", sid)
        if alert.intent == SignalIntent.ENTRY:
            old = con.execute(
                "SELECT underlying,option_right,strike,expiration_date,original_limit "
                "FROM option_signals WHERE signal_id=?", (sid,),
            ).fetchone()
            if old is None or not alert.has_contract or alert.premium is None:
                return self._result(row, alert, "REVIEW_REQUIRED", "ENTRY_EDIT_REVIEW", sid)
            same = (
                old["underlying"] == alert.underlying
                and old["option_right"] == alert.right.value
                and Decimal(old["strike"]) == alert.strike
                and (alert.expiry is not None and _match_date(
                    alert.expiry, date.fromisoformat(old["expiration_date"])
                ))
                and Decimal(old["original_limit"]) == alert.premium
            )
            if not same:
                return self._result(row, alert, "REVIEW_REQUIRED", "ENTRY_EDIT_REVIEW", sid)
            return self._result(row, alert, "IGNORED", "UNCHANGED_ENTRY_EDIT", sid)
        if alert.intent not in {SignalIntent.METADATA_ONLY, SignalIntent.CLOSE_100_PERCENT}:
            return self._result(row, alert, "REVIEW_REQUIRED", "UNSAFE_EDIT", sid)
        found, failure = self._find_signal(con, row, alert)
        if failure or found != sid:
            return self._result(
                row, alert, "REVIEW_REQUIRED", failure or "EDIT_LINEAGE_MISMATCH", sid
            )
        # Edits of existing exits are kept for audit only. Only revised averages
        # can affect metadata. Edits can never create a new close request.
        if alert.intent == SignalIntent.CLOSE_100_PERCENT:
            return self._result(row, alert, "IGNORED", "EXIT_EDIT_NO_NEW_ACTION", sid)
        if alert.swift_average is not None:
            return self._apply_average(con, row, alert, sid)
        return self._result(row, alert, "IGNORED", "METADATA_EDIT_NO_AVERAGE", sid)

    def _apply_average(
        self, con: sqlite3.Connection, row: sqlite3.Row, alert: ParsedAlert, sid: str,
    ) -> Decision:
        assert alert.swift_average is not None
        at = row["telegram_edited_at_utc"] or row["telegram_sent_at_utc"]
        previous = con.execute(
            "SELECT effective_at_utc FROM signal_average_history WHERE signal_id=? "
            "ORDER BY effective_at_utc DESC LIMIT 1", (sid,),
        ).fetchone()
        if previous is not None and at <= previous[0]:
            return self._result(row, alert, "SKIPPED", "STALE_AVERAGE", sid)
        con.execute(
            "INSERT INTO signal_average_history(revision_id,signal_id,average,effective_at_utc) "
            "VALUES(?,?,?,?)", (int(row["revision_id"]), sid, str(alert.swift_average), at),
        )
        con.execute(
            "UPDATE option_signals SET swift_reported_avg=? WHERE signal_id=?",
            (str(alert.swift_average), sid),
        )
        return self._result(row, alert, "RECORDED", "SWIFT_AVERAGE_METADATA_ONLY", sid)

    def _apply_followup(
        self, con: sqlite3.Connection, row: sqlite3.Row, alert: ParsedAlert, sid: str,
    ) -> Decision:
        if alert.intent == SignalIntent.METADATA_ONLY:
            if alert.swift_average is not None:
                return self._apply_average(con, row, alert, sid)
            return self._result(row, alert, "RECORDED", "NO_ADD_METADATA_ONLY", sid)
        if alert.intent != SignalIntent.CLOSE_100_PERCENT:
            return self._result(row, alert, "REVIEW_REQUIRED", "UNEXPECTED_FOLLOWUP", sid)
        con.execute(
            "INSERT OR IGNORE INTO signal_close_requests("
            "signal_id,first_revision_id,requested_at_utc) "
            "VALUES(?,?,?)",
            (sid, int(row["revision_id"]), row["telegram_sent_at_utc"]),
        )
        changed = con.execute("SELECT changes()").fetchone()[0] > 0
        if changed:
            con.execute(
                "UPDATE option_signals SET state='CLOSE_REQUESTED' WHERE signal_id=?",
                (sid,),
            )
        return self._result(
            row, alert, "RECORDED" if changed else "IGNORED",
            "CLOSE_100_PERCENT_REQUEST_ONLY" if changed else "DUPLICATE_CLOSE_REQUEST",
            sid,
        )

    def statistics(self) -> dict[str, int]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT status,COUNT(*) FROM signal_parser_decisions WHERE channel_id=? "
                "GROUP BY status", (self.channel_id,),
            ).fetchall()
            pending = con.execute(
                "SELECT COUNT(*) FROM telegram_message_revisions r "
                "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                "WHERE r.channel_id=? AND p.revision_id IS NULL", (self.channel_id,),
            ).fetchone()[0]
        counts = {str(row[0]): int(row[1]) for row in rows}
        counts["PENDING"] = int(pending)
        return counts
