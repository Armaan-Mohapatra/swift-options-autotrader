# Swift AutoTrader — release remediation and live readiness plan

**Disposition: REVIEW ONLY / NO-GO FOR REAL-MONEY ACTIVATION.** The source package intentionally blocks the execution companion's CLI. Existing production receiver and brokerage settings were not accessed or modified. The purpose is to finish engineering and demonstrate readiness, not certify a live release on the basis of mock tests.

## Baselines and evidence

- GitHub private repository `Armaan-Mohapatra/swift-options-autotrader`, last prior draft PR #25 head `09e854e9c7921f4a59bca5e6f0451d1a45688598` (remote review checkpoint). New review work belongs on `audit/gemini-remediation-20261114`, under `review/20261114-gemini-remediation/`, not on the production branch. The Windows installed runtime marker `0c090e034ba7a761a1eb1a0cd08e06c91ddf8098` has not been proved to be a Git commit.
- The earlier local receipt established receiver heartbeat, Task Scheduler logon registration, private bot acknowledgment and current-user DPAPI storage; it did not establish fresh source message delivery. 147 Windows synthetic tests passed earlier against a different source revision. The latest paper candidate passed 157 Linux tests, Windows deployment unconfirmed.
- This source package combines the exact 55-file archived runtime with Gemini's full adapter/companion/evidence modules, policy, mandate CLI, notifier extracted from lines 4181–4338 of the owner-supplied transcript, and the prior test suite. The newly repaired source and 14 new negative-path tests passed 161 isolated Linux tests, with 1 warning. Synthetic fixtures are not observed live broker responses. No actual trade or permission change occurred.

## A01–A15 remediation matrix

| ID | Implemented now | Remaining gate |
|---|---|---|
| A01 | Corrected invalid function name; compiled Python source and ran suite. | Final Windows exact-byte tests. |
| A02 | Retained advertised optional approval read-tool schemas in discovery; base facade still denies calls outside its read allowlist. | Provider-specific read-only authorization integration, strict input/output validation, account matching, pagination and actual evidence. Do not treat account-level permission as per-order permission. |
| A03 | Negative test confirms independent positions remain blocked from adoption; no ownership guard weakened. | Fresh reconciled external exposure, working orders, and isolation of independently held lots. Prior receipt showed three existing holdings and zero Swift trades. Never force-liquidate or delete journals. |
| A04 | Explicit clean order checks required; contradictory/malformed approvals blocked. | Verify provider schema, fresh review identity and reject missing response fields in final integration. |
| A05 | Expected release/config must actually match, not be omitted. MCP error-object denied, implicit equity fallback removed, nonpositive premium denied. | Mandatory release/config pinning at startup, atomic creation, revocation/expiry checks immediately before any authorized action and on restart; separate owner validation and secure local authorization. |
| A06 | Original total daily cap preserved; no unapproved widening. | Explicit owner-approved entry budget and reserved risk-reducing action budget, bounded by scope and broker requirements. Tests must show new exposure stops without stranding owned exits; resolve mandate expiry behavior. |
| A07 | Draft stop replacement retains marker, never auto-clears, does not falsely report termination. | Native Windows proof of process/task termination, no restart on stop, independent reconciliation. |
| A08 | Stop request explicitly not cancellation and not proof of stopped service; receiver untouched. | Actual service lifecycle and revocation; reconcile any outstanding orders separately. |
| A09 | Removed unsafe promotion scripts from runnable review package; manifest-verified disposable staging. | Final immutable deployment, verified dependencies, OS locks held over promotion, backup and journal compatibility, separate activation. |
| A10 | Validator tests exact manifest and distinguishes actual pass/fail. | Windows run and later read-only broker evidence, no fabricated readiness. |
| A11 | Uncertified execution CLI explicitly blocked. Existing receiver stays unchanged. | Single serialized account writer with verified receiver health, explicit task ownership, bounded restart, DPAPI/OAuth coordination, fault recovery and safe stop. |
| A12 | Retained notifier candidate with bounded TLS/no redirects and tests. | Integrate local encrypted bot credential securely; verify delivery, pause new entries for critical failure without losing journal. |
| A13 | Existing write-ahead/UNKNOWN halt preserved. | Provider-documented and independently supervised actual acknowledgment, fill, cancellation, reference handling, order expiry/exercise, recovery and position reconciliation. |
| A14 | No automatic rollback or release pointer change shipped. | Freeze, verify no writer, verify journal compatibility; rollback without discarding real fills. |
| A15 | Previous field/paper evidence retained conceptually, no fresh evidence claimed. | Fresh market-session quote and new Telegram message, paper entry/exit/cancel lifecycle and reconnect/restart observations. |

