# Swift AutoTrader — integrated candidate audit

**Disposition: NO-GO for live activation.** Review date: November 14, 2026 (Pacific). This is an engineering audit, not an installation package or authorization. Existing receive-only deployment was not modified. No broker requests, real orders, account settings changes, credentials, or Windows tasks were accessed or executed.

## Inputs and provenance

- Original runtime: `Swift-Integration-Handoff.zip`, source baseline release `0c090e034ba7a761a1eb1a0cd08e06c91ddf8098`.
- Tests: `Swift-Consolidated-Preparation.zip` (147 baseline cases).
- First Gemini full-module exports: `gemini-code-1794720671463.py` (broker evidence), `gemini-code-1794720685830.py` (read-only MCP adapter), `gemini-code-1794720698869.py` (companion).
- New exports: `gemini-code-1794724710287.py` (nine-line tool constants fragment, not complete adapter), `gemini-code-1794724714074.py` (policy), `gemini-code-1794724743190.py` (mandate CLI), `gemini-code-1794724724117.json` and `gemini-code-1794724740197.json` (byte-identical proposed approval contracts).
- Seven PowerShell script exports in current upload: installer, validator, starter, status, stop, emergency stop and rollback. Scripts are authored but not Windows-tested here.
- Telegram notifier candidate extracted from the supplied Gemini conversation transcript, lines 4181–4338 (not a standalone attachment). Recovery and migration modules reused unchanged.
- Local completion receipt verifies receiver heartbeat, DPAPI credential storage, logon registration, and notification acknowledgment, not live deployment.

## Actual independent test results

1. Python static parsing of the new policy fails at line 72: `def _review_ expressly_unblocked(...)` (invalid syntax).
2. The typo was corrected ONLY in a disposable source copy for testing. The original uploads remain unmodified. The latest nine-line adapter fragment and its prose integration instructions were NOT applied as a full adapter patch: the earlier full Gemini adapter was used unchanged. The latest notifier was reconstructed from the transcript.
3. Existing isolated suite: **147 passed, 1 warning in 2.05s**, Linux.
4. Expanded suite: **153 passed, 1 warning in 2.07s**, Linux. This means 147 baseline cases and 6 characterization cases reproducing known defects. These extra six passes are evidence of defects, not readiness. One intermediate rerun failed at the guard self-test because an existing disposable probe DB had a table of the same name; removing only that scratch probe and rerunning resolved the harness issue.
5. Network, external execution and SQLite boundary guard self-tests passed. Synthetic fake broker only; this is defense-in-depth, not a claim of OS sandboxing. No Windows PowerShell executable is available in this environment.

## Defect register and release acceptance criteria

