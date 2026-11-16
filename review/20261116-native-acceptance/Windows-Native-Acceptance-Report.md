# Swift AutoTrader — Native Windows Acceptance Report

Date: 2026-11-16 (local Windows host)

## Release integrity and installation

- Repository: `Armaan-Mohapatra/swift-options-autotrader`
- Reviewed source commit: `86828420f2544f942b64d723910fa194af58f8d9`
- Verified source tree: `34d33fe0e48b34225d802496eb1c7915d079acd1`
- Parent: `5bb00fea8c55019ecc7a7a2c7c205a6ac58e6e6e`
- The exact published commit was fetched and checked out detached in the clean external checkout. Commit, tree, parent, and clean status matched. No patch reconstruction or reapplication occurred.
- Installed active release: `86828420f2544f942b64d723910fa194af58f8d9`
- Managed root: `E:\SwiftAutoTrader\install-837aec03`
- Installed active interpreter: release-local Python 3.11 virtual environment; startup and smoke checks succeeded.
- The supported installer ran with `NoSchedule`. It retained the existing Paper task without registering a Live task. Candidate release pointer and rollback pointer were verified.
- Existing configuration, DPAPI account binding, OAuth file, Telegram session and API credential records had unchanged local fingerprints across deployment. Credential material and fingerprints are intentionally not published.
- Before application, both managed SQLite files were backed up through SQLite’s online backup API after the Paper service stopped. Backups passed integrity check. Post-install `swift.sqlite3` and `live.sqlite3` both pass `PRAGMA integrity_check`; each contains zero order intents. No simulated journal data was imported into the production journal.

## Native tests and verification

- Clean, locked Python 3.11 test environment on E:, Python 3.11.9; locked dependency audit: 50 packages / 711 hashed artifacts.
- Full offline suite against the exact candidate: **808 passed, 2 skipped, 0 failed**. Skips were Windows symlink creation unavailable in the test context and the POSIX-only file-mode test. A duplicate ZIP member warning is emitted by a negative-path test.
- The initial full-suite attempt with pytest temporary files on C: had two worker-output timeout failures under the nearly full system drive. Re-running with `TEMP`, `TMP`, and pytest temporary files on E: passed completely. No source changes were made in response.
- Focused boundary/supervised lifecycle/exposure/order-engine tests: **78 passed**; isolated synthetic exit-review refusal rehearsal: **1 passed**; paper-dispatch, paper-service guard, dashboard, Windows runtime and supervisor tests: **83 passed**.
- Python compile and dependency lock audit passed. Static lint did not pass: repository-wide Ruff reported multiple pre-existing style findings, including changed files. `scripts/phase7_verify.py` also fails its legacy static assumptions that the Robinhood adapter always denies writes and that the former MCP allowlist guard exists. Those checks do not model the reviewed, explicitly gated standalone live adapter; this audit must be updated and re-reviewed before treating that certification gate as passed.

## Authentication and read-only broker checks

- Telegram session loaded under the scheduled task’s designated interactive Windows user; Telegram status currently reports `CONNECTED` and the latest heartbeat is fresh. Telegram is read-only.
- Robinhood standalone OAuth DPAPI data decrypted using the supported loader. The DPAPI account binding matched the authenticated account; account eligibility/options permission and buying-power read succeeded. All required read-only MCP tool schemas were present with no mismatches. The diagnostic ran with broker writes disabled and callbacks that refuse interactive enrollment. No credential was re-enrolled.
- This proves standalone read-only connectivity and account binding, not order-placement permission, provider approval for unattended orders, market quote freshness for a specific alert, or successful standalone brokerage execution. No order endpoint was called.

## Current PAPER operation

