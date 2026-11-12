-- Phase 1: append-only Telegram change history; never persist credentials or session files.
CREATE TABLE IF NOT EXISTS telegram_message_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id INTEGER NOT NULL UNIQUE REFERENCES raw_events(event_id),
    channel_id TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    event_kind TEXT NOT NULL CHECK(event_kind IN ('new','edit')),
    origin TEXT NOT NULL CHECK(origin IN ('live','recovered')),
    telegram_sent_at_utc TEXT NOT NULL,
    telegram_edited_at_utc TEXT,
    received_at_utc TEXT NOT NULL,
    author_id TEXT,
    reply_to_message_id INTEGER,
    content_sha256 TEXT NOT NULL,
    eligible_for_future_processing INTEGER NOT NULL DEFAULT 0
        CHECK(eligible_for_future_processing IN (0,1)),
    ignored_reason TEXT,
    UNIQUE(channel_id, message_id, revision),
    UNIQUE(channel_id, message_id, content_sha256)
);
CREATE TABLE IF NOT EXISTS telegram_messages (
    channel_id TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    latest_revision_id INTEGER NOT NULL REFERENCES telegram_message_revisions(revision_id),
    latest_effective_at_utc TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    PRIMARY KEY(channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS telegram_runtime_state (
    channel_id TEXT PRIMARY KEY,
    max_observed_message_id INTEGER NOT NULL DEFAULT 0 CHECK(max_observed_message_id >= 0),
    last_received_at_utc TEXT,
    last_connected_at_utc TEXT,
    last_error_code TEXT,
    overflow_count INTEGER NOT NULL DEFAULT 0 CHECK(overflow_count >= 0),
    state TEXT NOT NULL DEFAULT 'INITIALIZED',
    updated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_revisions_time
    ON telegram_message_revisions(channel_id, received_at_utc);
CREATE INDEX IF NOT EXISTS idx_telegram_revisions_eligible
    ON telegram_message_revisions(channel_id, eligible_for_future_processing);