| ID | Finding | Required acceptance evidence |
|---|---|---|
| A01 | Policy is syntactically invalid as delivered (line 72). | All delivered Python modules compile and import in an isolated environment; retest exact final bytes. |
| A02 | Earlier complete adapter filters both approval read tools out of discovered tools. The new nine-line fragment declares optional tools but does not implement the full discovery/read changes. | Full integrated adapter, read-only discovery test with actual redacted tool schemas; no invented tools or provider permission assumptions. |
| A03 | Companion `entry_risk` still requires all nonzero account positions to match Swift-owned lots. Constructor rejects external-position opt-in. Prior field receipt had three existing account positions at that time. | Fresh, read-only ownership reconciliation; independent holdings protected, unresolved exposure blocks, no forced liquidation or journal deletion. |
| A04 | Policy helper accepts a review without `order_checks`, while companion requires checks. | One consistent validation definition; missing, malformed, contradictory, or warning-bearing reviews denied. |
| A05 | Release and risk binding are optional; `Mandate.load` permits absent fields even when expected values are provided. Companion startup does not supply expected values. Revocation is not re-read by the in-memory authorizer. | Explicit owner authorization bound to exact tested release/configuration; check expiration, revocation and identity without weakening provider permissions. |
| A06 | Daily authorization count is shared across entries, exits and cancellations, so a consumed entry budget blocks risk-reducing follow-ups. | Test end-of-budget behavior without accidentally allowing new exposure; account owner must explicitly authorize any risk-reducing action and broker rules continue to apply. |
| A07 | Stop script removes both stop markers even if wait deadline expires or health is absent, and unconditionally prints STOP_COMPLETED (lines 25–43). | Verified task/process termination and fresh health; persistent stop on timeout/error; no clearing emergency markers or restarting without operator action. |
| A08 | Emergency stop writes markers and moves canonical mandate; does not actually stop scheduled tasks or assert process termination. In-flight broker request may already have been accepted. | Distinguish stop requested, process stopped, and broker working orders. Demonstrate safe handling of in-flight orders and independent broker inspection. |
| A09 | Installer computes hashes but never compares to trusted expected manifest; arbitrary release hash, copies existing virtualenv, may overwrite existing release, checks lock only momentarily, and promotes before comprehensive validation. | Verified immutable source/dependency manifest; candidate-only staging; validate exact installed bytes, safe journal migration and mutually exclusive lifecycle; no automatic live activation. |
| A10 | Validator can report TESTED_SYNTHETIC even if tests were skipped; catalog failure not a hard gate. It checks credential file existence, not actual permission. | Honest performed/skipped/failed statuses; failed/unavailable required checks prevent readiness claim; all validation isolated from live broker writes. |
| A11 | Launcher invokes foreground Python only, does not register/supervise a live execution task and lacks certification evidence gate. Receiver sharing is optional; separate locks alone do not prove exclusive account-wide writer ownership. | One receiver, one account writer, verified ownership/health and bounded restart; preserve current receiver; all modes tested with no auto-activation from setup. |
| A12 | Notifier increments failures but `from_install_root` reads environment only; candidate does not independently bridge existing saved DPAPI credentials or gate new entries based on notification failure. | Test secure credential reuse, notifier outage/reporting, no secret logging; avoid blocking reconciliation on notification transport. |
| A13 | Order ACK and cancellation shapes remain unverified; order intent lacks full recoverable details, UNKNOWN blocks on restart; current decoder's missing-reference behavior is not evidence of brokerage idempotency. | Evidence-backed supervised order lifecycle, fills, cancellations, expiry/exercise and restart reconciliation. No retry when outcome uncertain. |
| A14 | Rollback flips pointer with marker set but doesn't prove writer stopped or journal compatibility. | Freeze, verify process/task quiescence, preserve execution history, verify rollback compatibility; no automatic reactivation. |
| A15 | Fresh in-session options quote and forward message ingestion not established by Saturday's receipt; paper pilot Windows deployment not confirmed. | Fresh in-session read-only data, actual new authorized message receipt and synthetic/paper end-to-end lifecycle demonstration; supervised broker validation separately authorized. |

## Consolidated next release requirements

Resolve this register as one integrated change set, using the exact pinned source baseline and full named modules (no snippets in place of complete source). Maintain read-only and paper isolation, keep real-money execution disabled by default, and provide one archive with manifest, regression and negative-path test results, dependency lock, administration scripts, receipts and rollback plan. A safe installer must not certify or activate unverified functionality. Confirm current account exposure and authorization with the owner; code ceilings are not approved operating budgets. Do not bypass broker-required approval. Do not automatically close independent positions or delete execution journals. If some provider behavior cannot be verified without real orders, label it unverified and require a separately authorized supervised check. Windows-specific checks and an explicit owner decision remain necessary after independent review. No claim of readiness is warranted from a synthetic suite alone.

## Test runner evidence (redacted, no credentials)

TRUSTED_TEST_RUNNER_INITIALIZED
NETWORK_GUARD_SELF_TEST_PASS
EXTERNAL_EXECUTION_GUARD_SELF_TEST_PASS
SQLITE_GUARD_SELF_TEST_PASS
........................................................................ [ 48%]
........................................................................ [ 97%]
...                                                                      [100%]
147 passed, 1 warning in 2.05s

Expanded characterization suite:

TRUSTED_TEST_RUNNER_INITIALIZED
NETWORK_GUARD_SELF_TEST_PASS
EXTERNAL_EXECUTION_GUARD_SELF_TEST_PASS
SQLITE_GUARD_SELF_TEST_PASS
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed, 1 warning in 2.07s
