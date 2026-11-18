"""Read-only loopback web interface never exposes paid text by default."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from swift_autotrader.analytics.metrics import PaperAnalytics
from swift_autotrader.dashboard.app import build_handler, serve_local
from swift_autotrader.database.migrate import apply_migrations


@contextmanager
def local_server(path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_is_read_only_and_local_only(tmp_path):
    path = tmp_path / "dashboard.sqlite3"
    apply_migrations(path)
    with local_server(path) as url:
        with urlopen(url + "/") as response:
            html = response.read().decode()
            assert response.status == 200
            assert "Swift / Local Monitor" in html
            assert "form action" not in html
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        with urlopen(url + "/static/dashboard.js") as response:
            assert b"/api/status" in response.read()
        with urlopen(url + "/api/status") as response:
            payload = json.loads(response.read())
            assert payload["mode"] == "PAPER_ONLY"
            assert payload["real_broker_writes_enabled"] is False
        with pytest.raises(HTTPError) as blocked:
            urlopen(Request(url + "/api/status", method="POST"))
        assert blocked.value.code == 405
        with pytest.raises(HTTPError) as blocked:
            urlopen(Request(url + "/api/status", headers={"Host": "attacker.example"}))
        assert blocked.value.code == 403
        with pytest.raises(HTTPError) as blocked:
            urlopen(url + "/api/status?leak=1")
        assert blocked.value.code == 400
        with pytest.raises(HTTPError) as blocked:
            urlopen(url + "/trade")
        assert blocked.value.code == 404


def test_private_telegram_body_is_not_in_json_status(tmp_path):
    path = tmp_path / "private.sqlite3"
    apply_migrations(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO raw_events(channel_id,telegram_message_id,"
                   "edit_version,received_at_utc,sent_at_utc,body)"
                   "VALUES('-1001234567000','1',0,'2026-11-12T15:00:00Z',"
                   "'2026-11-12T15:00:00Z','SYNTHETIC_SECRET_PAYLOAD_HIDE_ME')")
        raw_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO telegram_message_revisions(raw_event_id,channel_id,"
                   "message_id,revision,event_kind,origin,telegram_sent_at_utc,"
                   "received_at_utc,content_sha256) VALUES(?, '-1001234567000',1,0,"
                   "'new','live','2026-11-12T15:00:00Z',"
                   "'2026-11-12T15:00:00Z','synthetic-hash')", (raw_id,))
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO signal_parser_decisions(revision_id,channel_id,message_id,"
                   "intent,status,reason,processed_at_utc) VALUES(?,'-1001234567000',"
                   "1,'IGNORE_NON_OPTION','IGNORED','COMMENTARY','2026-11-12T15:00:01Z')",
                   (rid,))
    with local_server(path) as url, urlopen(url + "/api/status") as response:
        result = response.read().decode()
        assert "SYNTHETIC_SECRET_PAYLOAD_HIDE_ME" not in result
        assert "COMMENTARY" in result
    internal = PaperAnalytics(path).snapshot(as_of=datetime.now(UTC))
    assert "text" not in internal["recent_messages"][0]


def test_dashboard_cannot_bind_all_interfaces(tmp_path):
    with pytest.raises(ValueError):
        serve_local(tmp_path / "none.db", host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        serve_local(tmp_path / "none.db", host="127.0.0.1", port=65536)


def test_missing_database_returns_unavailable_not_fake_status(tmp_path):
    with local_server(tmp_path / "missing.db") as url:
        with pytest.raises(HTTPError) as error:
            urlopen(url + "/api/status")
        assert error.value.code == 503