- `SwiftAutoTrader-Paper-Local`: **Running** under the designated interactive user. Task Scheduler owns the launcher; it is not a child of the invoking Codex/PowerShell process. The task action points to this managed root’s `scripts\service.ps1`.
- Task settings: multiple instances `IgnoreNew`; no execution time limit; can start on battery and does not stop when switching to battery. A competing Live task is absent. Two older task names remain `Ready` and were not modified.
- Five supported Paper restart rehearsals completed. Each cycle logged orderly stop of the dashboard, Telegram and paper-dispatch workers, followed by one new worker set. Supervisor/dashboard identities changed on each restart; previous-cycle identities exited. No duplicate listener owned port 8765.
- Current health heartbeat is fresh; supervisor, dashboard, Telegram receiver and paper-dispatch workers report alive. `http://127.0.0.1:8765/api/status` returns HTTP 200. Dashboard states `PAPER_ONLY`, brokerage writes disabled, and explicitly reports no standalone execution evidence.
- The worker is configured for continuous Telegram-to-paper dispatch using the supported Robinhood read-only data source. This acceptance window contained no fresh eligible alert, so it produced no simulated order or fill. Worker liveness is not evidence of a simulated trade.
- Service logs: `E:\SwiftAutoTrader\install-837aec03\logs\supervisor.log` and `worker-*.jsonl`. Restart evidence is in `E:\SwiftAutoTrader\diagnostics\restart-rehearsals-86828420.json`.
- Historical/synthetic exit-review test: a filled synthetic entry followed by provider approval-required exit review resulted in no sell placement; the residual position remained visible, an `ALERT_REQUIRED` / `UNFILLED_EXIT` notification was generated, and automatic exit resubmission was not established. This is an unattended lifecycle limitation requiring an operator/provider action when an exit is refused.

## Host power and storage

- At final measurement C: had 23.58 GiB free; E: had approximately 496.0 GiB free. The validation caches and temporary test data were placed on E:. No storage cleanup or unrelated relocation was performed in this acceptance run.
- Windows sleep timers are `Never` on both AC and battery. The Paper task does not stop on battery. Lid-close behavior was not verified; retain the laptop open until checked.
- AC power remains unresolved: Windows `GetSystemPowerStatus` and WMI report AC offline / discharging while at 100% charge; the AC adapter device itself reports present/OK. These disagreeing sensors do not establish that the physical charger is disconnected. The owner must verify the actual charger/port and that Windows reports AC online before unattended continuity can be certified.
- Task logon type is Interactive. After reboot, the designated Windows user must log in for the DPAPI-backed task to start; automatic pre-logon service operation is not established.

## Production decision: NO-GO / not authorized

- Current actual mode: **PAPER_ONLY**. `deployment.json` and effective configuration both deny brokerage writes. No Live scheduled task is registered or running. Production journal is healthy and empty of orders. No order is pending and no Swift position is recorded in that journal.
- Offline synthetic boundary tests support expiry/revision/duplicate/provider-review refusals, but no actual supervised brokerage transaction was authorized or attempted. There is no eligible fresh, specifically approved alert/contract receipt, no standalone live order/reconciliation evidence, and no owner authorization for continuous unattended real-money operation. ChatGPT/Gemini/Codex brokerage history is not standalone Swift acceptance evidence.
- The provider-review refusal exit limitation means positions may require intervention. A single-entry supervised receipt cannot authorize continuous unattended trading. Provider-required approvals remain in force.
- Prior service exit-code-2 history has not recurred in the five restart rehearsals, but its original root cause remains unproven.

### Required actions before any production activation

1. Owner verifies stable AC recognition and decides lid-close behavior; task operation after reboot requires the designated user’s interactive logon under the current DPAPI design.
2. Resolve and retest the exit-review refusal/residual-exposure lifecycle so refusal cannot silently leave an unattended position; define provider-approval and operator-notification handling.
3. Reconcile the outdated Phase 7 static audit and clear the repository lint findings in a separately reviewed candidate; do not silently alter this exact release.
4. Obtain the required provider approval, then perform the supported supervised acceptance only after the owner supplies specific transaction authorization for a genuine fresh alert, exact contract/quantity/limit/exposure/fees, expiry, and permitted cancellation/exit lifecycle. Keep writes disabled until that test is independently accepted.
5. Obtain separate explicit owner authorization for continuous unattended production configuration. Then follow the reviewed activation procedure and verify the actual task, order/position reconciliation and runtime evidence. This report does not grant that authorization.

No live write, real order, credential reenrollment, paid workflow, or unrelated Phoenix/Kalshi modification occurred.
