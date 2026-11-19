# Windows operations and health monitoring — PAPER ONLY

## Runtime design

The Task Scheduler task `SwiftAutoTrader-Paper-Local` starts at **the designated Windows user's logon**, using a limited-permission interactive token so user-scoped DPAPI secrets can be unlocked. Restart-on-failure is three attempts two minutes apart, with `MultipleInstances=IgnoreNew`. Windows service mode and `SYSTEM`-based startup are intentionally excluded because DPAPI and user MTProto sessions would not be available without redesign. A powered-on machine waiting at the login screen is **not guaranteed** to run the task.

`service.ps1` loads private configuration and unwraps user-local DPAPI secrets in memory; `python -m swift_autotrader.windows_runtime service` enforces paper-only settings and an OS local single-instance lock. It launches **only** the `paper-dashboard` (localhost) and, if separately authorized, `telegram-listen` (read-only). It never starts `paper-cycle`, `parse-replay`, or calls Robinhood write tools. This is deliberate because no trusted real option catalog/preflight is configured yet; a parser replay with empty catalog could irreversibly classify otherwise genuine alerts as reviews. Watchdog loops every five seconds, backed by bounded exponential worker restart and Task Scheduler failure-retry. The supervisor records heartbeat metadata in `state/health.json`, with no tokens or message bodies.

## Controls

| Action | Example | Effect |
|---|---|---|
| Preview | `setup.ps1 -DryRun` | No files, tasks or DB modified |
| Status | `manage.ps1 -Action Status` | Task state and heartbeat metadata |
| Health | `manage.ps1 -Action Health` | Heartbeat plus local dashboard check |
| Start/stop/restart | `manage.ps1 -Action Start/Stop/Restart -Confirm:$false` | Scoped to Swift task, not other processes |
| Logs | `manage.ps1 -Action Logs` | Last 40 supervisor metadata lines |
| Backup | `manage.ps1 -Action Backup -Confirm:$false` | SQLite online backup to managed `backups` directory |
| Rollback | `manage.ps1 -Action Rollback -Confirm:$false` | Repoint active code to prior installed verified SHA, DB unchanged |
| Restore | `manage.ps1 -Action Restore -BackupFile <managed .sqlite3> -Confirm:$false` | Offline, hash-verified SQLite restoration after stopping task |
| Uninstall | `manage.ps1 -Action Uninstall -Confirm:$false` | Remove only this scheduled task; retain all sensitive data |

The dashboard is exclusively `http://127.0.0.1:8765/`. No inbound firewall exception is required or permitted. No remote access, HTTP POST order/control endpoints, cloud hosting, browser automation or live brokerage traffic are created. The notification-outbox scan runs every minute; Telegram status delivery is separately authorized and disabled by default. Failed status sends are logged only as symbolic codes, with no bot tokens or request URLs.

## Data folders and quotas

- `releases/<full-SHA>/`: immutable code + local venv. Rollback preserves old releases; operators must inspect disk consumption before pruning (never automatically prune unknown releases).
- `data/swift.sqlite3` with SQLite WAL. No raw paid message text in GitHub; sensitive messages remain on the approved host.
- `secrets/`: restricted access, user DPAPI token files, authorized Telegram user session. Never back up or paste secrets into GitHub; secure manual recovery for session files is a separate user decision.
- `logs/supervisor.log`: bounded 2 MiB × four files (current + three backups). Other logs can grow; audit them on the host.
- `backups/`: consistent SQLite backup snapshots, 7 retained by default. Exact disk consumption grows with the message database; monitor capacity. Backups can contain sensitive paid message data and must remain local with Windows ACL protection.
- `state/`: OS lock, health heartbeat, stop marker, and at-least-once status-notification fingerprints. All under the separate app root.

No exact CPU, RAM, or total-disk benchmark is claimed until the real host is observed. Five-second poll plus minute alert scan avoids tight busy loops; the Telegram queue remains bounded by existing configuration. No other apps or cloud resources are controlled.

## Troubleshooting table

| Symptom | Safe action | Do not do |
|---|---|---|
| `NOT_STARTED` | Check logon task registration and `Start` manually | Reuse a Phoenix/Kalshi task |
| `STALE` heartbeat | Check `manage Status/Health`, examine task state | Assume everything is running |
| Telegram `REQUIRES_LOCAL_AUTH` | Verify session location/user identity and the source peer | Store password in JSON or Git |
| Repeated worker crash | Inspect symbolic supervisor log; fix preflight/config | Endless immediate restarts |
| `INSTRUMENT_CATALOG_REQUIRED` | Keep entry in review until an official provider is validated | Use a guessed expiration year |
| `UNFILLED_EXIT` | Check paper ledger and correct matching position manually | Pretend partial sell completed |
| SQLite WAL sidecars at restore | Keep task stopped, check for open handles and safely checkpoint after review | Delete WAL or overwrite DB while live |
| `BACKUP_FAILED` | Check permissions/free disk, preserve previous backup | Delete unknown data |
| Unexpected open orders after restart | Read durable journal and reconcile before attempting any action | Mint new idempotency key |

## Privacy and recovery

No plaintext Telegram secrets, Robinhood account numbers, raw paid alert messages or user-identifying source IDs should be emitted by operational logs. User secrets are encrypted using Windows DPAPI **for that user on that computer**; restoring them on another account or machine may fail. Backups are **not automatically encrypted** beyond NTFS/user-level protection; use approved disk encryption/BitLocker where available. No backup file should enter a public cloud or GitHub repository without explicit, appropriate authorization.

## Operational stop gates

Phase 6 is source-development only. The host has **not** been installed or authenticated. Historical paper results are synthetic. Full Windows CI must pass after runner availability is restored, and Stage 7 must assess compatibility/security. Live broker writes remain disabled in every configuration; no user interface here can enable them.
