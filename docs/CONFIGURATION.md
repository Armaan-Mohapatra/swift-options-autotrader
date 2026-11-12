# Configuration reference — Phase 0

**Loading:** pass environment variables to the CLI; `.env.example` is an example only, never automatically read. Invalid configuration returns a redacted JSON error and exit code `2`. The CLI never prints environment variable values or paths in error output.

| Variable | Default | Validation / meaning |
| --- | --- | --- |
| `PAPER_ONLY` | `true` | **Must** be `true` in Phase 0, even if other variables are modified. |
| `LIVE_ENABLED` | `false` | **Must** be `false`; true fails closed. |
| `ALLOW_BROKER_WRITES` | `false` | **Must** be `false`; real broker adapter independently denies all write calls. |
| `BROKER_BACKEND` | `paper` | Only `paper` is allowed; the Phase 0 paper broker itself has no order implementation. |
| `TELEGRAM_ENABLED` | `false` | Must be false; listener/login are future-phase work. |
| `DB_PATH` | `data/swift_autotrader.sqlite3` | Dedicated local SQLite file. Directory is created **only** by `migrate`. Never point at another project's database. |
| `MAX_CONTRACTS_PER_SIGNAL` | `1` | Positive integer 1–100; schema placeholder, not yet enforced by order engine. |
| `MAX_PREMIUM_PER_SIGNAL_USD` | `0` | Finite nonnegative decimal; 0 means **no premium-risk budget authorized**. Not yet an execution gate. |
| `ENTRY_TIMEOUT_SECONDS` | `3600` | Exactly 3,600; other values rejected. Operates from confirmed broker acceptance time. |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL. Structured logging not implemented in Phase 0. |

Future-only local configuration names (not used by Phase 0): `SWIFT_TELEGRAM_CHANNEL_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ROBINHOOD_MCP_URL`. Put real values exclusively in protected Windows storage in a later separately authorized installation. Do not store usernames, passwords, tokens, account numbers, channel IDs, refresh tokens, or session files in GitHub Actions variables, source files, issue bodies, logs, or screenshots.

**Validation examples:** `LIVE_ENABLED=true` or `ALLOW_BROKER_WRITES=true` produces nonzero exit; `PAPER_ONLY=false` similarly fails. `dry-run` does not persist anything. `migrate` is deliberately a separate command that can write to the configured dedicated SQLite path.


## Phase 1 locally authorized Telegram settings

The Phase 0-only prohibition on `TELEGRAM_ENABLED=true` is superseded by Phase 1: it remains **false by default**, and can be enabled only with a positive local `TELEGRAM_API_ID`, 32-hex `TELEGRAM_API_HASH`, signed `SWIFT_TELEGRAM_CHANNEL_ID`, and an absolute **existing** pre-authorized `.session` path outside the checkout. No local session means no connection. Never put real values in source or CI.

`TELEGRAM_QUEUE_CAPACITY=512` (1–4096), `TELEGRAM_MAX_LIVE_AGE_SECONDS=120` (0–600), and `TELEGRAM_RECONNECT_ATTEMPTS=5` (1–20). `telegram-health` is read-only and does not require login. `telegram-listen` requires local opt-in, migration and session. Broker safety flags are unchanged. See [Telegram ingestion](TELEGRAM_INGESTION.md).
