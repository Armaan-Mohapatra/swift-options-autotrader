# Windows acceptance procedure (PAPER package, no live trading)

Status: prepared 2026-11-21 from the verified scripts in `deployment/windows/`.
Nothing here has been run on the Windows laptop yet. Every step is PAPER-only.
Do **not** run `-Apply` against the production root, register a LIVE task, or
enter any credential until the owner gives a separate, written go-ahead.

## 0. Blockers that only the laptop can clear

| # | Blocker | Why it cannot be done on the Mac |
|---|---|---|
| H1 | Windows 10/11 with Windows PowerShell 5.1 (`powershell.exe`) | Mac has no `powershell.exe`; `pwsh` is not a substitute for 5.1 parsing. |
| H2 | Python 3.11 available through `py.exe -3.11` | Only the Mac interpreter is verified here. |
| H3 | DPAPI-protected local secrets (`secrets.ps1`) | DPAPI is bound to the Windows user profile. |
| H4 | Task Scheduler at-logon behaviour, coexistence with Phoenix/Kalshi | Needs the real host and its existing tasks. |
| H5 | Telegram `.session` import and Swift channel access | Requires the owner's paid membership and a local login. |
| B1 | Robinhood agentic-account OAuth, schema, pin, quotes | Requires the owner's local OAuth and a read-only session. |
| B2 | Any supervised LIVE step | Owner-only, separate authorization, out of scope here. |

## 1. Verify the package on the Mac first (done once, recorded in the release folder)

The release folder holds `swift-autotrader-<sha12>.zip`, a `SHA256SUMS` file and a
manifest. Confirm the hash before copying it to the laptop:

```powershell
Get-FileHash -Algorithm SHA256 .\swift-autotrader-<sha12>.zip
# Expected: the SHA-256 printed in SHA256SUMS for that zip.
```

Expected: the hash matches exactly. Any mismatch is a stop condition.

## 2. Dry run (non-mutating)

```powershell
cd <extracted-package>\deployment\windows
.\setup.ps1 -DryRun -PackageZip <full-path>\swift-autotrader-<sha12>.zip -PackageSha256 <64-hex-sha256>
```

Expected: `status = PREVIEW_NO_CHANGES`, `mode = PAPER_ONLY`, and no files, tasks or
virtual environments created. Verify with `Test-Path <install-root>` returning `False`.

## 3. Isolated install, no scheduled task

Use a dedicated root, never a production or Phoenix/Kalshi path:

```powershell
.\setup.ps1 -Apply -NoSchedule -InstallRoot C:\SwiftAcceptance\install `
  -PackageZip <full-path>\swift-autotrader-<sha12>.zip -PackageSha256 <64-hex-sha256> `
  -ExpectedCommit <full-40-char-sha>
```

Expected:
- The uv bootstrap installs with `--require-hashes` (log line from `setup.ps1`), then
  `uv 0.10.0` is reported. Any "Unexpected or unverified uv installer version" is a stop.
- `Get-ScheduledTask -TaskName *Swift*` returns nothing (no task was created).
- The locked dependency install succeeds, and SQLite migrations apply without error.

## 4. Status and health (read-only)

```powershell
.\manage.ps1 -Action Status -InstallRoot C:\SwiftAcceptance\install -Mode Paper
.\manage.ps1 -Action Health -InstallRoot C:\SwiftAcceptance\install -Mode Paper
```

Expected: `mode = PAPER_ONLY`; no `LIVE` value anywhere; health reports the paper
heartbeat as absent or stale before a cycle, never "running" without evidence.

## 5. Backup and restore rehearsal

```powershell
.\manage.ps1 -Action Backup -InstallRoot C:\SwiftAcceptance\install -Mode Paper
# Pick the newest file in <install-root>\backups\ as $bk
.\manage.ps1 -Action Restore -InstallRoot C:\SwiftAcceptance\install -Mode Paper -BackupFile $bk
```

Expected: backup writes a SQLite online copy; restore is refused while the task is
running and succeeds when stopped. Record the SHA-256 of the database before and after.

## 6. Rollback rehearsal

Requires a previous managed release (`previous-release.txt`). Install a second
reviewed package into the same isolated root, then:

```powershell
.\manage.ps1 -Action Rollback -InstallRoot C:\SwiftAcceptance\install -Mode Paper
```

Expected: code returns to the previous release; the database is untouched. A failed
rollback is a stop condition, not something to repair by hand.

## 7. PowerShell 5.1 parse check (no execution)

```powershell
powershell.exe -NoProfile -Command "Get-ChildItem .\deployment\windows\*.ps1 | ForEach-Object { $e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$null,[ref]$e); if ($e) { $_.Name + ': ' + $e[0].Message } else { $_.Name + ': OK' } }"
```

Expected: every script prints `OK`. Repository tests that need 5.1
(`tests/integration/test_windows_deployment_contract.py`, `tests/unit/test_live_activation.py`)
are skipped on the Mac; run them on the laptop with the locked environment:

```powershell
uv run --locked --extra dev --extra telegram python -m pytest -q
```

Expected: the 4 Windows-only tests that skip on macOS now run and pass. Any failure is a stop.

## 8. Teardown and rollback to a clean state

```powershell
.\manage.ps1 -Action Stop -InstallRoot C:\SwiftAcceptance\install -Mode Paper
.\manage.ps1 -Action Uninstall -InstallRoot C:\SwiftAcceptance\install -Mode Paper
```

Expected: uninstall removes only the Swift acceptance root. It must not touch any other
scheduled task. Do not use `-PurgeData` or `-IUnderstandDataLoss` unless the owner
explicitly asks. Confirm Phoenix/Kalshi tasks and folders are untouched afterwards.

## 9. Stop conditions

Stop and report, without retrying, if any of these occur: hash mismatch; a dry run
that creates a file; a scheduled task appears during `-NoSchedule`; `mode` other than
`PAPER_ONLY`; any LIVE flag set to true; a Phoenix/Kalshi file or task is touched;
a failed restore or rollback.

## 10. Evidence to return

Package SHA-256, the commit SHA, each command's exit code and key output line, the
database SHA-256 before and after restore, the PowerShell 5.1 parse output, the pytest
summary for the four Windows-only tests, and any unexpected output.
