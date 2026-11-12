# Telegram read-only ingestion — Phase 1

**Status:** Code and synthetic tests, no owner Windows installation, real user login, private channel connection or paid messages. This guide does not authorize live trades or unattended authentication.

## Library and privacy

- Telethon==1.45.0 (MIT, released 2026-09-10) selected instead of Hydrogram 0.2.0 (LGPLv3, 2024-06-30). See https://pypi.org/project/Telethon/ and https://docs.telethon.dev/en/stable/modules/client.html.
- The operator must already be an authorized subscriber to the designated Swift channel. No private content from other chats is ingested, stored, logged or downloaded.
- The adapter uses an **existing** authorized local user `.session` file. It never calls `start()`, `sign_in()`, `send_message()` or history-fetch operations. It calls `connect()`, `is_user_authorized()`, and resolves exactly the selected peer. If the private peer is not in session cache, it may inspect read-only dialog metadata to locate that peer. Nothing from other dialogs is retained.
- NewMessage and MessageEdited handlers are restricted to the verified channel; the application also independently checks signed chat ID before enqueue.
- The client does not automatically catch up history. No automatic backfill, no old-trade replay and no contact with Robinhood.

## What is stored

- `raw_events`: text body and original receive/sent time (local SQLite only, not GitHub).
- `telegram_message_revisions`: unique version fingerprint, message identity, edit kind/timestamp, author, reply-to ID, arrival time, origin, and stale eligibility reason.
- `telegram_messages`: most recent materialized version for each (channel, message ID), with older out-of-order edits retained in the revision log.
- `telegram_runtime_state`: monotonic highest-seen message ID, last event/connection, status and bounded queue overflow count.
- Migrations are additive and checksum-verified: `0001_initial.sql`, `0002_telegram_ingest.sql`. WAL mode is enabled.

## Recovery and stale-signal rules

New messages are only flagged `eligible_for_future_processing` if first-seen, from the exact configured channel, strictly above the persisted highest ID, sent no earlier than the latest verified connection epoch, not future-dated and received within the configured age bound (default 120 seconds). Edits, duplicate deliveries, delayed historical messages and recovered gaps are never fresh entries. **Phase 1 does not consume this flag, parse trades or place orders.**

Each distinct edit is kept as a revision, and an older edit cannot overwrite a newer materialized snapshot. The bounded queue (default 512 events) writes each accepted event to durable SQLite before marking work complete. A storage-worker failure aborts an otherwise idle Telegram connection and marks the process degraded. Connection health changes to CONNECTED only after verified user/channel authorization. JSON operational logs are emitted with safe event codes only. On overflow the listener marks itself DEGRADED and disconnects rather than silently dropping data. On SQLite failure it stops; any missed Telegram events need manual gap review. A bounded retry policy creates a new connection epoch after disconnection; it does not automatically repost, reconstruct or trade missed alerts.

## Manual local configuration (future Windows authorization only)

Never enter these values in GitHub Actions, GitHub issues, source, examples, ChatGPT or screenshots. First, after an explicit local installation decision, authorize the Telegram user manually through documented Telethon sign-in on the actual Windows host. Confirm legitimate membership and signed channel ID. Store `.session` outside the checkout in a Windows ACL-protected location.

Required local environment: `PAPER_ONLY=true`; `LIVE_ENABLED=false`; `ALLOW_BROKER_WRITES=false`; `BROKER_BACKEND=paper`; `TELEGRAM_ENABLED=true`; `SWIFT_TELEGRAM_CHANNEL_ID=<verified signed -100... peer>`; `TELEGRAM_API_ID=<local ID>`; `TELEGRAM_API_HASH=<local 32-character hash>`; `TELEGRAM_SESSION_PATH=<absolute protected .session>`; `DB_PATH=<dedicated local SQLite path>`. The example `.env.example` has placeholders only.

Only after separate authorization, operator may run:

```powershell
python -m pip install -e ".[telegram]"
python -m swift_autotrader config-check
python -m swift_autotrader migrate
python -m swift_autotrader telegram-health
python -m swift_autotrader telegram-listen
```

Ctrl+C stops the foreground listener. `telegram-health` reads stored metadata only and does not authenticate or create the database. This is not an installer, scheduled task, persistent Windows supervisor or fully verified forward-data feed.

## Known limits

Real login, paid channel membership/peer cache, Telegram interruptions, Windows permissions, rate limits, message-delivery latency and fresh-message capture have **not** been tested in GitHub. No automatic historical gap repair is offered because an old alert must never turn into a new option order. Sessions can expire; record failures as local auth/health faults, not silently as success. No at-rest encryption is supplied by SQLite itself; rely on approved protected Windows storage, ACL and backup controls in later deployment.
