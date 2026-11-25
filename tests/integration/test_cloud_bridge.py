"""Cloud bridge: synthetic events, fake transports only. Never contacts Anthropic or Robinhood."""
from __future__ import annotations

import ast
import hashlib
import http.server
import json
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader import cloud_bridge as cb
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog

CHANNEL = -1001234000001  # fabricated
T0 = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
FIRE = "https://api.anthropic.com/v1/claude_code/routines/trig_TESTONLY/fire"
TOKEN = "sk-ant-oat01-SYNTHETIC-NEVER-REAL"
CATALOG = StaticInstrumentCatalog([
    OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13)),
])


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed(db, mid, body, *, sent=T0, reply=None, kind="new", edit_at=None, origin="live"):
    with sqlite3.connect(db) as con:
        rev = con.execute("SELECT COALESCE(MAX(revision),-1)+1 FROM telegram_message_revisions "
                          "WHERE channel_id=? AND message_id=?", (str(CHANNEL), mid)).fetchone()[0]
        con.execute("INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,"
                    "received_at_utc,sent_at_utc,body) VALUES(?,?,?,?,?,?)",
                    (str(CHANNEL), str(mid), rev, iso(sent), iso(sent), body))
        raw = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO telegram_message_revisions(raw_event_id,channel_id,message_id,revision,"
            "event_kind,origin,telegram_sent_at_utc,telegram_edited_at_utc,received_at_utc,"
            "reply_to_message_id,content_sha256,eligible_for_future_processing) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (raw, str(CHANNEL), mid, rev, kind, origin, iso(sent),
             iso(edit_at) if edit_at else None, iso(sent), reply, f"fx-{mid}-{rev}"))
        rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO telegram_messages(channel_id,message_id,latest_revision_id,"
                    "latest_effective_at_utc,first_seen_at_utc) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(channel_id,message_id) DO UPDATE SET "
                    "latest_revision_id=excluded.latest_revision_id",
                    (str(CHANNEL), mid, rid, iso(edit_at or sent), iso(sent)))
    SignalMatcher(db, CHANNEL, catalog=CATALOG).process_pending()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeTransport:
    def __init__(self, *results: cb.HttpResult) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        if self.results:
            return self.results.pop(0)
        n = len(self.calls)
        return cb.HttpResult(200, json.dumps({"type": "routine_fire",
                                              "claude_code_session_id": f"session_{n:04d}"}))


class StaticToken:
    def get(self) -> str:
        return TOKEN


@pytest.fixture
def env(tmp_path):
    live = tmp_path / "live.sqlite3"
    apply_migrations(live)
    config = cb.BridgeConfig(live_db=live, outbox_db=tmp_path / "outbox.sqlite3",
                             state_dir=tmp_path / "state", fire_url=FIRE,
                             channel_hash_salt="synthetic-salt-0123456789")
    return live, config


def bridge(config, transport, clock):
    return cb.CloudBridge(config, StaticToken(), transport=transport, clock=clock)


def states(config):
    with sqlite3.connect(config.outbox_db) as con:
        return [r[0] for r in con.execute("SELECT state FROM bridge_events ORDER BY revision_id")]


def test_fresh_entry_delivered_once_with_redacted_payload(env):
    live, config = env
    seed(live, 101, "SPY 680C 11/13 @ 1.20")
    t = FakeTransport()
    b = bridge(config, t, Clock(T0 + timedelta(seconds=5)))
    assert b.cycle() == {"DELIVERED": 1}
    b.cycle()
    b.collect()
    assert len(t.calls) == 1
    call = t.calls[0]
    assert call["headers"]["anthropic-beta"] == cb.BETA_HEADER
    text = call["body"]["text"]
    payload = json.loads(text.split("\n", 1)[1])
    assert payload["intent"] == "ENTRY" and payload["mode"] == "REVIEW_ONLY"
    assert payload["contract"]["original_limit"] in ("1.20", "1.2")
    assert "SPY 680C 11/13 @ 1.20" not in text  # no raw paid text
    assert str(CHANNEL) not in text and TOKEN not in text  # no channel ID, no token
    assert states(config) == ["DELIVERED"]


def test_stale_entry_rejected_and_never_sent(env):
    live, config = env
    seed(live, 102, "SPY 680C 11/13 @ 1.20")
    t = FakeTransport()
    bridge(config, t, Clock(T0 + timedelta(seconds=121))).cycle()
    assert t.calls == [] and states(config) == ["REJECTED_STALE"]


def test_recovered_history_never_replayed(env):
    live, config = env
    seed(live, 14635, "USO 130P 1/15 @ 6.00", origin="recovered")
    t = FakeTransport()
    bridge(config, t, Clock(T0 + timedelta(seconds=1))).cycle()
    assert t.calls == []
    assert set(states(config)) <= {"REJECTED_RECOVERED"}


