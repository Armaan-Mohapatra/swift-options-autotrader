# Windows recovery and rollback (Phase 6)

**Stop the scheduled Swift-only task first.** Never kill, uninstall, change permissions or relocate Phoenix/Kalshi or unrelated trading processes.

## Code rollback without changing orders or data

1. Run `manage.ps1 -Action Stop -Confirm:$false` and confirm task is no longer running.
2. Run `manage.ps1 -Action Backup -Confirm:$false` and verify the new `.sqlite3` + `.sha256.json` metadata under `backups/`.
3. Run `manage.ps1 -Action Rollback -Confirm:$false`. It chooses the previous 40-character verified release SHA already installed; it does **not** download, rewrite or downgrade a database migration.
4. Run `manage.ps1 -Action Health`. If the older code cannot read the newer SQLite schema, **do not** run it. Restore a suitable previously verified backup only after separate review, or switch back to the latest code, leaving data untouched.
5. Restart with `manage.ps1 -Action Start -Confirm:$false` only after checks pass.

## Database recovery (deliberately manual)

1. Stop the Swift task. Ensure no other application has opened its `data/swift.sqlite3`.
2. Back up the current database via the SQLite online backup command. The restore function also creates a pre-restore backup if there is an existing database.
3. Inspect the selected managed `backups/swift-*.sqlite3` and matching SHA-256 JSON. Never restore files from arbitrary paths, shares or downloads.
4. If `-wal` or `-shm` sidecar files remain, **halt**. Such files may contain committed data from a prior crash. Investigate open handles, checkpoint safely via an approved SQLite utility, and preserve originals. The restore command refuses to silently discard them.
5. Run `manage.ps1 -Action Restore -BackupFile "<FULL_MANAGED_BACKUP_PATH>" -Confirm:$false`. The Python restore checks the SHA-256, SQLite `quick_check`, managed path and exclusive service lock, creates a separate pre-restore snapshot, and atomically replaces the stopped DB.
6. Validate health, order/fill/revision history and the emergency-stop setting before restarting. Restoring an older database can lose newer messages or fills and MUST NOT be followed by automatic live trading.

## Unexpected shutdown or incomplete order state

The supervisor's OS lock is released automatically on a crash, and Task Scheduler can restart the supervisor. SQLite remains in WAL mode. Phase 3's order engine uses durable logical IDs/idempotency and broker journal reconciliation; **the Phase 6 supervisor does not automatically execute paper orders or recover real broker orders.** Review pending/open/unknown states before any replay or external integration. An unfilled or canceled order must not be assumed to have executed.

## Uninstall safely

`manage.ps1 -Action Uninstall -Confirm:$false` unregisters only the Swift task and preserves the entire managed app directory, including data, secrets, backups and logs. Permanent destruction requires BOTH `-PurgeData -IUnderstandDataLoss` plus explicit operator confirmation and a prior offline backup. Do not combine these switches in routine maintenance or automated scripts.

No production installation or real-world rollback was tested during GitHub development. Windows-only recovery validation, complete multi-phase CI and Phase 7 remain required gates.
