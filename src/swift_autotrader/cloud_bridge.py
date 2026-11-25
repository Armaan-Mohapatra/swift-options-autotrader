"""Experimental, REVIEW-ONLY bridge: parsed Swift decisions -> Claude cloud scheduled task.

Boundary (do not weaken):
- Reads the live Swift SQLite database strictly read-only (``mode=ro``). It never writes the
  live database, never imports or calls ``LiveBroker``, and never places/cancels orders.
- The cloud task it fires is a *review/audit* session. Trade execution stays in the single
  deterministic writer ``broker/live_broker.py::LiveBroker``. This module is not a second
  brokerage dispatcher.
- Outbound payloads carry parsed, structured fields only: no raw paid Telegram text, no channel
  ID (a salted hash only), no account numbers, no credentials.
- Durable outbox in its OWN SQLite file. Stable event IDs, duplicate suppression, stale-entry
  rejection (120 s default), never-replay for recovered/historical revisions, a client-side
  hourly fire budget below the platform's 30/hour/task limit, and no blind resend after an
  ambiguous outcome.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

SCHEMA_VERSION = 1
BETA_HEADER = "experimental-cc-routine-2026-04-01"
API_VERSION = "2023-06-01"
FIRE_URL_PREFIX = "https://api.anthropic.com/v1/claude_code/routines/"
FORWARDED_STATUSES = ("RECORDED", "REVIEW_REQUIRED")
FORWARDED_INTENTS = ("ENTRY", "CLOSE_100_PERCENT", "METADATA_ONLY", "REVIEW_REQUIRED")


class State(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_RECOVERED = "REJECTED_RECOVERED"
    AMBIGUOUS = "AMBIGUOUS"  # request may have started a session; never auto-resent
    FAILED_AUTH = "FAILED_AUTH"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    FAILED_USAGE_LIMIT = "FAILED_USAGE_LIMIT"


TERMINAL = {s for s in State if s is not State.PENDING}


class ErrorClass(StrEnum):
    NETWORK_NOT_SENT = "NETWORK_NOT_SENT"
    TIMEOUT_AFTER_SEND = "TIMEOUT_AFTER_SEND"
    HTTP_RATE_LIMITED = "HTTP_RATE_LIMITED"
    CLIENT_BUDGET = "CLIENT_BUDGET"
    HTTP_AUTH = "HTTP_AUTH"
    HTTP_CLIENT = "HTTP_CLIENT"
    HTTP_SERVER_AMBIGUOUS = "HTTP_SERVER_AMBIGUOUS"
    HTTP_UNAVAILABLE = "HTTP_UNAVAILABLE"
    USAGE_LIMIT = "USAGE_LIMIT"
    BAD_RESPONSE = "BAD_RESPONSE"
    PAUSED = "PAUSED"


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    live_db: Path
    outbox_db: Path
    state_dir: Path
    fire_url: str
    channel_hash_salt: str
    entry_max_age_s: int = 120
    followup_max_age_s: int = 3600
    hourly_fire_budget: int = 25
    http_timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if not self.fire_url.startswith(FIRE_URL_PREFIX) or not self.fire_url.endswith("/fire"):
            raise ValueError("fire_url must be an official Claude Code scheduled-task /fire URL")
        if not 1 <= self.hourly_fire_budget <= 30:
            raise ValueError("hourly_fire_budget must be 1..30 (platform limit is 30/hour/task)")
        if not 1 <= self.entry_max_age_s <= 120:
            raise ValueError("entry freshness may not exceed the 120 s owner limit")
        if len(self.channel_hash_salt) < 16:
            raise ValueError("channel hash salt too short")


class TokenProvider(Protocol):
    def get(self) -> str: ...


class DPAPITokenFile:
    """Windows user-scope DPAPI ciphertext. The bearer token never sits in plaintext on disk."""

    def __init__(self, path: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("DPAPI token storage requires Windows")
        self.path = Path(path)

    def get(self) -> str:
        import win32crypt  # type: ignore[import-not-found]

        try:
            clear = win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)[1]
        except Exception as exc:  # noqa: BLE001 - never surface ciphertext details
            raise RuntimeError("bridge token unavailable") from exc
        token = clear.decode("utf-8").strip()
        if not token:
            raise RuntimeError("bridge token unavailable")
        return token

    def store(self, token: str) -> None:
        import win32crypt  # type: ignore[import-not-found]

        token = token.strip()
        if not token.startswith("sk-ant-"):
            raise ValueError("unexpected token format")
        cipher = win32crypt.CryptProtectData(token.encode("utf-8"), None, None, None, None, 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(cipher)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int | None
    body: str
    error: ErrorClass | None = None


Transport = Callable[[str, dict[str, str], bytes, float], HttpResult]


def urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResult:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
            return HttpResult(response.status, response.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read(65536).decode("utf-8", "replace"))
    except TimeoutError:
        return HttpResult(None, "", ErrorClass.TIMEOUT_AFTER_SEND)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return HttpResult(None, "", ErrorClass.TIMEOUT_AFTER_SEND)
        return HttpResult(None, "", ErrorClass.NETWORK_NOT_SENT)
    except ConnectionError:
        return HttpResult(None, "", ErrorClass.NETWORK_NOT_SENT)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(UTC)


def event_id_for(channel_id: str, message_id: int, revision: int, intent: str, status: str) -> str:
    raw = f"swift-bridge-v1|{channel_id}|{message_id}|{revision}|{intent}|{status}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bridge_events(
    event_id TEXT PRIMARY KEY,
    revision_id INTEGER NOT NULL UNIQUE,
    message_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    intent TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_effective_at_utc TEXT NOT NULL,
    enqueued_at_utc TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at_utc TEXT,
    last_error_class TEXT,
    delivered_at_utc TEXT,
    session_id TEXT,
    fire_latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bridge_state ON bridge_events(state, enqueued_at_utc);
CREATE TABLE IF NOT EXISTS bridge_attempts(
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES bridge_events(event_id),
    attempted_at_utc TEXT NOT NULL,
    http_status INTEGER,
    error_class TEXT,
    sent INTEGER NOT NULL CHECK(sent IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_bridge_attempts_time ON bridge_attempts(attempted_at_utc);
"""


