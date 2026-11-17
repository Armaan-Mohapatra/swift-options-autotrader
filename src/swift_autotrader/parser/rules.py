"""Deterministic syntax parsing; never authorize trades or infer an option year."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from swift_autotrader.parser import patterns
from swift_autotrader.signals.identity import OptionRight


class SignalIntent(StrEnum):
    ENTRY = "ENTRY"
    METADATA_ONLY = "METADATA_ONLY"
    CLOSE_100_PERCENT = "CLOSE_100_PERCENT"
    IGNORE_NON_OPTION = "IGNORE_NON_OPTION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


ACCEPTED_OPTION_LABELS = frozenset({"LOTTO"})
NO_ADD_MARKERS = frozenset({"ADDING MORE", "AVERAGING DOWN", "SCALE IN", "ADD"})
NEW_AVERAGE_MARKERS = frozenset({"NEW AVG", "NEW AVERAGE", "REVISED AVG"})
FULL_CLOSE_MARKERS = frozenset(
    {"SOLD HALF", "SOLD 1/3", "TRIM", "SCALED OUT", "RUNNERS OUT", "ALL OUT"}
)


# First words of exit/add/average phrases and ordinary filler. A follow-up that
# starts with one of these names no ticker; treating it as one made a reply such
# as "SCALED OUT" fail its lineage check and left the position open.
NON_TICKER_LEADING_WORDS = frozenset({
    "SOLD", "ALL", "RUNNERS", "NEW", "ADD", "ADDING", "ADDED", "TRIM", "TRIMMED",
    "SCALED", "SCALE", "CLOSED", "FULL", "OUT", "REVISED", "AVG", "AVERAGING",
    "DOUBLE", "BUY", "NO", "THE", "WE", "JUST", "I", "A", "AND", "NOW", "OK",
    "FINALLY", "NICE", "GREAT", "DONE", "TOOK", "TAKING", "STILL", "HALF",
})


@dataclass(frozen=True, slots=True)
class ExpirationHint:
    month: int
    day: int
    year: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12 or not 1 <= self.day <= 31:
            raise ValueError("invalid expiry date")
        if self.year is not None:
            date(self.year, self.month, self.day)


@dataclass(frozen=True, slots=True)
class ParsedAlert:
    intent: SignalIntent
    underlying: str | None = None
    right: OptionRight | None = None
    strike: Decimal | None = None
    expiry: ExpirationHint | None = None
    premium: Decimal | None = None
    swift_average: Decimal | None = None
    category: str | None = None
    reason: str | None = None

    @property
    def has_contract(self) -> bool:
        return all((self.underlying, self.right, self.strike, self.expiry))


def _expiry(raw: str) -> ExpirationHint:
    if "-" in raw:
        y, m, d = map(int, raw.split("-"))
        return ExpirationHint(m, d, y)
    pieces = [int(item) for item in raw.split("/")]
    year = pieces[2] if len(pieces) == 3 else None
    if year is not None and year < 100:
        year += 2000  # Two-digit years are explicit 20xx dates; still verify instrument.
    return ExpirationHint(pieces[0], pieces[1], year)


def _decimal(raw: str) -> Decimal:
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("nonpositive decimal")
    return result


def _right(raw: str) -> OptionRight:
    return OptionRight.CALL if raw.upper() in {"C", "CALL"} else OptionRight.PUT


def _symbol_prefix(text: str) -> str | None:
    # Extract a ticker only from a strict first-token position. Never scan the
    # whole paragraph for a ticker to manufacture context.
    tokens = text.upper().split()
    if tokens and tokens[0] in {"BUY", "BTO", "OPEN", "LOTTO"}:
        tokens.pop(0)
    if tokens and tokens[0].startswith("$"):
        tokens[0] = tokens[0][1:]
    if tokens and re.fullmatch(patterns.SYMBOL, tokens[0]):
        if tokens[0] in NON_TICKER_LEADING_WORDS:
            return None
        return tokens[0]
    return None


def parse_message(text: str) -> ParsedAlert:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    # Emoji and other non-ASCII decoration carry no trade meaning. Replace each
    # run with a space (never join the characters around it) so a decorated
    # alert parses identically to its plain text and anything else stays unmatched.
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\x00-\x7F]+", " ", text).strip())
    if not normalized:
        return ParsedAlert(SignalIntent.IGNORE_NON_OPTION, reason="EMPTY")
    if len(normalized) > 1000:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="TOO_LONG")
    if patterns.NON_OPTION.search(normalized):
        return ParsedAlert(SignalIntent.IGNORE_NON_OPTION, reason="NON_OPTION_ASSET")
    uppercase = normalized.upper()
    # Multiple contract clauses, mixed intentions or corrections are never
    # collapsed into a single synthetic opening order.
    contracts = list(patterns.CONTRACT_REFERENCE.finditer(uppercase))
    if len(contracts) > 1:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="MULTIPLE_CONTRACTS")
    if patterns.CORRECTION.search(uppercase):
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="CORRECTION_REVIEW")
    close = bool(patterns.EXIT.search(uppercase))
    add = bool(patterns.NO_ADD.search(uppercase))
    avg_marker = bool(patterns.AVG_MARKER.search(uppercase))
    avg_match = patterns.AVG.search(uppercase)
    if close and add:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="CONFLICTING_ACTIONS")
    if close and avg_marker:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="CONFLICTING_ACTIONS")
    if avg_marker and avg_match is None:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="MALFORMED_AVERAGE")
    swift_avg = None
    try:
        if avg_match:
            swift_avg = _decimal(avg_match.group("average"))
    except ValueError:
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="INVALID_AVERAGE")
    if close or add or avg_marker:
        intent = SignalIntent.CLOSE_100_PERCENT if close else SignalIntent.METADATA_ONLY
        match = patterns.EXPLICIT_CONTRACT.search(uppercase)
        symbol = match.group("symbol").upper() if match else _symbol_prefix(uppercase)
        if symbol is not None and symbol in NON_TICKER_LEADING_WORDS:
            symbol = None
        try:
            return ParsedAlert(
                intent,
                underlying=symbol,
                right=_right(match.group("right")) if match else None,
                strike=_decimal(match.group("strike")) if match else None,
                expiry=_expiry(match.group("expiry")) if match else None,
                swift_average=swift_avg,
            )
        except (ValueError, OverflowError):
            return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="INVALID_CONTRACT_REFERENCE")
    matched = patterns.ENTRY.fullmatch(normalized)
    if matched:
        try:
            expiry = _expiry(matched.group("expiry"))
            strike = _decimal(matched.group("strike"))
            premium = _decimal(matched.group("premium"))
        except (ValueError, OverflowError):
            return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="INVALID_ENTRY_VALUES")
        category = (
            matched.group("category") or matched.group("lead_category") or "STANDARD"
        ).upper()
        return ParsedAlert(
            SignalIntent.ENTRY, matched.group("symbol").upper(),
            _right(matched.group("right")), strike, expiry, premium,
            category=category,
        )
    if patterns.ENTRY_LIKE.search(uppercase):
        return ParsedAlert(SignalIntent.REVIEW_REQUIRED, reason="UNRECOGNIZED_OPTION_SYNTAX")
    return ParsedAlert(SignalIntent.IGNORE_NON_OPTION, reason="COMMENTARY")
