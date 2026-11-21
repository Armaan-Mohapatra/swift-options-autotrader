# Swift Windows PAPER deployment package — Phase 6

All scripts are authored for Windows PowerShell 5.1 or newer and run as the ordinary logged-in Windows user. They **have not been installed on the owner's computer**. By default, `setup.ps1` previews all operations and changes nothing. The only real deployment entrypoint requires `-Apply` AND a reviewed full Git commit SHA. An isolated `-NoSchedule` option supports Windows CI without registering tasks.

| Script | Role |
|---|---|
| `setup.ps1` | Dry-run-first isolated installer, locked dependency install, SQLite migration, Task Scheduler logon task |
| `manage.ps1` | Start, stop, restart, status, health, logs, backup, restore, code rollback, protected uninstall |
| `service.ps1` | Per-user Task Scheduler entry, paper-only environment and DPAPI secret decrypt |
| `secrets.ps1` | Manual local DPAPI API hash/token entry and authorized Telegram `.session` import |
| `healthcheck.ps1` | Safe metadata-only health query |
| `common.ps1` | Strict path guard, manifest, ACL, config and Python helpers |

Read [the beginner Windows runbook](../../docs/WINDOWS_RUNBOOK.md), [operations](../../docs/OPERATIONS.md), and [rollback](../../docs/ROLLBACK.md) before using Codex for a later installation. The package does **not** enable a live brokerage, automatically connect to private accounts or automatically execute paper trades. No cloud hosting costs. GitHub Actions status remains an independent required acceptance gate.
