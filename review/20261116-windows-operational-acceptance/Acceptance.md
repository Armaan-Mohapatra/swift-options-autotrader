# Swift AutoTrader Windows Acceptance

## Release

- Installed release and source commit: `fb3d68811900adaffa262e0ac7b7e6f092c2e69f`.
- Installation root: `E:\SwiftAutoTrader\install-837aec03`.
- Source branch: `review/windows-reliability-acceptance-20261116`.
- Installation used the supported Windows installer with `-NoSchedule`; no Git operations were performed in the managed installation.
- Existing releases, the Telegram session, DPAPI Telegram/OAuth/account-binding records, configuration, logs and databases were retained. Credential files remain in the restricted managed `secrets` directory.

## Validation

- Native Windows Python 3.11 regression suite against the reviewed source: **733 passed, 2 platform skips, 0 failures**. The skips require POSIX-only file modes or symlink privileges unavailable in this environment. One archive-fixture warning reported a deliberately duplicated ZIP member.
- Targeted Windows service, supervisor and full-service integration tests: **50 passed**.
- A real child process wrote more than the pipe buffer, was drained, and exited. Worker JSONL logs rotated at 2 MiB with three backups; emitted message arguments were redacted.
- A real isolated Windows service worker exited with code 7, restarted after the configured 2-second backoff with a new PID, and was reaped on shutdown.
- Installed release smoke, configuration and offline startup checks passed. Configuration reports `PAPER_ONLY`, one contract per signal, 10% entry slippage, a $750 daily realized-loss threshold, optional kill switch disabled, and broker writes disabled.

## Read-only account checks

- Standalone Robinhood MCP diagnostics succeeded with `READ_ONLY_SCHEMA_OK`; required tools and schemas were available.
- The Windows DPAPI account binding was readable and matched the unique option-eligible agentic account returned by the provider. No order endpoint was called.
- The scheduled Telegram worker authenticated, logged `source_verified`, and the dashboard reported Telegram `CONNECTED`. No Telegram message bodies or account identifiers are included in this evidence.

## Windows operation

- `SwiftAutoTrader-Paper-Local` is enabled and running under the interactive logon principal. It has an at-logon trigger, three scheduler restart attempts at two-minute intervals, ignores duplicate instances, and does not stop when AC power is removed. A fresh heartbeat reported `PAPER_ONLY`, dashboard and Telegram workers alive, and live startup not ready.
- A supported managed restart stopped both old child processes, relaunched the task, and returned with exactly two fresh worker processes. Monitoring remained active after the launching PowerShell command returned.
- `http://127.0.0.1:8765/` returned HTTP 200 and `/api/status` was accessible locally. The API reports configured paper mode, writes disabled, and no standalone broker execution evidence.
- Both `swift.sqlite3` and the separate `live.sqlite3` pass SQLite integrity checks. Each has the expected 26 tables; the production journal contains zero imported alerts, signals, orders or fills. Paper dispatch is disabled in the running monitor.
- Logs are under `E:\SwiftAutoTrader\install-837aec03\logs\`: `supervisor.log`, `worker-dashboard.jsonl`, and `worker-telegram.jsonl`. Health is recorded in `state\health.json`. A verified pre-install SQLite backup is retained under `backups\`.

## Storage and power

- First measured free space this run: C: **495,480,832 bytes (472.53 MiB)**; E: **534,104,584,192 bytes (497.42 GiB)**.
- Latest measured free space: C: **0 bytes reported**; E: **533,210,894,336 bytes (496.59 GiB)**. No C: files were deliberately deleted or relocated during this installation. The cause of the C: decrease was not established. Do not treat the system as having adequate free space.
- Sleep timeout is set to Never on AC and battery. The battery provider reported 100% charge with status `Discharging`; continuous operation therefore requires AC power. No lid-close action was exposed in the queried power plan. The interactive logon task requires the designated Windows user to log in after a reboot for DPAPI access.

## Production boundary

- Real brokerage writes remain disabled in installation configuration and runtime. The paper task does not start the live dispatcher; `live_startup_ready` is false.
- Historical/fake-broker integration tests do not constitute Robinhood order acceptance. No real order was submitted during this installation. Standalone live-order acceptance and provider-required order approval remain unverified.
- Unattended live trading is **not authorized or operational**. A supervised real-money acceptance order requires explicit transaction-specific owner authorization, and production activation requires a separate final decision after that test.
- The only active mode is continuous paper/read-only monitoring. The 30 GiB C: recovery objective is not met; owner review of safe Windows cleanup and already-deleted recycle-bin contents is still required before relying on this laptop for uninterrupted operation.
