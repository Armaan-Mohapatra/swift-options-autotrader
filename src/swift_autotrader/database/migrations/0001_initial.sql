-- Phase 0 only: schema foundation; ingestion, matching and execution not yet implemented.
CREATE TABLE IF NOT EXISTS raw_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    telegram_message_id TEXT NOT NULL,
    edit_version INTEGER NOT NULL DEFAULT 0 CHECK (edit_version >= 0),
    received_at_utc TEXT NOT NULL,
    sent_at_utc TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE(channel_id, telegram_message_id, edit_version)
);
CREATE TABLE IF NOT EXISTS option_signals (
    signal_id TEXT PRIMARY KEY,
    source_event_id INTEGER REFERENCES raw_events(event_id),
    underlying TEXT NOT NULL,
    option_right TEXT NOT NULL CHECK (option_right IN ('CALL', 'PUT')),
    strike TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    original_limit TEXT NOT NULL,
    swift_reported_avg TEXT,
    confirmed_avg_fill_price TEXT,
    state TEXT NOT NULL DEFAULT 'SEEN',
    created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_intents (
    logical_order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES option_signals(signal_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL CHECK(side IN ('BUY_TO_OPEN', 'SELL_TO_CLOSE')),
    requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK(filled_quantity >= 0),
    limit_price TEXT NOT NULL,
    state TEXT NOT NULL,
    accepted_at_utc TEXT,
    expires_at_utc TEXT,
    broker_order_id TEXT UNIQUE,
    CHECK(filled_quantity <= requested_quantity)
);
CREATE TABLE IF NOT EXISTS order_fills (
    fill_id TEXT PRIMARY KEY,
    logical_order_id TEXT NOT NULL REFERENCES order_intents(logical_order_id),
    filled_quantity INTEGER NOT NULL CHECK(filled_quantity > 0),
    filled_price TEXT NOT NULL,
    filled_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_state_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_order_id TEXT NOT NULL REFERENCES order_intents(logical_order_id),
    state TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS telegram_checkpoints (
    channel_id TEXT PRIMARY KEY,
    last_processed_message_id TEXT,
    updated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(channel_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_order_intents_state ON order_intents(state);
CREATE INDEX IF NOT EXISTS idx_order_fills_order ON order_fills(logical_order_id);
