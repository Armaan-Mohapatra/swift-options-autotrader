-- Phase 3. Additive, durable PAPER-only broker journal and operator controls.
-- Older migrations and signal semantics are never changed.
CREATE TABLE paper_broker_orders (
 logical_order_id TEXT PRIMARY KEY,
 idempotency_key TEXT NOT NULL UNIQUE,
 signal_id TEXT NOT NULL,
 underlying TEXT NOT NULL,
 option_right TEXT NOT NULL CHECK(option_right IN ('CALL','PUT')),
 strike TEXT NOT NULL,
 expiration_date TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('BUY_TO_OPEN','SELL_TO_CLOSE')),
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 limit_price TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ACCEPTED','PARTIALLY_FILLED','FILLED','CANCEL_PENDING','CANCELED','REJECTED')),
 accepted_at_utc TEXT,
 reject_reason TEXT,
 created_at_utc TEXT NOT NULL
);
CREATE TABLE paper_broker_fills (
 fill_id TEXT PRIMARY KEY,
 logical_order_id TEXT NOT NULL REFERENCES paper_broker_orders(logical_order_id),
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 price TEXT NOT NULL,
 filled_at_utc TEXT NOT NULL
);
CREATE INDEX idx_paper_broker_fills_order ON paper_broker_fills(logical_order_id);
CREATE TABLE paper_engine_control (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
 emergency_stop INTEGER NOT NULL DEFAULT 0 CHECK(emergency_stop IN (0,1)),
 updated_at_utc TEXT NOT NULL,
 reason TEXT
);
INSERT INTO paper_engine_control(singleton,paused,emergency_stop,updated_at_utc)
VALUES(1,0,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TABLE paper_engine_decisions (
 signal_id TEXT PRIMARY KEY REFERENCES option_signals(signal_id),
 outcome TEXT NOT NULL CHECK(outcome IN ('INTENT_RECORDED','BLOCKED','CLOSED_NO_FILL')),
 reason TEXT,
 decided_at_utc TEXT NOT NULL
);
CREATE TABLE paper_engine_alerts (
 alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
 signal_id TEXT REFERENCES option_signals(signal_id),
 logical_order_id TEXT REFERENCES order_intents(logical_order_id),
 code TEXT NOT NULL,
 recorded_at_utc TEXT NOT NULL,
 resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN(0,1)),
 UNIQUE(signal_id,logical_order_id,code)
);
CREATE INDEX idx_paper_engine_alerts_open ON paper_engine_alerts(resolved,code);
CREATE TABLE paper_quote_observations (
 quote_id TEXT PRIMARY KEY,
 quote_fingerprint TEXT NOT NULL,
 observed_at_utc TEXT NOT NULL
);
