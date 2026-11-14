"""Local read-only status UI; no remote assets, cookies or trade endpoints."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from swift_autotrader.analytics.metrics import CostModel, PaperAnalytics

# No inline scripts or third-party remote assets. The browser fetches only localhost.
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swift Paper Console</title><style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif;
background:#0c1220;color:#e5ebfa}body{margin:0;max-width:1250px;padding:30px 28px 75px}
h1{font-size:26px;letter-spacing:-.035em;margin:0}p,small{color:#a0b0d0}
nav{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #29374f;
padding-bottom:20px}
#mode{color:#9bf1d2;font-size:12px;font-weight:700;letter-spacing:.1em}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:23px 0}
.card,section{background:#152037;border:1px solid #2a3852;border-radius:14px;padding:18px}
.card strong{font-size:23px;display:block;margin:6px 0;font-variant-numeric:tabular-nums}
section{margin-top:17px;overflow-x:auto}h2{font-size:16px;margin:0 0 12px}
.table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}
th,td{border-bottom:1px solid #2c3b53;padding:10px 9px;white-space:nowrap}
th{color:#a0b0d0;font-weight:500}td{color:#e4e8f6}
.flag{color:#f0b57b}.ok{color:#98e6c2}.missing{color:#a0b0d0}
#status{font-size:12px;color:#a0b0d0}
</style></head><body>
<nav><div><h1>Swift / Paper Console</h1>
<small>Local only · simulated fills · no broker orders</small></div>
<div id="mode">PAPER ONLY</div></nav>
<p id="status">Connecting to local read-only status …</p><div class="cards" id="cards"></div>
<section><h2>Pending orders &amp; deadlines</h2><div id="orders"></div></section>
<section><h2>Option positions and fill basis</h2><div id="positions"></div></section>
<section><h2>Recent alert classifications (private text hidden)</h2>
<div id="messages"></div></section>
<section><h2>Service health and notifications</h2><div id="alerts"></div></section>
<script src="/static/dashboard.js" defer></script></body></html>"""

JS = r"""'use strict';
const byId = id => document.getElementById(id);
const safe = x => x === null || x === undefined ? 'Unavailable' : String(x);
function table(id, headers, rows) {
  const host = byId(id); host.replaceChildren();
  if (!rows.length) {
    const p=document.createElement('p');p.textContent='No records';host.append(p);return;
  }
  const t=document.createElement('table');t.className='table';
  const h=document.createElement('tr');for (const title of headers) {
    const th=document.createElement('th');th.textContent=title;h.append(th);
  }const head=document.createElement('thead');head.append(h);t.append(head);
  const b=document.createElement('tbody'); for (const row of rows) {
    const tr=document.createElement('tr');for (const cell of row) {
      const td=document.createElement('td');td.textContent=safe(cell);tr.append(td);
    }b.append(tr);
  }t.append(b);host.append(t);
}
function card(label,value) {const e=document.createElement('div');e.className='card';
  const l=document.createElement('small');l.textContent=label;
  const v=document.createElement('strong');v.textContent=safe(value);e.append(l,v);return e;}
async function refresh() {try {
const result = await fetch('/api/status',{cache:'no-store'});
if (!result.ok) throw new Error('Unavailable'); const x=await result.json();
if (x.mode!=='PAPER_ONLY'||x.real_broker_writes_enabled!==false) throw new Error('Unsafe status');
const c=byId('cards');c.replaceChildren(
card('Realized gross (sim USD)',x.realized_gross_usd),
card('Realized net (sim USD)',x.realized_net_usd),
card('Unrealized gross (sim USD)',x.unrealized_gross_usd),
card('Completed trades',x.completed_positions),
card('Win rate',x.win_rate),card('Closed-fill drawdown',x.realized_max_drawdown_usd),
card('Emergency stop',x.emergency_stop?'ON':'OFF'),card('Telegram',x.telegram.status));
table('orders',['Order','Side','State','Limit','Open qty','Seconds left'],
x.pending_orders.map(o=>[o.logical_order_id,o.side,o.state,o.limit_price,
o.remaining_contracts,o.countdown_seconds]));
table('positions',['Contract','Swift avg','Fill avg','Open','Entry limit',
'Realized gross','Unrealized gross','Mark'],
x.positions.map(p=>[p.contract,p.swift_reported_average,p.actual_average_fill,p.open_contracts,
p.source_limit,p.realized_gross_usd,p.unrealized_gross_usd,p.mark_status]));
table('messages',['Message','Intent','Status','Reason','Received'],
x.recent_messages.map(m=>[m.message_id,m.intent,m.status,m.reason,m.received_at_utc]));
table('alerts',['Category','Code','Severity','UTC'],
[...x.notifications.map(n=>[n.category,n.code,n.severity,n.created_at_utc]),
...x.alerts.map(a=>['PAPER_ORDER',a.code,'ERROR',a.recorded_at_utc]),
...(x.telegram.status==='DEGRADED'||x.telegram.status==='DISCONNECTED' ?
[['TELEGRAM_SERVICE','TELEGRAM_'+x.telegram.status,'ERROR',x.as_of_utc]] : [])]);
byId('status').textContent='Updated '+x.as_of_utc+' · Fees: '+
safe(x.costs_assumed.fee_per_contract_side_usd)+
' · All figures simulated; missing quotes are unavailable.';
} catch (_) {byId('status').textContent='Local status unavailable. No data has been inferred.';}}
refresh();setInterval(refresh,15000);"""


def build_handler(db_path: Path, *, costs: CostModel | None = None) -> type[BaseHTTPRequestHandler]:
    """Return a handler without an OS socket; ideal for unit tests."""
    analytics = PaperAnalytics(db_path, costs=costs)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SwiftPaperConsole/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            # No URL query, private content or headers in logs.
            return

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; "
                             "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            self._send(405, b"Method not allowed", "text/plain")

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            # Restrict DNS rebinding or proxy attempts to recognized local hosts.
            host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            if host not in {"127.0.0.1", "localhost", "[::1]"}:
                self._send(403, b"Forbidden", "text/plain")
                return
            if urlsplit(self.path).query:
                self._send(400, b"Query parameters disabled", "text/plain")
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
            elif path == "/static/dashboard.js":
                self._send(200, JS.encode(), "application/javascript; charset=utf-8")
            elif path == "/api/status":
                try:
                    payload = analytics.snapshot(as_of=datetime.now(UTC))
                    body = json.dumps(payload, sort_keys=True).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except (OSError, ValueError, sqlite3.DatabaseError):
                    self._send(503, b'{"status":"UNAVAILABLE"}', "application/json")
            else:
                self._send(404, b"Not found", "text/plain")

    import sqlite3  # local for no other platform-specific dashboard imports
    return Handler


def serve_local(db_path: Path, *, host: str = "127.0.0.1", port: int = 8765,
                costs: CostModel | None = None) -> None:
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise ValueError("dashboard must bind to 127.0.0.1 with a valid port")
    with ThreadingHTTPServer((host, port), build_handler(db_path, costs=costs)) as server:
        server.serve_forever(poll_interval=0.25)