## Critical path to an operational release

### Gate 1 — Integrated source closure (developer)
Use the exact archive and review branch. Close the remaining A02–A06, A11–A14 implementation items. Preserve separate receiver and execution locks, and one account-wide writer. Verify every provider contract against official documentation or sanitized read-only observations. Enforce strict source lineage: options only; LOTTO eligible; no averaging down; NEW AVG metadata; full closure of precisely linked Swift-owned lots on partial/runners/all-out instructions; no historical entry replay. Preserve validated exact prices and review any range interpretation with the owner. Unknown provider outcomes must remain blocked, never automatically retried. No credential or financial records in GitHub.

Acceptance: reproducible dependency manifest, source hashes, full regression suite plus negative tests for recorded gaps. Do not change risk ceilings, mandate scope or approval settings as a test shortcut. Distinguish actual test observations from design assertions.

### Gate 2 — Consolidated Windows certification (one local workflow)
Use an isolated review directory, the already-installed Python environment, manifest and exact candidate bytes, no production imports or live calls. Verify dependencies and architecture, run tests with network/external execution/database guards, simulate receiver and service recovery, stop/restart, lock conflict, rollback and journal migration on a copy, and ensure metadata is secret-free. Verify actual existing receiver and notification setup without recreating credentials. Only stage a future deployment if all required checks succeed. A test failure or skipped mandatory check is a blocker. A self-contained review archive is not a live installer.

### Gate 3 — Market-session read-only and paper validation (local)
During the next open regular options session (target Monday November 16, 2026, subject to exchange calendar and service availability), verify a new legitimate source callout reaches the correct receiver; verify fresh exact-contract bid/ask, quote timestamp and tick schedule; fetch pinned account snapshots, owned versus external positions and all working orders; validate review responses and approval contract with read-only operations. Use the separate paper service to observe entries, fills under conservative assumptions, exits, cancellations, duplicate/edited alerts, restart and outage behavior. Do not generate trades from historical alerts. Capture secret-free receipts and failure details. No minimum number of days guarantees safety or profitability; evaluate actual observed cases and coverage.

### Gate 4 — Supervised broker lifecycle verification (owner and developer)
Once reviewed and installed into a separate certified execution environment, any real transaction for verification must be specifically authorized by the account owner and permitted by Robinhood, with clear order, price, quantity, fee and potential loss. A supervised lifecycle should demonstrate independent confirmation of broker IDs, actual fills or terminal outcomes, matching positions, cancellation and recovery after restart. Do not intentionally create a risky order merely to pass a test; use permitted broker test/sandbox facilities where they exist. No unattended activation on a failed or ambiguous response. Do not disable approval requirements merely to make tests pass.

### Gate 5 — Explicit operational acceptance (owner)
Review actual effective exposure, buying power, premium and concurrency caps and potential losses; previous software ceilings of $999 individual premium, $9,999 aggregate exposure and $655 daily gross realized loss are not approved budgets or hard guarantees. A limit order may not fill; open option losses, fees and market moves can exceed a realized-loss stop threshold. Confirm scope, risk-reducing order allowances, expiry, revocation and emergency procedures. The broker retains final approval authority. Only an independently reviewed, exact verified release with all previous gates passed should be considered for unattended activation. If not, continue monitoring/paper or use individually approved orders. No absolute guarantee of safe operation or consistent profit.

## Operating acceptance and maintenance

Require fresh receiver and execution heartbeats, account identity, valid authorization, reconciled orders/positions, available quotes, healthy notification path and exclusive writer. Missing or stale information: stop new exposure, report condition, reconcile before recovery. Emergency stop cannot revoke already accepted broker orders; inspect them in Robinhood. Never clear a stop marker, delete a lock, rotate a ledger or recreate a mandate just to resume. No automatic renewal or uncontrolled restart. Track daily results and failures independently from certification; changes to source hashes, permissions, dependencies or schemas require affected gates to be repeated. Keep existing manual positions protected. Always maintain a way to inspect and manage the account directly.

## Owner involvement

No new subscription, account, bot, coding assistant or reinstall is currently indicated. Leave the existing receiver as-is. The immediate review artifact is source-only and offline. The eventual Windows certification should be consolidated into one command, with actual real transactions and final owner authorization separated. If Windows cannot run or provider evidence is incomplete, report the precise reason and keep live disabled. No claims of overnight background work or automatic activation.
