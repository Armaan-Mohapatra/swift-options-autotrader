# Windows runbook — Phase 0 planning only

**Do not install or run as a service yet.** The intended host is the user's existing always-on Windows machine. No system access, deployment, existing process or production runtime change is authorized during Phase 0.

## Future deployment process (Phase 6)

1. From a verified reviewed private GitHub release, clone into a **dedicated** directory not shared with any other trading system.
2. Verify GitHub commit, release checksum, dependency lock and signatures where available.
3. Use dedicated Python virtual environment and separate data, logs, backup and Windows-protected credential directories.
4. Run all PowerShell scripts in `-WhatIf` / dry-run mode before explicit operator approval.
5. Initialize SQLite WAL migrations on a **new dedicated** database and test online backup/restore via the SQLite backup API; never casually copy a live WAL SQLite file.
6. Authorize the Telegram user MTProto session locally, with authorized Swift channel ID; enroll Robinhood agent connection locally under an authorized dedicated account only after the later protocol gates.
7. Start with `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`; do not install a live listener before Phase 1 validation, nor an engine before Phase 3 validation.
8. Configure single-instance lock, controlled shutdown, supervisor restart policy, checkpoint recovery, log retention/redaction, alerts, health and manual emergency stop.
9. Simulate restarts, network ambiguity, order reconciliation and rollback. Preserve fills and order history; never automatically resubmit stale orders.

In Phase 0, the only Windows script is `deployment/windows/healthcheck.ps1`, which prints that the runtime is **not installed**. It changes no Windows setting and accesses no credentials.
