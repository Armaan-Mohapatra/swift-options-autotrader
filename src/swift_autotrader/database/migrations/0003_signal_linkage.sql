-- Phase 2: parser audit decisions, immutable original signal identity, metadata.
-- Phase 3 will be separately responsible for actual order/broker transitions.
CREATE TABLE IF NOT EXISTS signal_metadata (
    signal_id TEXT PRIMARY KEY REFERENCES option_signals(signal_id),
    channel_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL CHECK(source_message_id > 0),
    category TEXT NOT NULL,
    first_revision_id INTEGER NOT NULL UNIQUE REFERENCES telegram_message_revisions(revision_id),
    UNIQUE(channel_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS signal_message_links (
    channel_id TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    signal_id TEXT NOT NULL REFERENCES option_signals(signal_id),
    source_revision_id INTEGER NOT NULL REFERENCES telegram_message_revisions(revision_id),
    PRIMARY KEY(channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_metadata_contract ON signal_metadata(channel_id, source_message_id);
CREATE INDEX IF NOT EXISTS idx_signal_links_signal ON signal_message_links(signal_id);
CREATE TABLE IF NOT EXISTS signal_parser_decisions (
    revision_id INTEGER PRIMARY KEY REFERENCES telegram_message_revisions(revision_id),
    channel_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    intent TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    signal_id TEXT REFERENCES option_signals(signal_id),
    processed_at_utc TEXT NOT NULL,
    CHECK(status IN ('RECORDED','IGNORED','REVIEW_REQUIRED','SKIPPED'))
);
CREATE TABLE IF NOT EXISTS signal_average_history (
    revision_id INTEGER PRIMARY KEY REFERENCES telegram_message_revisions(revision_id),
    signal_id TEXT NOT NULL REFERENCES option_signals(signal_id),
    average TEXT NOT NULL,
    effective_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_swift_avg_latest ON signal_average_history(signal_id, effective_at_utc);
CREATE TABLE IF NOT EXISTS signal_close_requests (
    signal_id TEXT PRIMARY KEY REFERENCES option_signals(signal_id),
    first_revision_id INTEGER NOT NULL UNIQUE REFERENCES telegram_message_revisions(revision_id),
    requested_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_review_queue (
    revision_id INTEGER PRIMARY KEY REFERENCES telegram_message_revisions(revision_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','RESOLVED'))
);
