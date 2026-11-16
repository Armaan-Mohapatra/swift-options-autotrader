# Swift AutoTrader — Production Deployment Compatibility Review & Remediation Report

**Date:** 2026-11-16  
**Repository:** `Armaan-Mohapatra/swift-options-autotrader`  
**Base Commit:** `5303a89ab707dd51e81331b54db3e70450bc3a6b`  
**Development Branch:** `fix/production-deployment-compatibility-20261116`  
**Status:** Remediated, independently verified in simulation, 720 tests passing (0 failures, 1 warning).

---

## Executive Summary

ChatGPT independently reviewed commit `5303a89a` and verified the published 710-test baseline. However, its audit identified six real deployment architecture deficiencies in the previous operational proposal:

1. **Manifest Mode Conflict in Credential Paths:** `config.py`'s `validate_private_path` and `WindowsDPAPITokenStorage` rejected any manifest where `mode != "PAPER_ONLY"`. When `deployment.json` is set to `LIVE` for production, managed credential initialization failed immediately.
2. **Hardcoded Paper Constraints in Deployment Scripts:** `deployment/windows/common.ps1` (`Assert-SwiftManifest` and `Set-SwiftEnvironment`) threw errors on any `LIVE` manifest and set paper-only environment variables.
3. **Missing Live Task Registration Action:** `deployment/windows/service.ps1` did not support the `RegisterLiveTask` action referenced in earlier documentation.
4. **Missing Account Pinning Utility:** `scripts/pin_account.py` was referenced in deployment steps but did not exist in the repository.
5. **Invalid Managed Root Git Operations:** Earlier deployment instructions erroneously ran Git commands inside `E:\SwiftAutoTrader\install-837aec03` (which is a managed runtime root, not a Git checkout) and referenced obsolete virtual environments.
6. **Execution vs. Read-Only Diagnostic Misrepresentation:** `live-dispatch --once` was inaccurately described as a read-only diagnostic, when in reality it executes a complete dispatch cycle and will submit real orders if unhandled signals exist.

All six issues have been remediated, verified, and backed by automated test suites without weakening any credential guards, brokerage authorization checks, or trading engine safety invariants.

---

## 1. Technical Remediation Details

### Issue 1: Manifest Mode Support in `validate_private_path` and `WindowsDPAPITokenStorage`
- **Location:** `src/swift_autotrader/config.py`, `src/swift_autotrader/broker/mcp_local_auth.py`, and `src/swift_autotrader/windows_runtime.py`
- **Root Cause:** `validate_private_path` hardcoded `manifest.get("mode") != "PAPER_ONLY"` and required `live_enabled` and `broker_writes_enabled` to be `False`. `validate_install_root` had a similar restriction.
- **Resolution:**
  - `validate_private_path` now takes `allowed_modes: tuple[str, ...] = ("PAPER_ONLY", "LIVE")`.
  - Mode-specific validation:
    - If `mode == "PAPER_ONLY"`, `live_enabled` and `broker_writes_enabled` must both be `False`.
    - If `mode == "LIVE"`, `live_enabled` and `broker_writes_enabled` must both be `True`.
  - In `Settings.validate()`, `expected_mode = "LIVE" if self.is_live else "PAPER_ONLY"`. A paper configuration strictly rejects a `LIVE` manifest (`test_live_manifest_not_accepted` preserved), while a live configuration strictly rejects a `PAPER_ONLY` manifest.
  - `WindowsDPAPITokenStorage` passes `allowed_modes=("PAPER_ONLY", "LIVE")` to allow credential verification during setup and production execution.
  - `validate_install_root` in `windows_runtime.py` similarly validates mode-specific flags for both `PAPER_ONLY` and `LIVE` roots.

### Issue 2: Live Support in `deployment/windows/common.ps1`
- **Location:** `deployment/windows/common.ps1`
- **Root Cause:** `Assert-SwiftManifest` rejected `mode -ne 'PAPER_ONLY'`. `Set-SwiftEnvironment` threw on non-paper settings.
- **Resolution:**
  - `Assert-SwiftManifest` now accepts parameter `[string[]]$AllowedModes = @('PAPER_ONLY', 'LIVE')` and validates matching flags for both modes.
  - `Set-SwiftEnvironment` now accepts parameter `[ValidateSet('PAPER_ONLY', 'LIVE')][string]$Mode = 'PAPER_ONLY'`.
  - When `$Mode -eq 'LIVE'`, it exports:
    - `$env:PAPER_ONLY = 'false'`
    - `$env:LIVE_ENABLED = 'true'`
    - `$env:ALLOW_BROKER_WRITES = 'true'`
    - `$env:BROKER_BACKEND = 'robinhood_mcp'`
    - `$env:DB_PATH = Join-Path $InstallRoot 'data\live.sqlite3'`
    - Standing risk ceilings: `$env:MAX_CONTRACTS_PER_SIGNAL = '1'`, `$env:MAX_DAILY_LOSS_USD = '750'`, `$env:MAX_CONCURRENT_POSITIONS = '3'`, `$env:SLIPPAGE_TOLERANCE_PCT = '10'`
    - Enforces presence of `secrets\oauth.dpapi` and `secrets\agentic_account.dpapi`.
  - Added helper function `Set-SwiftLiveEnvironment` for one-call initialization.

