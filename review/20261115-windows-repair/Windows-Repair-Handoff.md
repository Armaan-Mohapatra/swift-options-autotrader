# Swift Windows installation repair — 2026-11-15

## Scope and evidence

Base: 837aec03e42e8d424fb0435f4b32166e1940e111
Six-file tested source checkpoint: c0fe3e9c9f7c5f28c9e294cf5dd3674d678ff2e8
Prior Windows acceptance: 628 passed, 1 failed, 1 skipped; stopped paper-only. The original installed-directory Telegram failure was reproduced independently with synthetic credentials; it raised "Session path cannot be inside the checkout".

Repairs:
- Shared managed-secret path validation recognizes only the declared paper-only installation's exact credential slot, blocks Git checkouts and redirected paths, and retains the unmanaged working-directory guard.
- Telegram settings and enrollment use the shared validation; POSIX permission bits are asserted only on POSIX. Windows ACL protection remains the existing deployment script's responsibility and must be verified natively.
- Robinhood DPAPI local authentication and paper service pass the managed installation root, avoiding the equivalent checkout-path error.
- Nine regression tests include constructing PaperSupervisor from the installed directory, OAuth path validation, wrong slots, missing/unsafe manifests, Git/worktree paths and symlink rejection.

Independent result: Python 3.13.5 on Linux, reconstructed source base plus reviewed changes, 639 passed, 0 failed, 0 skipped, 1 warning (deliberate duplicate archive fixture), 16.80s. External network/broker/Telegram operations were not exercised; no order was submitted. All six published content hashes were checked against the independently tested local files. This is not a native Windows or production live acceptance result.

| File | Git blob SHA |
|---|---|
| src/swift_autotrader/config.py | 0462da34f746509dc878fa9561e5f891d2b8b074 |
| src/swift_autotrader/telegram/enrollment.py | 27b1a21c1d39bbb05d92a156fa1678158fcbad7a |
| src/swift_autotrader/broker/mcp_local_auth.py | b4ab35075b3400e6b6143946d3cacbd80e85c707 |
| src/swift_autotrader/paper_service.py | 2aec1fdbec70b009190d52b0c6e9e28f5c2b0b33 |
| tests/unit/test_telegram_enrollment.py | 46b02a6539a256f48ba71a01469f17eaa60adb79 |
| tests/unit/test_managed_credential_paths.py | 3cb41fda34d96dddd9ecd75240d07fc48da6af42 |

## Native acceptance instructions

Use the exact tested source checkpoint in a separate clean checkout on E:. Do not edit the repair or reinterpret a paper success as live readiness. Use the existing managed install E:\SwiftAutoTrader\install-837aec03 only after verifying its manifest and ownership, that the candidate task is disabled/stopped, and that no associated worker processes are running. Preserve all databases, private sessions, user-scoped DPAPI files, configuration, old releases and task definitions; never copy secrets into source. Verify C: free space (recommend 15–20 GB) and separately E: capacity; locate temporary directories, uv cache, test base temp and logs on E: using process-local settings. Do not clean up unrelated files.

Run the native suite in the clean checkout with locked dependencies, explicit paper-only configuration and isolated E: test data. Report all failures and skips. For the known POSIX-only mode skip, verify NTFS ACLs separately; do not weaken ACLs or expose secret material.

Use the existing deployment/windows/setup.ps1 with the clean SourceRoot, verified InstallRoot, exact ExpectedCommit, -Apply and -NoSchedule (existing scheduled-task collision would otherwise abort). Preview first. Preserve the old active-release fingerprint and perform a verified SQLite backup before migration. Do not modify a colliding or unrelated task. Confirm the new release and its local interpreter, run config-check from the installed working directory with the supported Set-SwiftEnvironment, verify the Telegram session and OAuth storage resolve without revealing paths or secret contents in published artifacts, and verify database integrity. Check owner and restrictive ACLs on secrets including existing explicit grants; if ACLs are unsafe, stop and report. Leave deployment marker and configuration PAPER_ONLY with broker writes disabled.

Validate that the existing task's action, principal and install root match the intended managed install; only then use the supported task management route to enable/start the known task. Confirm sustained heartbeat, actual worker process identity, dashboard on localhost:8765 and read-only connection/health. Test a controlled restart and absence of duplicate processes. Validate that monitoring survives closing the launching terminal; report actual logon/lid/sleep constraints, not an invented 24/7 guarantee. If a step fails, stop only the affected candidate, preserve logs and revisit the previous release pointer if appropriate; do not automatically delete files or downgrade the database.

## Production boundary

The repaired source still has no registered production live dispatcher command. Existing checks are offline/diagnostic and do not certify a real broker lifecycle or authorization. No supported unattended real-money activation is included. Keep paper/read-only and real brokerage writes disabled. A fully integrated production path, genuine standalone authorized acceptance and observed fills/reconciliation remain separate blockers. Return sanitized diagnostics and a precise pass/fail gate report, without claiming that fixing startup enables real-money trading.