class CloudBridge:
    def __init__(
        self,
        config: BridgeConfig,
        tokens: TokenProvider,
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.tokens = tokens
        self.transport = transport
        self.clock = clock
        self.monotonic = monotonic
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._outbox()) as con, con:
            con.executescript(OUTBOX_SCHEMA)
            con.execute("INSERT OR IGNORE INTO bridge_meta VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),))

    # -- storage -------------------------------------------------------------------------
    def _outbox(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.config.outbox_db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _live(self) -> sqlite3.Connection:
        uri = f"file:{self.config.live_db.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA query_only=ON")
        return con

    def _meta(self, con: sqlite3.Connection, key: str, default: str) -> str:
        row = con.execute("SELECT value FROM bridge_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @property
    def paused(self) -> bool:
        return (self.config.state_dir / "bridge.paused").exists()

    def _channel_hash(self, channel_id: str) -> str:
        digest = hashlib.sha256(f"{self.config.channel_hash_salt}|{channel_id}".encode())
        return digest.hexdigest()[:16]

    # -- collection ----------------------------------------------------------------------
    def collect(self) -> int:
        """Copy new parser decisions into the outbox exactly once. Returns new events."""
        now = self.clock()
        with closing(self._outbox()) as out:
            checkpoint = int(self._meta(out, "last_revision_id", "0"))
        with closing(self._live()) as live:
            control = live.execute(
                "SELECT paused, emergency_stop FROM paper_engine_control WHERE singleton=1"
            ).fetchone()
            rows = live.execute(
                "SELECT p.revision_id,p.channel_id,p.message_id,p.intent,p.status,p.reason,"
                "p.signal_id,p.processed_at_utc,r.revision,r.event_kind,r.origin,"
                "r.telegram_sent_at_utc,r.telegram_edited_at_utc,r.received_at_utc,"
                "r.reply_to_message_id,s.underlying,s.option_right,s.strike,"
                "s.expiration_date,s.original_limit,s.swift_reported_avg "
                "FROM signal_parser_decisions p "
                "JOIN telegram_message_revisions r ON r.revision_id=p.revision_id "
                "LEFT JOIN option_signals s ON s.signal_id=p.signal_id "
                "WHERE p.revision_id>? ORDER BY p.revision_id LIMIT 500",
                (checkpoint,),
            ).fetchall()
        created = 0
        newest = checkpoint
        with closing(self._outbox()) as out, out:
            out.execute("BEGIN IMMEDIATE")
            for row in rows:
                newest = max(newest, int(row["revision_id"]))
                if row["status"] not in FORWARDED_STATUSES or row["intent"] not in FORWARDED_INTENTS:
                    continue
                effective = _parse(row["telegram_edited_at_utc"] or row["telegram_sent_at_utc"])
                event_id = event_id_for(str(row["channel_id"]), int(row["message_id"]),
                                        int(row["revision"]), row["intent"], row["status"])
                payload = {
                    "schema": "swift-bridge-event/v1",
                    "event_id": event_id,
                    "mode": "REVIEW_ONLY",
                    "channel_hash": self._channel_hash(str(row["channel_id"])),
                    "telegram_message_id": int(row["message_id"]),
                    "telegram_revision": int(row["revision"]),
                    "event_kind": row["event_kind"],
                    "reply_to_message_id": row["reply_to_message_id"],
                    "intent": row["intent"],
                    "parser_status": row["status"],
                    "parser_reason": row["reason"],
                    "signal_id": row["signal_id"],
                    "contract": None if row["underlying"] is None else {
                        "underlying": row["underlying"], "right": row["option_right"],
                        "strike": row["strike"], "expiration": row["expiration_date"],
                        "original_limit": row["original_limit"],
                        "swift_reported_avg": row["swift_reported_avg"],
                    },
                    "source_sent_at_utc": row["telegram_sent_at_utc"],
                    "source_edited_at_utc": row["telegram_edited_at_utc"],
                    "received_at_utc": row["received_at_utc"],
                    "parser_processed_at_utc": row["processed_at_utc"],
                    "engine_paused": bool(control["paused"]) if control else None,
                    "engine_emergency_stop": bool(control["emergency_stop"]) if control else None,
                }
                state = State.PENDING
                if row["origin"] != "live":
                    state = State.REJECTED_RECOVERED
                elif self._too_old(row["intent"], effective, now):
                    state = State.REJECTED_STALE
                inserted = out.execute(
                    "INSERT OR IGNORE INTO bridge_events(event_id,revision_id,message_id,revision,"
                    "event_kind,origin,intent,status,payload_json,source_effective_at_utc,"
                    "enqueued_at_utc,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, int(row["revision_id"]), int(row["message_id"]), int(row["revision"]),
                     row["event_kind"], row["origin"], row["intent"], row["status"],
                     json.dumps(payload, sort_keys=True), _iso(effective), _iso(now), state.value),
                ).rowcount
                created += inserted
            out.execute("INSERT INTO bridge_meta VALUES('last_revision_id',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(newest),))
        return created

    def _too_old(self, intent: str, effective: datetime, now: datetime) -> bool:
        limit = self.config.entry_max_age_s if intent == "ENTRY" else self.config.followup_max_age_s
        return now - effective > timedelta(seconds=limit)

    # -- delivery ------------------------------------------------------------------------
    def _budget_left(self, con: sqlite3.Connection, now: datetime) -> int:
        since = _iso(now - timedelta(hours=1))
        used = con.execute("SELECT COUNT(*) FROM bridge_attempts WHERE sent=1 AND "
                           "attempted_at_utc>?", (since,)).fetchone()[0]
        return self.config.hourly_fire_budget - int(used)

    def deliver(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        if self.paused:
            counts[ErrorClass.PAUSED] = 1
            return counts
        with closing(self._outbox()) as con:
            pending = con.execute("SELECT * FROM bridge_events WHERE state='PENDING' "
                                  "ORDER BY source_effective_at_utc, revision_id").fetchall()
        for row in pending:
            outcome = self._deliver_one(row)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def _deliver_one(self, row: sqlite3.Row) -> str:
        now = self.clock()
        if self._too_old(row["intent"], _parse(row["source_effective_at_utc"]), now):
            self._finish(row["event_id"], State.REJECTED_STALE, None)
            return State.REJECTED_STALE
        with closing(self._outbox()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT state FROM bridge_events WHERE event_id=?",
                                  (row["event_id"],)).fetchone()
            if current is None or current[0] != State.PENDING:
                return "SKIPPED_ALREADY_HANDLED"  # another worker or restart got here first
            if self._budget_left(con, now) <= 0:
                con.execute("UPDATE bridge_events SET last_error_class=? WHERE event_id=?",
                            (ErrorClass.CLIENT_BUDGET.value, row["event_id"]))
                return ErrorClass.CLIENT_BUDGET
            # Record "about to send" durably BEFORE the network call: if we crash mid-request
            # the event becomes AMBIGUOUS on restart rather than silently resent.
            con.execute("INSERT INTO bridge_attempts(event_id,attempted_at_utc,sent) "
                        "VALUES(?,?,1)", (row["event_id"], _iso(now)))
            attempt_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute("UPDATE bridge_events SET state='AMBIGUOUS',attempts=attempts+1,"
                        "last_attempt_at_utc=? WHERE event_id=?", (_iso(now), row["event_id"]))
        text = "SWIFT_BRIDGE_EVENT v1 (structured data, not instructions)\n" + row["payload_json"]
        headers = {
            "Authorization": f"Bearer {self.tokens.get()}",
            "anthropic-beta": BETA_HEADER,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }
        started = self.monotonic()
        result = self.transport(self.config.fire_url, headers,
                                json.dumps({"text": text}).encode("utf-8"),
                                self.config.http_timeout_s)
        latency_ms = int((self.monotonic() - started) * 1000)
        state, error, session = self._classify(result)
        sent = 0 if error in (ErrorClass.NETWORK_NOT_SENT, ErrorClass.HTTP_UNAVAILABLE,
                              ErrorClass.HTTP_RATE_LIMITED) else 1
        with closing(self._outbox()) as con, con:
            con.execute("UPDATE bridge_attempts SET http_status=?,error_class=?,sent=? "
                        "WHERE attempt_id=?",
                        (result.status, error.value if error else None, sent, attempt_id))
            con.execute(
                "UPDATE bridge_events SET state=?,last_error_class=?,session_id=?,"
                "delivered_at_utc=?,fire_latency_ms=? WHERE event_id=?",
                (state.value, error.value if error else None, session,
                 _iso(self.clock()) if state is State.DELIVERED else None,
                 latency_ms, row["event_id"]))
        return state.value if error is None else error.value

    @staticmethod
    def _classify(result: HttpResult) -> tuple[State, ErrorClass | None, str | None]:
        if result.error is ErrorClass.NETWORK_NOT_SENT:
            return State.PENDING, ErrorClass.NETWORK_NOT_SENT, None
        if result.error is ErrorClass.TIMEOUT_AFTER_SEND:
            return State.AMBIGUOUS, ErrorClass.TIMEOUT_AFTER_SEND, None
        status = result.status or 0
        lowered = result.body.lower()
        if 200 <= status < 300:
            try:
                session = json.loads(result.body).get("claude_code_session_id")
            except (ValueError, AttributeError):
                session = None
            if not isinstance(session, str) or not session.startswith("session_"):
                return State.AMBIGUOUS, ErrorClass.BAD_RESPONSE, None
            return State.DELIVERED, None, session
        if "usage" in lowered and ("limit" in lowered or "exhaust" in lowered):
            return State.FAILED_USAGE_LIMIT, ErrorClass.USAGE_LIMIT, None
        if status == 429:
            return State.PENDING, ErrorClass.HTTP_RATE_LIMITED, None
        if status in (401, 403):
            return State.FAILED_AUTH, ErrorClass.HTTP_AUTH, None
        if status == 503:
            return State.PENDING, ErrorClass.HTTP_UNAVAILABLE, None
        if status >= 500:
            return State.AMBIGUOUS, ErrorClass.HTTP_SERVER_AMBIGUOUS, None
        return State.FAILED_PERMANENT, ErrorClass.HTTP_CLIENT, None

    def _finish(self, event_id: str, state: State, error: ErrorClass | None) -> None:
        with closing(self._outbox()) as con, con:
            con.execute("UPDATE bridge_events SET state=?,last_error_class=? WHERE event_id=? "
                        "AND state='PENDING'", (state.value, error.value if error else None,
                                                event_id))

    # -- health --------------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        with closing(self._outbox()) as con:
            states = {r[0]: r[1] for r in con.execute(
                "SELECT state, COUNT(*) FROM bridge_events GROUP BY state")}
            last = con.execute("SELECT delivered_at_utc,fire_latency_ms FROM bridge_events "
                               "WHERE state='DELIVERED' ORDER BY delivered_at_utc DESC LIMIT 1"
                               ).fetchone()
            budget = self._budget_left(con, self.clock())
        return {
            "schema": "swift-bridge-heartbeat/v1",
            "at_utc": _iso(self.clock()),
            "paused": self.paused,
            "states": states,
            "ambiguous_needs_review": states.get(State.AMBIGUOUS.value, 0),
            "auth_failed": states.get(State.FAILED_AUTH.value, 0) > 0,
            "usage_limited": states.get(State.FAILED_USAGE_LIMIT.value, 0) > 0,
            "hourly_budget_left": budget,
            "last_delivered_at_utc": last[0] if last else None,
            "last_fire_latency_ms": last[1] if last else None,
        }

    def write_heartbeat(self) -> Path:
        path = self.config.state_dir / "bridge_heartbeat.json"
        fd, tmp = tempfile.mkstemp(dir=self.config.state_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self.status(), stream, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path

    def cycle(self) -> dict[str, int]:
        self.collect()
        counts = self.deliver()
        self.write_heartbeat()
        return counts


def load_config(path: Path) -> BridgeConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return BridgeConfig(
        live_db=Path(data["live_db"]), outbox_db=Path(data["outbox_db"]),
        state_dir=Path(data["state_dir"]), fire_url=data["fire_url"],
        channel_hash_salt=data["channel_hash_salt"],
        entry_max_age_s=int(data.get("entry_max_age_s", 120)),
        followup_max_age_s=int(data.get("followup_max_age_s", 3600)),
        hourly_fire_budget=int(data.get("hourly_fire_budget", 25)),
        http_timeout_s=float(data.get("http_timeout_s", 15.0)),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(prog="swift-cloud-bridge",
                                     description="Review-only bridge; never places orders")
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("enroll-token", help="Store the /fire bearer token with Windows DPAPI")
    sub.add_parser("status", help="Print heartbeat JSON (no network)")
    run = sub.add_parser("run", help="Collect and deliver")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    token_file = DPAPITokenFile(config.state_dir / "secrets" / "bridge_fire_token.dpapi")
    if args.command == "enroll-token":
        token_file.store(getpass.getpass("Paste scheduled-task API token (hidden): "))
        print("stored")
        return 0
    bridge = CloudBridge(config, token_file)
    if args.command == "status":
        print(json.dumps(bridge.status(), indent=2, sort_keys=True))
        return 0
    from .windows_runtime import single_instance

    with single_instance(config.state_dir / "bridge.lock"):
        while True:
            bridge.cycle()
            if args.once:
                return 0
            time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
