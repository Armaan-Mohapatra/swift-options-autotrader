-- Phase 5: optional evidence-linked quotes, replay provenance and local alert outbox.
-- Historical research MUST use a distinct temporary SQLite database.
CREATE TABLE paper_market_observations (
 quote_id TEXT PRIMARY KEY,
 underlying TEXT NOT NULL,
 option_right TEXT NOT NULL CHECK(option_right IN ('CALL','PUT')),
 strike TEXT NOT NULL,
 expiration_date TEXT NOT NULL,
 bid TEXT NOT NULL,
 ask TEXT NOT NULL,
 bid_size INTEGER NOT NULL CHECK(bid_size >= 0),
 ask_size INTEGER NOT NULL CHECK(ask_size >= 0),
 observed_at_utc TEXT NOT NULL,
 provenance TEXT NOT NULL CHECK(provenance IN ('SYNTHETIC','EXTERNAL_UNVERIFIED','EXTERNAL_VERIFIED')),
 source_reference TEXT
);
CREATE INDEX idx_paper_market_contract_time ON paper_market_observations(
 underlying,option_right,strike,expiration_date,observed_at_utc);
CREATE TABLE local_notifications (
 notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
 fingerprint TEXT NOT NULL UNIQUE,
 category TEXT NOT NULL,
 severity TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','ERROR','CRITICAL')),
 source_code TEXT NOT NULL,
 created_at_utc TEXT NOT NULL,
 acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1))
);
CREATE INDEX idx_local_notifications_unack ON local_notifications(acknowledged,created_at_utc);
CREATE TABLE replay_runs (
 run_id TEXT PRIMARY KEY,
 input_sha256 TEXT NOT NULL,
 source_type TEXT NOT NULL CHECK(source_type IN ('SYNTHETIC','HISTORICAL_UNVERIFIED')),
 assumption TEXT NOT NULL,
 started_at_utc TEXT NOT NULL,
 end_at_utc TEXT NOT NULL,
 evidence_quality TEXT NOT NULL,
 CHECK(end_at_utc>=started_at_utc)
);
