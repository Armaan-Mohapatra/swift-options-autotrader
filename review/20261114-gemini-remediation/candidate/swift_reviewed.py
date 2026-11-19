"""Portfolio-level per-order-approved execution companion for Swift AutoTrader.

Replaces the single-position restriction with multi-position portfolio management
while preserving exclusive pinned Robinhood MCP account binding, 1-contract per
entry alert, $999 max trade premium, $9,999 max combined exposure, $655 daily
gross realized loss stop, 60-minute unfilled entry cancellation, midpoint range
pricing, versioned journal migration, and mandatory human order approval.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from broker_evidence import (
    EvidenceError,
    TERMINAL,
    WORKING,
    count,
    decode_order,
    instant,
    money,
)
from alert_recovery import FollowupRecovery
from journal_migration import JournalMigrationError, migrate_journal_file
from telegram_notifier import TelegramNotifier, redact_sensitive
from unattended_policy import (
    ConfiguredApprovalProbe,
    Mandate,
    MandateError,
    UnattendedAuthorizer,
    capture_tool_catalog,
)

from swift_autotrader.broker.mcp_local_auth import (
    WindowsDPAPITokenStorage,
    load_pinned_agentic_account,
)
from swift_autotrader.broker.mcp_paper_data import ExchangeCalendarsSession
from swift_autotrader.broker.mcp_transport import (
    OAuthEnrollment,
    authenticated_mcp_client,
)
from swift_autotrader.broker.robinhood_mcp import (
    MCPContractError,
    RobinhoodMCPBroker,
    _data,
    _next_cursor,
    _obj,
    _rows,
    extract_normalized_review_fee,
    normalize_review_payload,
)
from swift_autotrader.config import Settings
from swift_autotrader.launch_preview import _cursor, _no_new_auth, preview_event, scan
from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.windows_runtime import single_instance, validate_install_root

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MAX_PREMIUM, MAX_EXPOSURE, DAILY_STOP = map(Decimal, ("999", "9999", "655"))

_NEW_AVG_RE = re.compile(r"\bNEW\s+AVG(?:ERAGE)?\b[:\s]*\$?([0-9]+(?:\.[0-9]+)?)", re.I)


class Blocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    premium: Decimal = MAX_PREMIUM
    exposure: Decimal = MAX_EXPOSURE
    daily_stop: Decimal = DAILY_STOP
    max_concurrent_positions: int | None = None

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.premium, MAX_PREMIUM),
            (self.exposure, MAX_EXPOSURE),
            (self.daily_stop, DAILY_STOP),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not 0 < value <= ceiling
            ):
                raise Blocked("invalid configured risk ceiling")
        if self.max_concurrent_positions is not None and (
            type(self.max_concurrent_positions) is not int
            or self.max_concurrent_positions < 1
        ):
            raise Blocked("invalid max_concurrent_positions")

    @classmethod
    def from_file(cls, path: Path) -> RiskLimits:
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise Blocked("reviewed configuration missing or unsafe")
        try:
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            if (
                d["mode"] != "PAPER_ONLY"
                or d["live_enabled"] is not False
                or d["paper_dispatch_enabled"] is not False
                or d["telegram_enabled"] is not True
                or type(d["paper_max_contracts"]) is not int
                or d["paper_max_contracts"] != 1
            ):
                raise Blocked("unexpected reviewed configuration")

            def value(key: str) -> Decimal:
                v = d[key]
                if type(v) not in (int, str):
                    raise Blocked("risk ceiling must be a decimal string or integer")
                return Decimal(str(v))

            max_pos = d.get("max_concurrent_positions")
            return cls(
                value("paper_max_premium_usd"),
                value("paper_max_exposure_usd"),
                value("paper_daily_gross_loss_stop_usd"),
                max_pos,
            )
        except Blocked:
            raise
        except Exception as exc:
            raise Blocked("invalid reviewed configuration") from exc


@dataclass(frozen=True)
class PortfolioMetrics:
    working_entry_exposure_usd: Decimal
    open_position_exposure_usd: Decimal
    reserved_fees_usd: Decimal
    total_exposure_usd: Decimal
    daily_gross_realized_loss_usd: Decimal
    daily_realized_gross_pnl_usd: Decimal
    daily_realized_net_pnl_usd: Decimal | None
    unrealized_gross_pnl_usd: Decimal | None
    total_fees_usd: Decimal | None
    active_position_count: int
    working_entry_count: int


def assert_not_stopped(path: Path | None) -> None:
    if path is not None and os.path.lexists(path):
        raise Blocked("emergency stop is active; no new brokerage requests")


def now() -> datetime:
    return datetime.now(UTC)


def say(event: str, **fields: object) -> None:
    print(
        json.dumps({"status": event, **fields}, default=str, ensure_ascii=True),
        flush=True,
    )


def atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="swift-reviewed-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def health_record(
    path: Path,
    journal: Journal,
    state: str,
    stop_path: Path | None,
    *,
    metrics: PortfolioMetrics | None = None,
    notifier: TelegramNotifier | None = None,
    unattended_mode: bool = False,
) -> None:
    """Write requested monitor mode, not proof of broker authorization."""
    payload: dict[str, object] = {
        "version": 1,
        "status": state,
        "updated_at_utc": now().isoformat(),
        "per_order_approval": not unattended_mode,
        "unattended": unattended_mode,
        "emergency_stop": bool(stop_path and os.path.lexists(stop_path)),
        "unknown_outcome": any(a["status"] == "UNKNOWN" for a in journal.doc["attempts"]),
    }
    if metrics is not None:
        payload["portfolio"] = {
            "active_positions": metrics.active_position_count,
            "working_entries": metrics.working_entry_count,
            "total_exposure_usd": str(metrics.total_exposure_usd),
            "daily_gross_loss_usd": str(metrics.daily_gross_realized_loss_usd),
            "daily_realized_net_pnl_usd": str(metrics.daily_realized_net_pnl_usd) if metrics.daily_realized_net_pnl_usd is not None else None,
            "unrealized_gross_pnl_usd": str(metrics.unrealized_gross_pnl_usd) if metrics.unrealized_gross_pnl_usd is not None else None,
            "total_fees_usd": str(metrics.total_fees_usd) if metrics.total_fees_usd is not None else None,
        }
    if notifier is not None:
        payload["telegram_notifications_configured"] = notifier.configured
        payload["telegram_last_error"] = notifier.last_error
    atomic_json(path, payload)


class Journal:
    """Durable, versioned transaction journal with non-destructive v1->v2 migration."""

    def __init__(self, path: Path, pin: str, source_channel: int | None = None) -> None:
        self.path = Path(path)
        self.pin = pin
        self.source_channel = source_channel
        fingerprint = hashlib.sha256(pin.encode()).hexdigest()
        if self.path.is_symlink():
            raise Blocked("journal symlink")
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise Blocked("corrupt journal") from exc
            if (
                raw.get("version") != 1
                or raw.get("pin") != fingerprint
                or raw.get("source_channel") != source_channel
            ):
                raise Blocked("journal account/version mismatch")
            try:
                self.doc = migrate_journal_file(
                    self.path, pin=pin, source_channel=source_channel
                )
            except JournalMigrationError as exc:
                raise Blocked(str(exc)) from exc
        else:
            self.doc = {
                "version": 1,
                "schema_version": 2,
                "pin": fingerprint,
                "source_channel": source_channel,
                "trades": [],
                "attempts": [],
                "notifications_sent": [],
            }
            self.save()

    def save(self) -> None:
        atomic_json(self.path, self.doc)

    def assert_known(self) -> None:
        if any(a["status"] == "UNKNOWN" for a in self.doc["attempts"]):
            raise Blocked(
                "UNKNOWN BROKER OUTCOME: stop and inspect account; never retry or delete journal"
            )

    def attempt(
        self, trade: Mapping[str, object], kind: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        self.assert_known()
        attempt_seq = int(trade.get("exit_attempt_seq", 0)) if kind == "SELL" else 0
        for a in self.doc["attempts"]:
            if a["trade"] == trade["source"] and a["kind"] == kind:
                if kind != "SELL" or int(a.get("seq", 0)) == attempt_seq:
                    raise Blocked("duplicate logical action")
        item: dict[str, object] = {
            "trade": trade["source"],
            "kind": kind,
            "seq": attempt_seq,
            "ref": payload.get("ref_id"),
            "payload_digest": digest(payload),
            "created": now().isoformat(),
            "status": "UNKNOWN",
            "order_id": None,
        }
        self.doc["attempts"].append(item)
        self.save()  # Persisted BEFORE any network write
        return item


class ReadBroker(RobinhoodMCPBroker):
    async def discover(self):
        result = await super().discover()
        assert self._discovered is not None
        for item in self._discovered.values():
            camel, snake = item.get("inputSchema"), item.get("input_schema")
            if camel is not None and snake is not None and camel != snake:
                raise Blocked("conflicting tool schema aliases")
            if camel is None and isinstance(snake, Mapping):
                item["inputSchema"] = snake
        if result.status != "READ_ONLY_SCHEMA_OK":
            raise Blocked("incompatible read-tool schema")
        return result

    async def all_rows(self, name: str, field: str, pin: str, **filters: object) -> list[dict]:
        result, seen, cursor = [], set(), None
        for _ in range(10):
            args = {"account_number": pin, **filters}
            if cursor:
                args["cursor"] = cursor
            page = await self._call(name, args)
            result.extend(_rows(page, field))
            cursor = _next_cursor(page)
            if not cursor:
                return result
            if cursor in seen:
                raise Blocked("repeated account pagination cursor")
            seen.add(cursor)
        raise Blocked("incomplete account pagination")


def contract_dict(contract: OptionContract) -> dict[str, str]:
    return {
        "symbol": contract.underlying,
        "right": contract.right.value,
        "strike": str(contract.strike),
        "expiration": contract.expiration.isoformat(),
    }


def from_dict(d: Mapping[str, str]) -> OptionContract:
    return OptionContract(
        d["symbol"],
        OptionRight(d["right"]),
        Decimal(d["strike"]),
        date.fromisoformat(d["expiration"]),
    )


def resolve_entry_limit_price(ticket: Mapping[str, object], source_text: str | None = None) -> Decimal:
    """Use only the validated parser ticket; never rescan commentary for prices."""
    price = money(ticket["limit_price"])
    if price <= 0:
        raise Blocked("nonpositive entry price")
    if "price_range_low" in ticket or "price_range_high" in ticket:
        low, high = money(ticket.get("price_range_low")), money(ticket.get("price_range_high"))
        if low <= 0 or low > high or (low + high) / 2 != price:
            raise Blocked("entry range does not reconcile to parser price")
    return price


def contract_for_ticket(
    ticket: Mapping[str, object], chains: list[dict], at: datetime
) -> OptionContract:
    symbol = str(ticket["underlying"])
    dates = []
    for row in chains:
        if (row.get("underlying_symbol") or row.get("symbol")) != symbol:
            continue
        raw = row.get("expiration_dates")
        if not isinstance(raw, list):
            raise Blocked("invalid expiration list")
        for value in raw:
            d = date.fromisoformat(value)
            if (
                d >= at.astimezone(NY).date()
                and (d.month, d.day) == (ticket["expiration_month"], ticket["expiration_day"])
                and (ticket["expiration_year"] is None or d.year == ticket["expiration_year"])
            ):
                dates.append(d)
    unique_dates = set(dates)
    if len(unique_dates) != 1:
        raise Blocked("exact expiration unavailable or ambiguous")
    return OptionContract(
        symbol, OptionRight(str(ticket["option_right"])), money(ticket["strike"]), unique_dates.pop()
    )


def chain_type(row: Mapping[str, object]) -> str:
    explicit = row.get("underlying_type")
    if explicit in {"equity", "index"}:
        return str(explicit)
    if explicit is not None:
        raise Blocked("unknown asset class")
    refs = row.get("underlying_instruments")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
        raise Blocked("underlying asset type not verified")
    url = refs[0].get("instrument")
    if not isinstance(url, str):
        raise Blocked("missing equity instrument reference")
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "instruments":
        raise Blocked("unrecognized underlying instrument resource")
    try:
        UUID(parts[1])
    except ValueError as exc:
        raise Blocked("invalid equity instrument reference") from exc
    return "equity"


def order_payload(pin: str, option_id: str, side: str, price: Decimal, ref: str) -> dict:
    return {
        "account_number": pin,
        "legs": [
            {
                "option_id": option_id,
                "side": side,
                "position_effect": "open" if side == "buy" else "close",
                "ratio_quantity": 1,
            }
        ],
        "quantity": "1",
        "price": str(price),
        "type": "limit",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": ref,
    }


def sanitized(x: object, pin: str) -> object:
    return json.loads(
        json.dumps(x, default=str, ensure_ascii=True).replace(pin, "[PINNED MCP]")
    )


def digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]


def confirm_console(request: Mapping[str, object], details: Mapping[str, object], pin: str, *, seconds: int = 20) -> bool:
    import msvcrt

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Blocked("interactive console required")
    code = digest(request)
    while msvcrt.kbhit():
        msvcrt.getwch()
    say(
        "REAL_ORDER_APPROVAL_REQUIRED",
        order=sanitized(request, pin),
        details=sanitized(details, pin),
        confirm="APPROVE " + code,
        expires_in_seconds=seconds,
    )
    print("Type exact approval, or press Enter to skip: ", end="", flush=True)
    end = time.monotonic() + seconds
    typed = ""
    while time.monotonic() < end:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getwch()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            print(flush=True)
            return typed == "APPROVE " + code
        if ch == "\b":
            if typed:
                typed = typed[:-1]
                print("\b \b", end="", flush=True)
        elif ch.isascii() and ch.isprintable() and len(typed) < 64:
            typed += ch
            print(ch, end="", flush=True)
    print("\nApproval expired.", flush=True)
    while msvcrt.kbhit():
        msvcrt.getwch()
    return False


def evidence_record(ev) -> dict[str, object]:
    return {
        "id": ev.broker_id,
        "state": ev.state,
        "filled": ev.processed_quantity,
        "created": ev.created_at.isoformat(),
        "updated": ev.updated_at.isoformat(),
        "fills": [
            {
                "id": f.id,
                "quantity": f.quantity,
                "price": str(f.price),
                "at": f.timestamp.isoformat(),
            }
            for f in ev.executions
        ],
    }


def observe_evidence(trade: dict[str, object], prefix: str, ev) -> bool:
    key = prefix + "_evidence"
    record = evidence_record(ev)
    old = trade.get(key)
    if isinstance(old, Mapping):
        if (
            record["id"] != old["id"]
            or record["created"] != old["created"]
            or instant(record["updated"]) < instant(old["updated"])
            or int(record["filled"]) < int(old["filled"])
            or (old["state"] in TERMINAL and record["state"] != old["state"])
            or (
                old["state"] == "pending_cancelled"
                and record["state"] not in TERMINAL | {"pending_cancelled"}
            )
            or any(f not in record["fills"] for f in old["fills"])
        ):
            raise Blocked("broker evidence regressed or changed; halt")
    trade[key] = record
    return old != record


def checked_review(review: Mapping[str, object]) -> Mapping[str, object]:
    """Validate review_option_order response across observed Nov 2026 and legacy shapes."""
    if not isinstance(review, Mapping):
        raise Blocked("review response shape not verified; no order submitted")
    checks = review.get("order_checks")
    if isinstance(checks, Mapping):
        if checks:
            raise Blocked("review response shape not verified; no order submitted")
        checks_list: list[object] = []
    elif isinstance(checks, list):
        checks_list = checks
    else:
        raise Blocked("review response shape not verified; no order submitted")

    keys = [k for k in ("customer_approval_required", "approval_required") if k in review]
    if not keys or any(type(review.get(k)) is not bool for k in keys):
        raise Blocked("review response shape not verified; no order submitted")
    if len(keys) == 2 and review["customer_approval_required"] != review["approval_required"]:
        raise Blocked("review response shape not verified; no order submitted")

    for row in checks_list:
        if not isinstance(row, Mapping) or row.get("severity") not in {"pass", "ok", "info"}:
            raise Blocked("broker review contains warning/error or unknown check")
    return review


def review_requires_customer_approval(review: Mapping[str, object]) -> bool:
    checked_review(review)
    if "customer_approval_required" in review:
        return bool(review["customer_approval_required"])
    return bool(review["approval_required"])


def extract_review_fee(review: Mapping[str, object]) -> Decimal:
    """Extract verified review fee from fees.total_fee, fees.estimated, or estimated_fees."""
    try:
        fee = extract_normalized_review_fee(review)
    except (MCPContractError, EvidenceError) as exc:
        raise Blocked("broker review fee estimate unavailable") from exc
    if fee is None:
        raise Blocked("broker review fee estimate unavailable")
    return fee


class Companion:
    """Multi-position portfolio companion with per-order human or mandate approval."""

    def __init__(
        self,
        client,
        pin: str,
        journal: Journal,
        approve,
        calendar=None,
        *,
        limits: RiskLimits | None = None,
        stop_path: Path | None = None,
        notifier: TelegramNotifier | None = None,
        allow_external_positions: bool = False,
        unattended_mode: bool = False,
    ) -> None:
        self.client = client
        self.pin = pin
        self.journal = journal
        self.approve = approve
        self.read = ReadBroker(client, approved_account_number=pin)
        self.calendar = calendar or ExchangeCalendarsSession()
        self.limits = limits or RiskLimits()
        self.stop_path = stop_path
        self.notifier = notifier or TelegramNotifier(
            pin=pin, sent_fingerprints=self.journal.doc.get("notifications_sent", [])
        )
        if allow_external_positions:
            raise Blocked("external exposure cannot be safely attributed")
        self.allow_external_positions = False
        self.unattended_mode = unattended_mode
        self.last_metrics: PortfolioMetrics | None = None

    def _emit_notification(self, event_code: str, *, dedupe_key: str | None = None, **fields: object) -> None:
        receipt = self.notifier.notify(event_code, dedupe_key=dedupe_key, **fields)
        self.journal.doc["notifications_sent"] = list(self.notifier.sent_fingerprints)
        if receipt.delivered:
            self.journal.save()

    async def identity(self):
        await self.read.discover()
        account = await self.read.external_account()
        if account.account_number != self.pin or not account.eligible:
            raise Blocked("pinned MCP account changed")
        return account

    async def snapshot(self):
        self.journal.assert_known()
        account = await self.identity()
        orders = await self.read.all_rows("get_option_orders", "orders", self.pin)
        positions = await self.read.all_rows(
            "get_option_positions", "positions", self.pin, nonzero=True
        )
        order_map: dict[str, dict] = {}
        for row in orders:
            oid = row.get("id")
            if not isinstance(oid, str) or not oid or oid in order_map:
                raise Blocked("invalid or duplicate brokerage order id")
            order_map[oid] = row
        return account, order_map, positions

    def reconcile(self, order_map: Mapping[str, dict], quotes_by_option: Mapping[str, Decimal] | None = None):
        """Reconcile all Swift trades across the portfolio independently."""
        active: list[tuple[dict, object, object]] = []
        losses = Decimal("0")
        daily_gross_pnl = Decimal("0")
        daily_net_pnl: Decimal | None = Decimal("0")
        unrealized_gross: Decimal | None = Decimal("0")
        working_exposure = Decimal("0")
        open_exposure = Decimal("0")
        reserved_fees = Decimal("0")
        total_fees: Decimal | None = Decimal("0")
        working_entries = 0
        open_positions = 0
        known: set[str] = set()
        changed = False
        day = now().astimezone(NY).date()

        for trade in self.journal.doc["trades"]:
            for prior in trade.get("prior_exits", []):
                poid = prior.get("exit_id")
                if poid:
                    if poid in known or poid not in order_map:
                        raise Blocked("missing or conflicting archived exit order; halt")
                    known.add(poid)
                    pev = decode_order(
                        order_map[poid],
                        expected_id=poid,
                        expected_option_id=trade["option"],
                        side="sell",
                        position_effect="close",
                        quantity=1,
                        limit_price=prior["exit_price"],
                        expected_ref=prior["exit_ref"],
                    )
                    if pev.processed_quantity != 0 or pev.state not in TERMINAL:
                        raise Blocked("archived exit order unexpectedly active or filled; halt")

            evidence: dict[str, object] = {}
            for side, field in [("buy", "entry_id"), ("sell", "exit_id")]:
                oid = trade.get(field)
                if oid is None:
                    continue
                if oid in known or oid not in order_map:
                    raise Blocked("missing or conflicting acknowledged order; halt")
                known.add(oid)
                ev = decode_order(
                    order_map[oid],
                    expected_id=oid,
                    expected_option_id=trade["option"],
                    side=side,
                    position_effect="open" if side == "buy" else "close",
                    quantity=1,
                    limit_price=trade["entry_price" if side == "buy" else "exit_price"],
                    expected_ref=trade["entry_ref" if side == "buy" else "exit_ref"],
                )
                was_changed = observe_evidence(
                    trade, "entry" if side == "buy" else "exit", ev
                )
                changed = was_changed or changed
                if was_changed and ev.processed_quantity == 1:
                    self._emit_notification(
                        "ORDER_FILLED" if side == "buy" else "POSITION_CLOSED",
                        dedupe_key=f"fill:{oid}",
                        contract=trade["contract"]["symbol"],
                        side=side.upper(),
                        price=str(ev.executions[0].price),
                    )
                elif was_changed and ev.state == "cancelled":
                    self._emit_notification(
                        "ORDER_CANCELLED",
                        dedupe_key=f"cancel:{oid}",
                        contract=trade["contract"]["symbol"],
                        side=side.upper(),
                    )
                evidence[side] = ev

            e = evidence.get("buy")
            x = evidence.get("sell")
            if not e:
                raise Blocked("missing entry acknowledgment")

            entry_est_fee = money(trade.get("estimated_entry_fee_usd", "0"))

            if x and (
                e.processed_quantity != 1
                or e.state not in TERMINAL
                or x.created_at < e.created_at
            ):
                raise Blocked("exit does not reconcile to owned entry")

            if x and x.processed_quantity:
                ep, xp = e.executions[0], x.executions[0]
                if xp.timestamp < ep.timestamp:
                    raise Blocked("exit before entry fill")
                total_fees = None
                if xp.timestamp.astimezone(NY).date() == day:
                    gross_delta = (xp.price - ep.price) * 100
                    daily_gross_pnl += gross_delta
                    daily_net_pnl = None
                    losses += max(Decimal("0"), (ep.price - xp.price) * 100)

            is_working_entry = e.state in WORKING and e.processed_quantity == 0
            is_open_lot = e.processed_quantity == 1 and (not x or x.processed_quantity == 0)
            is_working_exit = x is not None and x.state in WORKING

            if is_working_entry:
                working_entries += 1
                working_exposure += e.price * 100
                reserved_fees += entry_est_fee
            if is_open_lot:
                open_positions += 1
                ep = e.executions[0]
                open_exposure += ep.price * 100
                reserved_fees += entry_est_fee
                total_fees = None
                if unrealized_gross is not None:
                    if quotes_by_option is not None and trade["option"] in quotes_by_option:
                        mark_bid = money(quotes_by_option[trade["option"]])
                        if mark_bid <= 0:
                            unrealized_gross = None
                        else:
                            unrealized_gross += (mark_bid - ep.price) * 100
                    else:
                        unrealized_gross = None

            if is_working_entry or is_open_lot or is_working_exit:
                active.append((trade, e, x))

        total_exp = working_exposure + open_exposure + reserved_fees
        self.last_metrics = PortfolioMetrics(
            working_entry_exposure_usd=working_exposure,
            open_position_exposure_usd=open_exposure,
            reserved_fees_usd=reserved_fees,
            total_exposure_usd=total_exp,
            daily_gross_realized_loss_usd=losses,
            daily_realized_gross_pnl_usd=daily_gross_pnl,
            daily_realized_net_pnl_usd=daily_net_pnl,
            unrealized_gross_pnl_usd=unrealized_gross,
            total_fees_usd=total_fees,
            active_position_count=open_positions,
            working_entry_count=working_entries,
        )
        if changed:
            self.journal.save()
        return active, losses, known

    def entry_risk(
        self,
        order_map: Mapping[str, dict],
        positions: list[dict],
        price: Decimal,
        active: list[tuple[dict, object, object]],
        losses: Decimal,
        known: set[str],
        *,
        estimated_fee_usd: Decimal = Decimal("0"),
    ) -> None:
        """Portfolio-level entry risk check: supports multiple concurrent Swift positions."""
        trade_premium = price * 100
        if not 0 < trade_premium <= self.limits.premium:
            self._emit_notification(
                "RISK_LIMIT_VIOLATION",
                reason="MAX_TRADE_PREMIUM_EXCEEDED",
                requested_premium_usd=str(trade_premium),
                limit_usd=str(self.limits.premium),
            )
            raise Blocked("approved risk ceiling reached: per-trade premium")

        if losses >= self.limits.daily_stop:
            self._emit_notification(
                "RISK_LIMIT_VIOLATION",
                reason="DAILY_LOSS_STOP_REACHED",
                daily_loss_usd=str(losses),
                stop_usd=str(self.limits.daily_stop),
            )
            raise Blocked("approved risk ceiling reached: daily gross loss stop")

        if (
            self.limits.max_concurrent_positions is not None
            and len(active) >= self.limits.max_concurrent_positions
        ):
            raise Blocked("approved risk ceiling reached: max concurrent positions")

        current_exposure = Decimal("0")
        owned_open_counts: dict[str, int] = {}
        for trade, e, x in active:
            if trade.get("estimated_entry_fee_usd") is None:
                raise Blocked("existing entry fee estimate unavailable; cannot certify exposure")
            est_fee = money(trade["estimated_entry_fee_usd"])
            if e.state in WORKING and e.processed_quantity == 0:
                current_exposure += e.price * 100 + est_fee
            elif e.processed_quantity == 1 and (not x or x.processed_quantity == 0):
                current_exposure += e.executions[0].price * 100 + est_fee
                opt_id = str(trade["option"])
                owned_open_counts[opt_id] = owned_open_counts.get(opt_id, 0) + 1

        if current_exposure + trade_premium + estimated_fee_usd > self.limits.exposure:
            self._emit_notification(
                "RISK_LIMIT_VIOLATION",
                reason="MAX_PORTFOLIO_EXPOSURE_EXCEEDED",
                current_exposure_usd=str(current_exposure),
                new_trade_usd=str(trade_premium + estimated_fee_usd),
                ceiling_usd=str(self.limits.exposure),
            )
            raise Blocked("approved risk ceiling reached: aggregate portfolio exposure")

        broker_counts: dict[str, int] = {}
        for p in positions:
            qty = count(p.get("quantity"))
            if qty == 0:
                continue
            opt_id = p.get("option_id")
            if (not isinstance(opt_id, str) or not opt_id or opt_id in broker_counts
                    or p.get("type") != "long" or count(p.get("trade_value_multiplier")) != 100):
                raise Blocked("unreconciled existing account position; no new entry")
            broker_counts[opt_id] = qty
        if broker_counts != owned_open_counts:
            raise Blocked("unreconciled existing account position; no new entry")

        day = now().astimezone(NY).date()
        for oid, row in order_map.items():
            if oid in known:
                continue
            if row.get("state") not in TERMINAL:
                raise Blocked("unreconciled account order; no new entry")
            if (
                count(row.get("processed_quantity"))
                and instant(row.get("updated_at")).astimezone(NY).date() == day
            ):
                legs = row.get("legs")
                if (
                    not isinstance(legs, list)
                    or not legs
                    or any(
                        not isinstance(l, Mapping) or l.get("position_effect") != "open"
                        for l in legs
                    )
                ):
                    raise Blocked("external same-day closing fills; daily loss not established")

    def available_buying_power_after_reservations(
        self, raw_buying_power: Decimal, active: list[tuple[dict, object, object]]
    ) -> Decimal:
        """Subtract working Swift entry orders that the broker may not have deducted."""
        reserved = Decimal("0")
        for trade, e, _x in active:
            if e.state in WORKING and e.processed_quantity == 0:
                reserved += e.price * 100 + money(trade.get("estimated_entry_fee_usd", "0"))
        return raw_buying_power - reserved

    async def instrument(self, contract: OptionContract, price: Decimal):
        instrument = await self.read.resolve_instrument(contract)
        if instrument.multiplier != 100 or not instrument.tradable:
            raise Blocked("standard 100 multiplier required")
        try:
            tick = instrument.tick_for_price(price)
        except MCPContractError as exc:
            if "Ambiguous threshold tick" in str(exc):
                raise Blocked("ambiguous threshold tick; do not round") from exc
            raise Blocked("invalid tick size") from exc
        if tick is None:
            args: dict[str, object] = {
                "chain_id": instrument.chain_id,
                "expiration_dates": contract.expiration.isoformat(),
                "strike_price": str(contract.strike),
                "type": contract.right.value.lower(),
                "state": "active",
            }
            rows: list[dict] = []
            seen_cursors: set[str] = set()
            for _ in range(10):
                response = await self.read._call("get_option_instruments", args)
                rows.extend(_rows(response, "instruments", "results"))
                cursor = _next_cursor(response)
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise Blocked("tick lookup pagination incomplete")
                seen_cursors.add(cursor)
                args["cursor"] = cursor
            else:
                raise Blocked("tick lookup pagination incomplete")
            matches = [r for r in rows if r.get("id") == instrument.option_id]
            if len(matches) != 1 or not isinstance(matches[0].get("min_ticks"), Mapping):
                raise Blocked("tick size unavailable")
            t = matches[0]["min_ticks"]
            below, above, cutoff = (
                money(t.get(k)) for k in ("below_tick", "above_tick", "cutoff_price")
            )
            if below <= 0 or above <= 0:
                raise Blocked("invalid tick size")
            if price == cutoff and (price % below or price % above):
                raise Blocked("ambiguous threshold tick; do not round")
            tick = below if price < cutoff else above
        if tick <= 0 or price <= 0 or price % tick:
            raise Blocked("alert limit not on verified tick; do not reprice")
        return instrument

    def assert_market(self) -> None:
        if not self.calendar.is_regular_session_open(now()):
            raise Blocked("regular options session closed")

    async def review(self, payload: Mapping[str, object], contract: OptionContract, instrument, underlying_type: str):
        self.assert_market()
        quote = await self.read.quote(instrument, max_age_seconds=15)
        if quote.ask <= 0 or quote.bid <= 0:
            raise Blocked("nonpositive quote")
        args = {k: v for k, v in payload.items() if k != "ref_id"}
        if underlying_type not in {"equity", "index"}:
            raise Blocked("chain underlying type not verified")
        args.update(chain_symbol=contract.underlying, underlying_type=underlying_type)
        result = await self.read._call("review_option_order", args)
        say("BROKER_REVIEW", review=sanitized(result, self.pin))
        checked_review(result)
        est_fee = extract_review_fee(result)
        return {
            "contract": contract_dict(contract),
            "bid": str(quote.bid),
            "ask": str(quote.ask),
            "maximum_premium_usd": str(Decimal(str(payload["price"])) * 100),
            "estimated_fee_usd": str(est_fee),
            "customer_approval_required": review_requires_customer_approval(result),
            "broker_review": result,
        }

    async def write_after_approval(
        self, trade: dict[str, object], kind: str, payload: dict[str, object], details: dict[str, object], recheck
    ) -> bool:
        self.journal.assert_known()
        assert_not_stopped(self.stop_path)
        tool = "cancel_option_order" if kind == "CANCEL" else "place_option_order"
        if payload.get("account_number") != self.pin:
            raise Blocked("wrong account")
        if kind != "CANCEL" and self.unattended_mode:
            broker_rev = details.get("broker_review")
            if isinstance(broker_rev, Mapping) and review_requires_customer_approval(broker_rev):
                self._emit_notification(
                    "BROKER_APPROVAL_REQUIRED_BLOCKED",
                    dedupe_key=f"broker-approval-req:{trade['source']}:{kind}",
                    action=kind,
                    contract=trade.get("contract", {}).get("symbol", "UNKNOWN"),
                )
                raise Blocked("broker requires customer approval for this transaction; unattended submission blocked")
        schema = (self.read._discovered or {}).get(tool, {}).get("inputSchema")
        if not isinstance(schema, Mapping) or not set(payload) <= set(
            schema.get("properties", {})
        ):
            raise Blocked("write tool schema unavailable")
        import jsonschema

        jsonschema.validate(payload, schema)
        self._emit_notification(
            "ORDER_REVIEW_PENDING",
            dedupe_key=f"review:{trade['source']}:{kind}:{payload.get('ref_id', payload.get('order_id'))}",
            action=kind,
            contract=trade.get("contract", {}).get("symbol", "UNKNOWN"),
            price=str(payload.get("price", "N/A")),
        )
        deadline = time.monotonic() + 30
        approved = self.approve(payload, details, self.pin)
        if hasattr(approved, "__await__"):
            approved = await approved
        if approved is not True or time.monotonic() >= deadline:
            say("ORDER_NOT_APPROVED", action=kind, source=trade["source"])
            return False
        await recheck()
        if time.monotonic() >= deadline:
            raise Blocked("approval expired during recheck")
        assert_not_stopped(self.stop_path)
        item = self.journal.attempt(trade, kind, payload)
        try:
            response = await asyncio.wait_for(
                self.client.call_tool(tool, payload), timeout=15
            )
            data = _data(response)
            if kind == "CANCEL":
                if data.get("accepted") is not True:
                    raise Blocked("cancel not acknowledged")
                trade["cancel_ack"] = True
            else:
                order = _obj(data.get("order"))
                oid = order.get("id")
                if not isinstance(oid, str) or not oid:
                    raise Blocked("missing broker order id")
                expected_side = payload["legs"][0]["side"]
                ev = decode_order(
                    order,
                    expected_id=oid,
                    expected_option_id=str(trade["option"]),
                    side=expected_side,
                    position_effect=payload["legs"][0]["position_effect"],
                    quantity=1,
                    limit_price=payload["price"],
                    expected_ref=str(payload["ref_id"]),
                )
                if not -10 <= (ev.created_at - instant(item["created"])).total_seconds() <= 30:
                    raise Blocked("acknowledged order timestamp not contemporaneous")
                prefix = "entry" if kind == "BUY" else "exit"
                observe_evidence(trade, prefix, ev)
                trade[prefix + "_id"] = item["order_id"] = oid
                trade[prefix + "_price"] = payload["price"]
                trade[prefix + "_ref"] = payload["ref_id"]
                if kind == "BUY":
                    self.journal.doc["trades"].append(trade)
            item["status"] = "ACKNOWLEDGED"
            self.journal.save()
        except BaseException:
            item["status"] = "UNKNOWN"
            self.journal.save()
            say("UNKNOWN_OUTCOME_STOP", action=kind, source=trade["source"])
            self._emit_notification(
                "UNKNOWN_BROKER_OUTCOME",
                dedupe_key=f"unknown:{trade['source']}:{kind}",
                action=kind,
                contract=trade.get("contract", {}).get("symbol", "UNKNOWN"),
            )
            raise
        say("BROKER_ACKNOWLEDGED", action=kind, source=trade["source"], filled_not_assumed=True)
        self._emit_notification(
            "ORDER_ACKNOWLEDGED",
            dedupe_key=f"ack:{trade['source']}:{kind}:{item.get('order_id') or payload.get('order_id')}",
            action=kind,
            contract=trade.get("contract", {}).get("symbol", "UNKNOWN"),
            filled_assumed=False,
        )
        return True

    async def entry(self, ticket: Mapping[str, object], source_check) -> None:
        source = str(ticket["source_message_id"])
        if any(t["source"] == source for t in self.journal.doc["trades"]) or any(
            a["trade"] == source for a in self.journal.doc["attempts"]
        ):
            return
        source_text = source_check()
        self.assert_market()
        account, orders, positions = await self.snapshot()
        active, losses, known = self.reconcile(orders)
        price = resolve_entry_limit_price(ticket, source_text if isinstance(source_text, str) else None)
        self.entry_risk(orders, positions, price, active, losses, known)
        raw_bp = await self.read.buying_power(account)
        avail_bp = self.available_buying_power_after_reservations(raw_bp, active)
        if avail_bp < price * 100:
            self._emit_notification(
                "INSUFFICIENT_BUYING_POWER",
                dedupe_key=f"bp:{source}",
                available_usd=str(avail_bp),
                required_usd=str(price * 100),
            )
            raise Blocked("buying power below requested premium after working order reservations")
        chains = _rows(
            await self.read._call(
                "get_option_chains", {"underlying_symbol": ticket["underlying"]}
            ),
            "chains",
            "results",
        )
        contract = contract_for_ticket(ticket, chains, now())
        chain_types = {
            chain_type(r)
            for r in chains
            if (r.get("underlying_symbol") or r.get("symbol")) == contract.underlying
            and contract.expiration.isoformat() in (r.get("expiration_dates") or [])
        }
        if len(chain_types) != 1 or not chain_types <= {"equity", "index"}:
            raise Blocked("underlying type unavailable from chain; no fee/collateral assumption")
        underlying_type = chain_types.pop()
        instrument = await self.instrument(contract, price)
        payload = order_payload(self.pin, instrument.option_id, "buy", price, str(uuid4()))
        details = await self.review(payload, contract, instrument, underlying_type)
        est_fee = money(details.get("estimated_fee_usd", "0"))
        self.entry_risk(
            orders, positions, price, active, losses, known, estimated_fee_usd=est_fee
        )
        if avail_bp < price * 100 + est_fee:
            self._emit_notification(
                "INSUFFICIENT_BUYING_POWER",
                dedupe_key=f"bp-fee:{source}",
                available_usd=str(avail_bp),
                required_usd=str(price * 100 + est_fee),
            )
            raise Blocked("buying power below requested premium plus estimated fee")
        trade: dict[str, object] = {
            "source": source,
            "option": instrument.option_id,
            "contract": contract_dict(contract),
            "exit_requested": False,
            "underlying_type": underlying_type,
            "estimated_entry_fee_usd": str(est_fee),
            "estimated_exit_fee_usd": "0.00",
            "prior_exits": [],
            "exit_attempt_seq": 0,
            "swift_reported_avg": None,
            "is_lotto": bool("LOTTO" in str(source_text).upper()),
        }
        details.update(source_message_id=source, source_alert=source_text)
        self._emit_notification(
            "NEW_SWIFT_ALERT",
            dedupe_key=f"alert:{source}",
            source_message_id=source,
            contract=f"{contract.underlying} {contract.expiration.isoformat()} {contract.strike}{contract.right.value[0]}",
            limit_price=str(price),
        )

        async def recheck() -> None:
            source_check()
            self.assert_market()
            a, o, p = await self.snapshot()
            aa, ll, kk = self.reconcile(o)
            self.entry_risk(o, p, price, aa, ll, kk, estimated_fee_usd=est_fee)
            rbp = await self.read.buying_power(a)
            if self.available_buying_power_after_reservations(rbp, aa) < price * 100 + est_fee:
                raise Blocked("buying power changed")
            await self.read.quote(instrument, max_age_seconds=15)
            source_check()

        await self.write_after_approval(trade, "BUY", payload, details, recheck)

    def metadata_alert(self, ticket: Mapping[str, object], body_text: str | None = None) -> None:
        """Record informational NEW AVG updates without altering fill basis or orders."""
        reply = ticket.get("reply_to_message_id")
        if reply is None:
            return
        matches = [t for t in self.journal.doc["trades"] if str(reply) == t["source"]]
        if len(matches) == 1 and body_text:
            m = _NEW_AVG_RE.search(body_text)
            if m:
                matches[0]["swift_reported_avg"] = str(money(m.group(1)))
                self.journal.save()

    def exit_alert(self, ticket: Mapping[str, object]) -> None:
        """Match exit instruction via exact reply link; reject ambiguous exits."""
        reply = ticket.get("reply_to_message_id")
        matches = [t for t in self.journal.doc["trades"] if str(reply) == t["source"]]
        if (
            len(matches) != 1
            or ticket.get("underlying") not in (None, matches[0]["contract"]["symbol"])
        ):
            say("EXIT_REQUIRES_MANUAL_MATCH", source_message_id=ticket.get("source_message_id"))
            self._emit_notification(
                "AMBIGUOUS_EXIT_REJECTED",
                dedupe_key=f"ambig-exit:{ticket.get('source_message_id')}",
                source_message_id=str(ticket.get("source_message_id")),
            )
            return
        matches[0]["exit_requested"] = True
        matches[0]["exit_message_id"] = str(ticket["source_message_id"])
        self.journal.save()
        self._emit_notification(
            "EXIT_REQUEST_MATCHED",
            dedupe_key=f"exit-req:{ticket.get('source_message_id')}",
            entry_source_id=matches[0]["source"],
            contract=matches[0]["contract"]["symbol"],
        )

    def owns_position(
        self,
        trade: Mapping[str, object],
        entry,
        order_map: Mapping[str, dict],
        positions: list[dict],
        active: list[tuple[dict, object, object]],
        known: set[str],
    ) -> None:
        """Verify exact Swift lot ownership across single or multiple same-contract alerts."""
        if entry.processed_quantity != 1 or entry.state not in TERMINAL:
            raise Blocked("entry fill not confirmed")

        option_id = trade["option"]
        swift_open_lots = sum(
            1
            for t, e, x in active
            if t["option"] == option_id
            and e.processed_quantity == 1
            and (not x or x.processed_quantity == 0)
        )
        if swift_open_lots < 1:
            raise Blocked("no open Swift lot found for contract")

        matches = [p for p in positions if p.get("option_id") == option_id]
        if (
            len(matches) != 1
            or matches[0].get("type") != "long"
            or count(matches[0].get("quantity")) < swift_open_lots
            or (
                not self.allow_external_positions
                and count(matches[0].get("quantity")) != swift_open_lots
            )
            or count(matches[0].get("trade_value_multiplier")) != 100
        ):
            raise Blocked("exact owned long position not verified")

        for oid, row in order_map.items():
            if oid in known:
                continue
            legs = row.get("legs")
            if not isinstance(legs, list):
                raise Blocked("unverified brokerage legs")
            if any(isinstance(l, Mapping) and l.get("option_id") == option_id for l in legs):
                if row.get("state") not in TERMINAL or instant(row.get("updated_at")) >= entry.created_at:
                    raise Blocked("another order touched this contract; do not sell")

    def _archive_failed_exit(self, trade: dict[str, object], x) -> None:
        """Archive a terminal unfilled exit order so the position can retry closing."""
        prior = list(trade.get("prior_exits", []))
        prior.append(
            {
                "exit_id": trade.pop("exit_id"),
                "exit_price": trade.pop("exit_price"),
                "exit_ref": trade.pop("exit_ref"),
                "exit_evidence": trade.pop("exit_evidence", None),
                "archived_at_utc": now().isoformat(),
                "terminal_state": x.state,
            }
        )
        trade["prior_exits"] = prior
        trade["exit_attempt_seq"] = int(trade.get("exit_attempt_seq", 0)) + 1
        self.journal.save()

    async def _manage_single_position(
        self,
        trade: dict[str, object],
        e,
        x,
        orders: Mapping[str, dict],
        positions: list[dict],
        active: list[tuple[dict, object, object]],
        known: set[str],
    ) -> None:
        if x is not None:
            if x.state in TERMINAL and not x.processed_quantity:
                say(
                    "EXIT_NOT_FILLED_MANUAL_REVIEW_REQUIRED",
                    source=trade["source"],
                    exit_state=x.state,
                )
                self._emit_notification(
                    "EXIT_ORDER_UNFILLED_TERMINAL",
                    dedupe_key=f"unfilled-exit:{x.broker_id}",
                    source=trade["source"],
                    state=x.state,
                )
                if trade.get("allow_exit_retry") is True:
                    self._archive_failed_exit(trade, x)
                return
            else:
                return

        if (
            e.state in {"queued", "confirmed", "partially_filled"}
            and not trade.get("cancel_ack")
            and (
                trade.get("exit_requested")
                or (now() - e.created_at).total_seconds() >= 3600
            )
        ):
            payload = {"account_number": self.pin, "order_id": e.broker_id}

            async def recheck_cancel() -> None:
                _, rows, _pos = await self.snapshot()
                aa, _, _ = self.reconcile(rows)
                matched = [
                    item for item in aa if item[0]["source"] == trade["source"]
                ]
                if (
                    len(matched) != 1
                    or matched[0][1].state not in {"queued", "confirmed", "partially_filled"}
                    or matched[0][1].broker_id != e.broker_id
                ):
                    raise Blocked("entry no longer cancellable")

            await self.write_after_approval(
                trade,
                "CANCEL",
                payload,
                {
                    "contract": trade["contract"],
                    "reason": "exit alert or 60-minute entry timeout",
                    "entry_state": e.state,
                },
                recheck_cancel,
            )
            return

        if not trade.get("exit_requested") or e.state not in TERMINAL or not e.processed_quantity:
            return

        self.assert_market()
        self.owns_position(trade, e, orders, positions, active, known)
        contract = from_dict(trade["contract"])
        instrument = await self.read.resolve_instrument(contract)
        if instrument.option_id != trade["option"] or instrument.multiplier != 100:
            raise Blocked("contract identity changed")
        quote = await self.read.quote(instrument, max_age_seconds=15)
        price = quote.bid
        instrument = await self.instrument(contract, price)
        payload = order_payload(self.pin, str(trade["option"]), "sell", price, str(uuid4()))
        details = await self.review(
            payload, contract, instrument, str(trade["underlying_type"])
        )
        trade["estimated_exit_fee_usd"] = str(details.get("estimated_fee_usd", "0.00"))
        details.update(
            exit_source_message_id=trade.get("exit_message_id"),
            entry_source_message_id=trade["source"],
        )

        async def recheck_sell() -> None:
            self.assert_market()
            _, rows, pos = await self.snapshot()
            aa, _, kk = self.reconcile(rows)
            matched = [item for item in aa if item[0]["source"] == trade["source"]]
            if len(matched) != 1 or matched[0][2] is not None:
                raise Blocked("exit ownership changed")
            self.owns_position(trade, matched[0][1], rows, pos, aa, kk)
            await self.read.quote(instrument, max_age_seconds=15)

        await self.write_after_approval(trade, "SELL", payload, details, recheck_sell)

    async def manage(self) -> None:
        if not self.journal.doc["trades"]:
            return
        _, orders, positions = await self.snapshot()
        active, _, known = self.reconcile(orders)
        if not active:
            return
        for trade, e, x in list(active):
            try:
                await self._manage_single_position(
                    trade, e, x, orders, positions, active, known
                )
            except (Blocked, MCPContractError, EvidenceError) as exc:
                self.journal.assert_known()
                if len(active) == 1:
                    raise
                say(
                    "POSITION_MANAGEMENT_BLOCKED",
                    source=trade["source"],
                    reason=str(exc),
                )


def verify_source(db: Path, channel: int, ticket: Mapping[str, object], started: datetime) -> str:
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        select = (
            "SELECT r.revision_id,m.latest_revision_id,r.message_id,r.event_kind AS kind,"
            "r.origin,r.eligible_for_future_processing AS eligible,r.received_at_utc AS received,"
            "r.telegram_sent_at_utc AS sent,r.telegram_edited_at_utc AS edited,"
            "r.reply_to_message_id AS reply_id,e.body FROM telegram_message_revisions r "
            "JOIN raw_events e ON e.event_id=r.raw_event_id JOIN telegram_messages m "
            "ON m.channel_id=r.channel_id AND m.message_id=r.message_id "
            "WHERE r.channel_id=? AND r.revision_id=m.latest_revision_id AND "
        )
        rows = con.execute(
            select + "r.message_id=?", (str(channel), str(ticket["source_message_id"]))
        ).fetchall()
        at = now()
        if (
            len(rows) != 1
            or preview_event(dict(rows[0]), started=started, now=at, max_age=120) != ticket
        ):
            raise Blocked("source alert expired, changed, or superseded")
        if ticket["status"] == "ENTRY_PREVIEW_ONLY":
            replies = con.execute(
                select + "r.reply_to_message_id=?",
                (str(channel), str(ticket["source_message_id"])),
            ).fetchall()
            if any(
                parse_message(r["body"]).intent == SignalIntent.CLOSE_100_PERCENT
                for r in replies
            ):
                raise Blocked("exit reply already exists; no late entry")
        return str(rows[0]["body"])


async def build_unattended_authorizer(
    root: Path, client, agent: "Companion", pin: str, channel: int | None, mandate_path: Path, stop_path: Path
) -> UnattendedAuthorizer:
    """Fail closed: any problem raises Blocked and the monitor does not start."""
    try:
        assert channel is not None
        mandate = Mandate.load(mandate_path, pin=pin, source_channel=channel, at=now())
        contract_path = root / "config" / "broker-approval-contract.json"
        if not contract_path.is_file() or contract_path.is_symlink():
            raise MandateError("broker approval contract file missing")
        contract = json.loads(contract_path.read_text(encoding="utf-8-sig"))
        tools = agent.read._discovered or {}
        probe = ConfiguredApprovalProbe(client, contract, tools)
        return UnattendedAuthorizer(
            mandate, pin, probe, root / "state" / "unattended-ledger.json", stop_path,
            notify=lambda code, **f: agent._emit_notification(code, **f),
        )
    except (MandateError, OSError, ValueError, AssertionError) as exc:
        raise Blocked("unattended mandate unusable: " + str(exc)) from exc


async def capture_catalog(root: Path) -> int:
    """Read-only: list MCP tool names/schemas so the owner can write the approval contract."""
    path = root / "secrets" / "oauth.dpapi"
    if not path.is_file() or path.is_symlink():
        raise Blocked("existing OAuth required")
    enrollment = OAuthEnrollment(WindowsDPAPITokenStorage(path), _no_new_auth, _no_new_auth)
    async with authenticated_mcp_client(enrollment) as client:
        advertised = await asyncio.wait_for(client.list_tools(), timeout=30)
        defs = advertised if isinstance(advertised, list) else _obj(advertised).get("tools")
        if not isinstance(defs, list):
            raise Blocked("no usable tools/list response")
        atomic_json(root / "state" / "mcp-tool-catalog.json", capture_tool_catalog(defs))
    say("TOOL_CATALOG_CAPTURED", path=str(root / "state" / "mcp-tool-catalog.json"), broker_calls="list_tools_only")
    return 0


async def monitor(
    root: Path,
    cfg: Settings,
    mandate_path: Path | None = None,
    *,
    external_receiver: bool = False,
) -> None:
    limits = RiskLimits.from_file(root / "config" / "settings.json")
    stop_path = root / "state" / "SWIFT_REVIEWED_STOP"
    health_path = root / "state" / "swift-reviewed-health.json"
    assert_not_stopped(stop_path)
    pin = load_pinned_agentic_account(root)
    journal = Journal(
        root / "state" / "swift-reviewed-live.json", pin, cfg.telegram_channel_id
    )
    journal.assert_known()
    notifier = TelegramNotifier.from_install_root(
        root, pin=pin, sent_fingerprints=journal.doc.get("notifications_sent", [])
    )
    path = root / "secrets" / "oauth.dpapi"
    if not path.is_file() or path.is_symlink():
        raise Blocked("existing OAuth required")
    enrollment = OAuthEnrollment(WindowsDPAPITokenStorage(path), _no_new_auth, _no_new_auth)
    async with authenticated_mcp_client(enrollment) as client:
        approver = confirm_console
        agent = Companion(
            client,
            pin,
            journal,
            lambda *a: approver(*a),
            limits=limits,
            stop_path=stop_path,
            notifier=notifier,
            unattended_mode=mandate_path is not None,
        )
        await agent.identity()
        assert_not_stopped(stop_path)
        if mandate_path is not None:
            approver = await build_unattended_authorizer(
                root, client, agent, pin, cfg.telegram_channel_id, mandate_path, stop_path
            )
        assert_not_stopped(stop_path)
        recovery = FollowupRecovery(journal, agent, cfg.db_path, cfg.telegram_channel_id)
        recovery.initialize_checkpoint()
        assert cfg.telegram_channel_id is not None
        cursor = _cursor(cfg.db_path, cfg.telegram_channel_id)
        started = now()

        def spawn_receiver() -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, "-m", "swift_autotrader", "telegram-listen"],
                cwd=root / "state",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        assert_not_stopped(stop_path)
        child: subprocess.Popen | None = None if external_receiver else spawn_receiver()
        restart_count = 0
        try:
            say(
                "REVIEWED_LIVE_MONITOR_RUNNING",
                per_order_approval=mandate_path is None,
                unattended=mandate_path is not None,
                external_receiver=external_receiver,
                pinned_mcp_only=True,
                portfolio_multi_position=True,
                real_order_submitted=False,
            )
            agent._emit_notification(
                "APPLICATION_STARTED",
                dedupe_key=f"start:{started.isoformat()}",
                mode="REVIEWED_LIVE_PORTFOLIO",
                telegram_notifier_ready=notifier.configured,
            )
            next_manage = 0.0
            next_health = 0.0
            last_pnl_day: date | None = None
            while True:
                if os.path.lexists(stop_path):
                    break
                if child is not None and child.poll() is not None:
                    say("TELEGRAM_RECEIVER_EXITED", exit_code=child.returncode)
                    agent._emit_notification(
                        "CONNECTION_FAILURE",
                        dedupe_key=f"rx-exit:{restart_count}:{child.returncode}",
                        component="telegram-listen",
                        exit_code=str(child.returncode),
                    )
                    if restart_count >= 5 or os.path.lexists(stop_path):
                        raise Blocked("Telegram receiver stopped; max restarts reached")
                    await asyncio.sleep(min(30, 2 ** restart_count))
                    if os.path.lexists(stop_path):
                        break
                    child = spawn_receiver()
                    restart_count += 1

                if time.monotonic() >= next_health:
                    health_record(
                        health_path,
                        journal,
                        "RUNNING",
                        stop_path,
                        metrics=agent.last_metrics,
                        notifier=notifier,
                        unattended_mode=mandate_path is not None,
                    )
                    next_health = time.monotonic() + 5

                try:
                    recovery.run_once()
                except (Blocked, MCPContractError, EvidenceError) as exc:
                    say("FOLLOWUP_RECOVERY_BLOCKED", reason=redact_sensitive(str(exc), pin=pin))
                if os.path.lexists(stop_path):
                    break
                cursor, tickets = scan(
                    cfg.db_path, cfg.telegram_channel_id, cursor, started=started, now=now()
                )
                for ticket in tickets:
                    if os.path.lexists(stop_path):
                        break
                    try:
                        if ticket["status"] == "ENTRY_PREVIEW_ONLY":
                            await agent.entry(
                                ticket,
                                lambda t=ticket: verify_source(
                                    cfg.db_path, cfg.telegram_channel_id, t, started
                                ),
                            )
                        elif ticket["status"] == "MANUAL_POSITION_MATCH_REQUIRED":
                            verify_source(cfg.db_path, cfg.telegram_channel_id, ticket, started)
                            agent.exit_alert(ticket)
                        else:
                            say("ALERT_SKIPPED", reason=ticket["status"])
                    except (Blocked, MCPContractError, EvidenceError) as exc:
                        say("ACTION_BLOCKED", reason=redact_sensitive(str(exc), pin=pin))
                        journal.assert_known()

                if os.path.lexists(stop_path):
                    break
                if time.monotonic() >= next_manage:
                    try:
                        await agent.manage()
                        current_et_day = now().astimezone(NY).date()
                        if (
                            agent.last_metrics is not None
                            and last_pnl_day != current_et_day
                            and now().astimezone(NY).hour >= 16
                        ):
                            last_pnl_day = current_et_day
                            agent._emit_notification(
                                "DAILY_PNL_SUMMARY",
                                dedupe_key=f"pnl:{current_et_day.isoformat()}",
                                date_et=current_et_day.isoformat(),
                                realized_gross_pnl_usd=str(
                                    agent.last_metrics.daily_realized_gross_pnl_usd
                                ),
                                realized_net_pnl_usd=str(
                                    agent.last_metrics.daily_realized_net_pnl_usd
                                ),
                                unrealized_gross_pnl_usd=str(
                                    agent.last_metrics.unrealized_gross_pnl_usd
                                ),
                                total_fees_usd=str(agent.last_metrics.total_fees_usd) if agent.last_metrics.total_fees_usd is not None else None,
                            )
                    except (Blocked, MCPContractError, EvidenceError) as exc:
                        say("MANAGEMENT_BLOCKED", reason=redact_sensitive(str(exc), pin=pin))
                        journal.assert_known()
                    next_manage = time.monotonic() + 15
                await asyncio.sleep(1)
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            try:
                agent._emit_notification(
                    "APPLICATION_STOPPED",
                    dedupe_key=f"stop:{now().isoformat()}",
                    emergency_stop=os.path.lexists(stop_path),
                )
            finally:
                health_record(
                    health_path,
                    journal,
                    "STOPPED",
                    stop_path,
                    metrics=agent.last_metrics,
                    notifier=notifier,
                    unattended_mode=mandate_path is not None,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reviewed-live", action="store_true", required=True)
    parser.add_argument("--unattended-mandate", type=Path, default=None,
                        help="Run without console approval ONLY under an owner-issued mandate")
    parser.add_argument("--external-receiver", action="store_true",
                        help="Consume canonical DB tickets from an existing receiver without spawning telegram-listen")
    parser.add_argument("--capture-tool-catalog", action="store_true",
                        help="Read-only: save MCP tool names/schemas; no orders")
    args = parser.parse_args()
    raise Blocked("review-only candidate: real brokerage execution not certified")
    unattended = args.unattended_mandate is not None
    if os.name != "nt" or os.environ.get("CI") or (not unattended and not sys.stdin.isatty()):
        raise Blocked("local Windows only; console required unless a mandate is supplied")
    root = validate_install_root(args.root)
    os.chdir(root / "state")
    cfg = Settings.from_env()
    if (
        os.environ.get("PAPER_DISPATCH_ENABLED") != "false"
        or not cfg.telegram_enabled
        or cfg.db_path.resolve() != (root / "data" / "swift.sqlite3").resolve()
    ):
        raise Blocked("existing isolated receiver required")
    lock_name = "execution.lock" if args.external_receiver else "service.lock"
    if args.capture_tool_catalog:
        with single_instance(root / "state" / lock_name):
            return asyncio.run(capture_catalog(root))
    if not unattended and input("Start reviewed real-money MCP companion? Type REVIEWED LIVE: ") != "REVIEWED LIVE":
        return 0
    with single_instance(root / "state" / lock_name):
        asyncio.run(
            monitor(
                root,
                cfg,
                args.unattended_mandate,
                external_receiver=args.external_receiver,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        say("COMPANION_STOPPED", reason=type(exc).__name__)
        raise SystemExit(2) from None