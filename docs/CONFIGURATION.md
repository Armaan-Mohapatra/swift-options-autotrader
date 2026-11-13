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

## Phase 3 PAPER engine controls (environment, synthetic only)

These additional variables are read by `RiskCeilings.from_environment()` or `ExitPolicy.from_environment()` when constructing the paper engine, not by the Phase 0 `Settings.public_summary()` schema:

| Variable | Default | Policy |
| --- | --- | --- |
| `MAX_CONTRACTS_PER_SIGNAL` | `1` | Quantity; 1–100. Applies to entry size. |
| `MAX_PREMIUM_PER_SIGNAL_USD` | `0` | Mandatory max order premium including the standard 100-share options multiplier; **zero blocks entries**. |
| `PAPER_MAX_TOTAL_EXPOSURE_USD` | `0` | Aggregate reserved notional ceiling; **zero blocks entries**. |
| `PAPER_MAX_OPEN_SIGNALS` | `1` | Maximum concurrently active signals, 1–1000. |
| `PAPER_ENTRY_MAX_AGE_SECONDS` | `120` | Accept only fresh persisted entry signals; 0–600. |
| `PAPER_EXIT_QUOTE_MAX_AGE_SECONDS` | `30` | Require matching, positive, tick-valid and recent BID for sell-to-close LIMIT; 0–300. |

Further checks require exact contract identity, verified test instrument, current market session permission, options permission, tick increment and modeled buying power. There is **no implicit trusted feed**: the standalone CLI supplies an empty instrument provider, refuses `paper-cycle --confirm-simulation` and does not turn synthetic option-chain guesses into "verified" live instruments. Build and test paper cycles via an explicitly supplied synthetic `PaperPreflight` in isolated fixtures. Options-only, no quote price chasing, no new average repricing. `paper-health` and control commands require an explicitly migrated local SQLite database. `paper-emergency-stop` persists a stop and requests cancellation of resting paper entries; manual API approval is required to reset the emergency flag.

All original trading safety flags remain enforced; real broker writes are impossible. No unattended scheduled execution or Windows deployment exists in Phase 3.

## Phase 4 local-only MCP diagnostics

No Robinhood credentials are ever configured in GitHub or standard env files.
`mcp-diagnostics` prints read-only/offline status by default. On the separately
authorized Windows host only, `--connect` plus
`ROBINHOOD_MCP_INTERACTIVE=true` enables a human PKCE OAuth flow using DPAPI
storage outside the checkout. This flow has not been field validated; keep all
real brokerage write flags false. See `ROBINHOOD_MCP_PHASE4.md`.


## Phase 7 release-safety reminder

`PAPER_ONLY=true`, `LIVE_ENABLED=false` and `ALLOW_BROKER_WRITES=false` are enforced startup invariants. No environment flag, installer step, mock OAuth session or GitHub release candidate can authorize real broker writes; the adapter's live `submit_order`, `get_order` and `cancel_order` methods stay deny-only. Entry risk caps default to $0 until separately approved explicit synthetic preflight. The Windows supervisor has no verified instrument catalog or automatic paper-order dispatch. Any Telegram session, API hash and optional status bot token must be enrolled **locally and manually** under the approved Windows user's DPAPI/NTFS-protected directories. Never commit `.env`, `.session`, database, bot token or paid-channel text. See `docs/PHASE_REPORTS/PHASE_7.md` for unverified conditions.


## Phase 8 optional PAPER worker (default OFF)

In the privately managed Windows settings.json, `paper_dispatch_enabled=false` and `paper_data_source=none` are default. To enable a separately authorized read-only verification worker, both must be explicitly set along with positive, bounded PAPER-only `paper_max_contracts`, `paper_max_premium_usd`, `paper_max_exposure_usd`, and `paper_max_open_signals`, an authorized Telegram session, existing DPAPI-protected MCP OAuth credentials, and official quote/option fields passing fail-closed checks. Required runtime variables include PAPER_DISPATCH_ENABLED, PAPER_MARKET_DATA_SOURCE, with live writes always hard-disabled. Absence of a full optional SDK lock or a supported MCP data schema means the worker must remain OFF. Never place token values in settings.json.