### Issue 3: Scheduled Task Support in `service.ps1` and `manage.ps1`
- **Location:** `deployment/windows/service.ps1` and `deployment/windows/manage.ps1`
- **Root Cause:** `service.ps1` was only a task runner script without parameter handling or task registration.
- **Resolution:**
  - Updated `service.ps1` to accept:
    - `[ValidateSet('Run', 'RegisterLiveTask', 'UnregisterLiveTask')][string]$Action = 'Run'`
    - `[ValidateSet('Paper', 'Live')][string]$Mode = 'Paper'`
  - Action `RegisterLiveTask` registers `SwiftAutoTrader-Live-Local` pointing to:
    `service.ps1 -InstallRoot $root -Mode Live`
  - Action `UnregisterLiveTask` unregisters `SwiftAutoTrader-Live-Local`.
  - In `manage.ps1`, added `RegisterLiveTask` and `UnregisterLiveTask` to the supported action set, and added `-Mode 'Paper' | 'Live'` to allow querying health status (`live-health.json` vs `health.json`).

### Issue 4: Implemented `pin_agentic_account` and `scripts/pin_account.py`
- **Location:** `src/swift_autotrader/broker/mcp_local_auth.py`, `src/swift_autotrader/app.py`, and `scripts/pin_account.py`
- **Root Cause:** `load_pinned_agentic_account` existed, but there was no tool or script to write `secrets\agentic_account.dpapi`.
- **Resolution:**
  - Implemented `pin_agentic_account(root: Path, account_number: str) -> Path` using Windows DPAPI (`win32crypt.CryptProtectData`).
  - Added CLI command `swift-autotrader pin-account --root <ROOT> --account-number <ACCOUNT>`.
  - Created standalone script `scripts/pin_account.py`.
  - Updated `scripts/build_windows_package.py` and `setup.ps1` to stage `scripts/pin_account.py`.
  - Added complete test coverage in `tests/unit/test_pin_account.py` (roundtrip encryption, account length validation, CLI script execution, and app CLI execution).