def test_edit_is_new_revision_event(env):
    live, config = env
    seed(live, 103, "SPY 680C 11/13 @ 1.20")
    seed(live, 103, "SPY 680C 11/13 @ 1.25", kind="edit", edit_at=T0 + timedelta(seconds=3))
    t = FakeTransport()
    bridge(config, t, Clock(T0 + timedelta(seconds=10))).cycle()
    revisions = [json.loads(c["body"]["text"].split("\n", 1)[1])["telegram_revision"]
                 for c in t.calls]
    assert revisions == sorted(set(revisions)) and 0 in revisions
    with sqlite3.connect(config.outbox_db) as con:
        ids = [r[0] for r in con.execute("SELECT event_id FROM bridge_events")]
    assert len(ids) == len(set(ids))


def test_restart_resumes_without_duplicates(env):
    live, config = env
    seed(live, 104, "SPY 680C 11/13 @ 1.20")
    clock = Clock(T0 + timedelta(seconds=2))
    t1 = FakeTransport()
    bridge(config, t1, clock).cycle()
    t2 = FakeTransport()
    bridge(config, t2, clock).cycle()  # simulated Windows restart: new process, same files
    assert len(t1.calls) == 1 and t2.calls == []


def test_crash_mid_request_becomes_ambiguous_not_resent(env):
    live, config = env
    seed(live, 105, "SPY 680C 11/13 @ 1.20")
    clock = Clock(T0 + timedelta(seconds=2))

    def crash(*_args):
        raise KeyboardInterrupt  # process killed after durable "about to send"

    with pytest.raises(KeyboardInterrupt):
        bridge(config, crash, clock).cycle()
    t = FakeTransport()
    bridge(config, t, clock).cycle()
    assert t.calls == [] and states(config) == ["AMBIGUOUS"]


@pytest.mark.parametrize(("result", "expected"), [
    (cb.HttpResult(None, "", cb.ErrorClass.TIMEOUT_AFTER_SEND), "AMBIGUOUS"),  # delayed start
    (cb.HttpResult(500, "boom"), "AMBIGUOUS"),
    (cb.HttpResult(200, "{}"), "AMBIGUOUS"),  # unexpected response shape
    (cb.HttpResult(401, "unauthorized"), "FAILED_AUTH"),
    (cb.HttpResult(400, "bad"), "FAILED_PERMANENT"),
    (cb.HttpResult(429, '{"error":"usage limit reached"}'), "FAILED_USAGE_LIMIT"),
])
def test_error_classification(env, result, expected):
    live, config = env
    seed(live, 106, "SPY 680C 11/13 @ 1.20")
    t = FakeTransport(result)
    b = bridge(config, t, Clock(T0 + timedelta(seconds=2)))
    b.cycle()
    b.cycle()
    assert len(t.calls) == 1 and states(config) == [expected]


@pytest.mark.parametrize("result", [
    cb.HttpResult(None, "", cb.ErrorClass.NETWORK_NOT_SENT),  # lost network
    cb.HttpResult(429, "rate limited"),
    cb.HttpResult(503, "unavailable"),
])
def test_retryable_while_fresh_then_stale(env, result):
    live, config = env
    seed(live, 107, "SPY 680C 11/13 @ 1.20")
    clock = Clock(T0 + timedelta(seconds=2))
    t = FakeTransport(result)
    b = bridge(config, t, clock)
    b.cycle()
    assert states(config) == ["PENDING"]
    b.cycle()
    assert states(config) == ["DELIVERED"] and len(t.calls) == 2
    seed(live, 108, "SPY 680C 11/13 @ 1.20")
    t.results = [result]
    b.cycle()
    clock.now = T0 + timedelta(seconds=200)
    b.cycle()
    assert states(config)[-1] == "REJECTED_STALE"


def test_concurrent_signals_respect_hourly_budget(env, tmp_path):
    live, config = env
    config = cb.BridgeConfig(**{**{f: getattr(config, f) for f in config.__slots__},
                                "hourly_fire_budget": 2})
    for mid in (201, 202, 203):
        seed(live, mid, "SPY 680C 11/13 @ 1.20")
    t = FakeTransport()
    b = bridge(config, t, Clock(T0 + timedelta(seconds=2)))
    counts = b.cycle()
    assert len(t.calls) == 2 and counts.get("CLIENT_BUDGET") == 1
    assert b.status()["hourly_budget_left"] == 0