### Issue 5: Corrected Windows Deployment and Upgrade Architecture
- **Root Cause:** Instructions confused the Git checkout directory with the managed installation runtime directory.
- **Clarification:**
  - `E:\SwiftAutoTrader\install-837aec03` is a managed runtime root. It does **not** contain a `.git` repository, and `validate_private_path` actively forbids any `.git` repository in the install root or parent directory hierarchy.
  - Source checkouts live separately (e.g. `C:\Users\armaan\swift-options-autotrader` or an approved release ZIP).
  - Updates must be applied using `setup.ps1`:
    ```powershell
    powershell -ExecutionPolicy RemoteSigned -File .\deployment\windows\setup.ps1 `
        -Apply -ExpectedCommit <REVIEWED_40_CHAR_SHA> `
        -SourceRoot C:\Users\armaan\swift-options-autotrader `
        -InstallRoot E:\SwiftAutoTrader\install-837aec03 `
        -NoSchedule
    ```
  - `setup.ps1` copies clean source into `releases\<commit>\`, runs locked `uv sync` into `releases\<commit>\.venv\`, copies deployment scripts into `scripts\`, updates `active-release.txt`, and validates migrations and smoke tests.
  - The Python binary is always resolved via `Get-SwiftPython $InstallRoot` (`releases\$(Get-Content active-release.txt)\.venv\Scripts\python.exe`), never hardcoding obsolete commit hashes.

### Issue 6: Accurate Designation of Read-Only Diagnostics vs. Live Execution
- **Distinction:**
  - **Read-Only Diagnostic:**
    ```powershell
    $env:ROBINHOOD_MCP_INTERACTIVE = 'true'
    & (Get-SwiftPython $root) -m swift_autotrader mcp-diagnostics --connect
    ```
    This command connects to the Robinhood MCP server, verifies tool declarations, inspects account approvals, and returns account status with `real_broker_writes_enabled: False`. It never places orders.
  - **Active Execution Cycle:**
    ```powershell
    & (Get-SwiftPython $root) -m swift_autotrader live-dispatch --root $root --once
    ```
    This command executes a full live dispatch cycle. If matching unhandled Telegram signals are present, it evaluates risk, checks buying power, and **submits real live orders to Robinhood**. It must never be run or described as a diagnostic.

---

## 2. Verification & Test Evidence

### Test Suite Execution Summary
- **Baseline (prior turn):** 710 passed, 1 warning in 10.92s
- **Current (after remediation):** 720 passed, 1 warning in 10.16s
- **New Unit Tests Added (+10):**
  - `tests/unit/test_managed_credential_paths.py`:
    - `test_live_manifest_accepted_for_live_settings` (verifies live settings accept LIVE manifest)
    - `test_paper_manifest_rejected_for_live_settings` (verifies live settings reject PAPER_ONLY manifest)
    - `test_oauth_path_accepts_live_and_paper_manifest` (verifies OAuth token storage accepts both modes)
  - `tests/unit/test_windows_runtime.py`:
    - `test_validate_install_root_live_manifest` (validates LIVE manifest flag requirements and mode restrictions)
  - `tests/unit/test_pin_account.py`:
    - `test_pin_account_refuses_non_windows`
    - `test_load_account_refuses_non_windows`
    - `test_pin_and_load_account_roundtrip`
    - `test_pin_account_length_validation`
    - `test_pin_account_script_cli`
    - `test_app_pin_account_command`
- **Secret Scan (`scripts/scan_secrets.py`):** Clean (heuristic scan passed, zero violations).

---

## 3. Cryptographic File Fingerprints

| File Path | Bytes | SHA-256 Digest | Git Blob Object |
| :--- | :---: | :--- | :--- |
| `deployment/windows/common.ps1` | 13,718 | `0e299cb844c1260347ed6a4583df07e7332e9d2aef995444dfbdd78dfa26bb3a` | `98f5076ffc93849a835860c91ab6207565308914` |
| `deployment/windows/manage.ps1` | 7,666 | `04aa3fb113b5b56fdafd572445b9ad387db5d65e81b56f2c68179cafbcb9fe6e` | `616bf4048968aa5c20f8cb8d8f15e04ef7fb8271` |
| `deployment/windows/service.ps1` | 3,357 | `26effc0f48ab5fd0905b711cac2348a0236dec8ac86547aab0814a45871a0a03` | `bc3e33985b8d0958ab7c6d338e5340a9adbad8b4` |
| `deployment/windows/setup.ps1` | 13,582 | `8d4631f30fd8a35982d11040408852b398da1533bf2eeaf622d05c1d8393e6d8` | `1cbedf2c16e0dfc34bcf8c3e4f859c6c1052f297` |
| `scripts/build_windows_package.py` | 9,584 | `c073cc82eba420670b5c253bb4ee7795f3129d6c969891a718ad6f22e3f38a58` | `24a60036808537abdf81f832a5d997723e0b0d78` |
| `scripts/pin_account.py` | 1,163 | `4044fe7af902b0278127a4fb8f83d504e8f913347eb1b0d75c57e756f35619eb` | `e0b006cbce69941007c6b86d9c6384409f7bd065` |
| `src/swift_autotrader/app.py` | 15,587 | `52b3916e572ee3c1c4e7008a7ebf22ce2e032f7477210023250325c987903100` | `c1bb11f994113301e8e15e01dc432f6675b29bcb` |
| `src/swift_autotrader/broker/mcp_local_auth.py` | 11,498 | `6d8254a77a5f9740a103d8196fc6c916572b7c5d8c63fca1379ff736725e024a` | `3556dd7c12c523f26312d224c1ffb34d4dbaff34` |
| `src/swift_autotrader/config.py` | 13,208 | `945125d6a55dd3d426ac07669c5e3f3fcd035df2d0831c03479833392d9f32f8` | `24e729e08ea6c754501dc766502ce1f4308bf5ec` |
| `src/swift_autotrader/paper_service.py` | 3,722 | `8ebbed5291d8bc11dc24b1225f05d6bea7f4de784189e06f7f22f2554d05cb18` | `6a23b4c729d6a22283e5d1845944ce214808d097` |
| `src/swift_autotrader/windows_runtime.py` | 21,760 | `2ba99f0abe2b61401b8238e39b4d8a4f45d8fdf62ecbc0653b05d9d85fd90d82` | `8b667ed324fd7de356661862cb3d55d3fd1d8a78` |
| `tests/unit/test_managed_credential_paths.py` | 6,153 | `fcad15b207f66b4f324e66392d665dfb7b0bb40dbcd3e2dc132c37d83ea891f0` | `a50bacb36b1f85f8f3dda86368e56344c0fb75fc` |
| `tests/unit/test_pin_account.py` | 4,526 | `39777b2e80ef9ac3881e5726c4127f7a662845cfccfa327fc81bb569fe7d959c` | `f368be4b4ae607682fe5c532ec599367ca86f978` |
| `tests/unit/test_windows_runtime.py` | 13,093 | `a9938cc1ef59b3f5103bc59ed8240613927591ce6f1a8164e0dc597f4322c462` | `3d4d0799c4b8a53a8458bbfc5bbdeba54b1f210c` |

---

## 4. Corrected Windows Operational Procedure

### Step 1: Deploy / Update Application Code via `setup.ps1`
From an updated Git checkout directory (e.g., `C:\Users\armaan\swift-options-autotrader`):
```powershell
powershell -ExecutionPolicy RemoteSigned -File .\deployment\windows\setup.ps1 `
    -Apply -ExpectedCommit <REVIEWED_40_CHAR_SHA> `
    -SourceRoot C:\Users\armaan\swift-options-autotrader `
    -InstallRoot E:\SwiftAutoTrader\install-837aec03 `
    -NoSchedule
```

### Step 2: Configure Production Mode and Approved Parameters
Update `E:\SwiftAutoTrader\install-837aec03\deployment.json`:
```json
{
  "product": "swift-options-autotrader",
  "mode": "LIVE",
  "live_enabled": true,
  "broker_writes_enabled": true,
  "created_utc": "2026-11-16T05:00:00Z",
  "installation_guid": "837aec03-0000-0000-0000-000000000000"
}
```

Update `E:\SwiftAutoTrader\install-837aec03\config\settings.json`:
```json
{
  "mode": "LIVE",
  "live_enabled": true,
  "broker_writes_enabled": true,
  "broker_backend": "robinhood_mcp",
  "robinhood_mcp_account": "<CONFIRMED_ACCOUNT_NUMBER>",
  "telegram_enabled": true,
  "telegram_channel_id": -1001234567890,
  "telegram_api_id": 12345678,
  "max_contracts_per_signal": 1,
  "max_daily_loss_usd": "750",
  "max_concurrent_positions": 3,
  "slippage_tolerance_pct": "10",
  "log_level": "INFO"
}
```

### Step 3: Pin Confirmed Brokerage Account
Using the pinned script staged in the installation:
```powershell
$root = "E:\SwiftAutoTrader\install-837aec03"
. (Join-Path $root 'scripts\common.ps1')
$py = Get-SwiftPython $root

& $py (Join-Path $root 'scripts\pin_account.py') --root $root --account-number "<CONFIRMED_ACCOUNT_NUMBER>"
```
*Expected Output:*
```json
{"status": "ACCOUNT_PINNED", "path": "E:\\SwiftAutoTrader\\install-837aec03\\secrets\\agentic_account.dpapi", "account_length": 8}
```

### Step 4: Run Safe, Read-Only Diagnostics
Verify account access and tool schemas **without** submitting orders:
```powershell
$env:SWIFT_INSTALL_ROOT = $root
$env:ROBINHOOD_MCP_INTERACTIVE = "true"
& $py -m swift_autotrader mcp-diagnostics --connect
```
*Expected Output:*
`real_broker_writes_enabled: false`, account discovery status `READY`, eligible accounts listed.

### Step 5: Register the Live Scheduled Task
```powershell
& (Join-Path $root 'scripts\service.ps1') -Action RegisterLiveTask -InstallRoot $root
```
*Expected Output:*
```json
{"status": "REGISTERED_LIVE_TASK", "task_name": "SwiftAutoTrader-Live-Local", "install_root": "E:\\SwiftAutoTrader\\install-837aec03"}
```

### Step 6: Monitor Service Status
```powershell
& (Join-Path $root 'scripts\manage.ps1') -Action Status -Mode Live -InstallRoot $root
```

---

## 5. Rollback & Emergency Stop Instructions

### Emergency Stop
To immediately stop live dispatch and prevent further entries:
```powershell
$root = "E:\SwiftAutoTrader\install-837aec03"
"STOP" | Set-Content -LiteralPath (Join-Path $root 'state\stop.request') -Encoding Ascii
Stop-ScheduledTask -TaskName 'SwiftAutoTrader-Live-Local' -ErrorAction SilentlyContinue
```

### Code Rollback
To rollback to the previously deployed release:
```powershell
& (Join-Path $root 'scripts\manage.ps1') -Action Rollback -InstallRoot $root
```

---

## 6. Remaining Real-World Acceptance Gates

The offline codebase is completely verified. Standalone live execution requires only the following on-laptop actions:
1. Interactive Windows login under the designated user account.
2. Initial browser-based Robinhood OAuth approval (saving tokens to `secrets\oauth.dpapi`).
3. Confirmation of the single agentic account number with options approval.
4. Setting persistent stop marker removal when ready for unattended operation.