def test_two_workers_cannot_double_send(env):
    live, config = env
    seed(live, 109, "SPY 680C 11/13 @ 1.20")
    clock = Clock(T0 + timedelta(seconds=2))
    a, b = bridge(config, FakeTransport(), clock), bridge(config, FakeTransport(), clock)
    a.collect()
    rows = sqlite3.connect(config.outbox_db)
    rows.row_factory = sqlite3.Row
    row = rows.execute("SELECT * FROM bridge_events").fetchone()
    first = a._deliver_one(row)
    second = b._deliver_one(row)  # stale snapshot of the same row
    assert first == "DELIVERED" and second == "SKIPPED_ALREADY_HANDLED"
    assert len(a.transport.calls) + len(b.transport.calls) == 1


def test_kill_switch_and_bridge_pause(env):
    live, config = env
    with sqlite3.connect(live) as con:
        con.execute("UPDATE paper_engine_control SET emergency_stop=1")
    seed(live, 110, "SPY 680C 11/13 @ 1.20")
    clock = Clock(T0 + timedelta(seconds=2))
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "bridge.paused").write_text("paused")
    t = FakeTransport()
    b = bridge(config, t, clock)
    assert b.cycle() == {"PAUSED": 1} and t.calls == []
    (config.state_dir / "bridge.paused").unlink()
    b.cycle()
    payload = json.loads(t.calls[0]["body"]["text"].split("\n", 1)[1])
    assert payload["engine_emergency_stop"] is True


def test_ignored_and_add_alerts_not_forwarded(env):
    live, config = env
    seed(live, 111, "good morning everyone")
    t = FakeTransport()
    bridge(config, t, Clock(T0 + timedelta(seconds=2))).cycle()
    for c in t.calls:
        assert json.loads(c["body"]["text"].split("\n", 1)[1])["intent"] != "IGNORE_NON_OPTION"


def test_live_database_is_never_modified(env):
    live, config = env
    seed(live, 112, "SPY 680C 11/13 @ 1.20")
    before = hashlib.sha256(Path(live).read_bytes()).hexdigest()
    bridge(config, FakeTransport(), Clock(T0 + timedelta(seconds=2))).cycle()
    assert hashlib.sha256(Path(live).read_bytes()).hexdigest() == before


def test_heartbeat_written(env):
    live, config = env
    seed(live, 113, "SPY 680C 11/13 @ 1.20")
    bridge(config, FakeTransport(), Clock(T0 + timedelta(seconds=2))).cycle()
    beat = json.loads((config.state_dir / "bridge_heartbeat.json").read_text())
    assert beat["states"] == {"DELIVERED": 1} and beat["auth_failed"] is False


def test_config_rejects_unofficial_url_and_loose_limits(tmp_path):
    base = dict(live_db=tmp_path / "a", outbox_db=tmp_path / "b", state_dir=tmp_path,
                channel_hash_salt="synthetic-salt-0123456789")
    with pytest.raises(ValueError):
        cb.BridgeConfig(fire_url="https://evil.example/fire", **base)
    with pytest.raises(ValueError):
        cb.BridgeConfig(fire_url=FIRE, entry_max_age_s=300, **base)
    with pytest.raises(ValueError):
        cb.BridgeConfig(fire_url=FIRE, hourly_fire_budget=31, **base)


def test_bridge_has_no_broker_write_path():
    source = Path(cb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("broker" in name or "live_" in name for name in imported)
    for forbidden in ("place_option_order", "cancel_option_order", "LiveBroker("):
        assert forbidden not in source.replace("``LiveBroker``", "")


def test_real_http_loopback_latency(env, tmp_path):
    """End-to-end through urllib against a loopback stub of the /fire contract."""
    live, config = env
    seen: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"auth": self.headers["Authorization"], "body": json.loads(body)})
            out = json.dumps({"type": "routine_fire", "claude_code_session_id": "session_LOOP",
                              "claude_code_session_url": "https://claude.ai/code/session_LOOP"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out.encode())

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def loopback(url, headers, body, timeout):
        assert url == FIRE
        return cb.urllib_transport(f"http://127.0.0.1:{port}/fire", headers, body, timeout)

    now = datetime.now(UTC)
    seed(live, 114, "SPY 680C 11/13 @ 1.20", sent=now)
    started = time.perf_counter()
    b = cb.CloudBridge(config, StaticToken(), transport=loopback)
    assert b.cycle() == {"DELIVERED": 1}
    elapsed_ms = (time.perf_counter() - started) * 1000
    server.shutdown()
    assert seen[0]["auth"] == f"Bearer {TOKEN}"
    assert elapsed_ms < 2000
    print(f"LOOPBACK_BRIDGE_CYCLE_MS={elapsed_ms:.1f}")
