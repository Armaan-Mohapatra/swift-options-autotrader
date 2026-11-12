# Swift Trades AutoTrader — 48-Hour GitHub Development Sprint

**Status:** Implementation instructions only. This document is not proof that the repository, software, broker connectivity, or tests have been completed.

**Target repository:** `Armaan-Mohapatra/swift-options-autotrader` (PRIVATE; check whether it already exists before creating it).
**Development workflow:** ChatGPT Work with GitHub access, one phase at a time. GitHub is for source control and CI; the existing Windows machine is the production host after separate authorization and installation with Codex.
**Deadline:** Aim to complete all GitHub coding phases within 48 hours. Do not trade away test coverage to hit the deadline. Report honest blockers and distinguish code-complete from field-validated.
**Default mode:** `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`.

## A. What the system does, in simple terms

Swift posts a Telegram options alert. The listener saves it. The interpreter determines whether it's a new options entry, an average update, an additional buy instruction, or an exit. The order engine places a *paper* (and only after separate go-live approval, real) buy-limit order at Swift's original alert price and waits up to 60 elapsed minutes from broker acceptance. It cancels any unfilled remainder when the deadline arrives. When Swift announces a partial or full exit, it tries to close **all** of our filled quantity for the exact corresponding contract. It always verifies order status and actual fills.

### Authoritative behavior rules

1. **OPTIONS ONLY:** Ignore outright stocks, crypto, futures, shares, and general market commentary. Long single-leg call/put entries are the starting scope. Do not convert a stock alert into an option trade.
2. **LOTTO IS NORMAL:** `LOTTO` is an option-alert label, not an exclusion or a modified entry rule. Same contract validation and broker-required checks as any other option.
3. **ENTRY AT SWIFT LIMIT:** Parse and submit the original numeric entry price as a buy limit, not the current bid/ask, never chase or reprice upward because of market movement. The broker may execute a buy immediately at or below that limit. Respect option tick-size and broker constraints; never silently change invalid prices.
4. **ONE-HOUR RESTING:** For an accepted entry, persist `accepted_at_utc` and `expires_at_utc=accepted_at_utc+3600 seconds`. On timeout, request cancellation of only unfilled quantity, then verify state to handle cancel/fill races. If it fills partially, keep and manage the actually filled position. If the broker terminates it sooner at market close, do not recreate it automatically next session.
5. **NO ADDS:** `ADDING MORE`, `ADD`, `AVERAGING DOWN`, `SCALE IN`, etc. must never open additional positions by themselves.
6. **NEW AVERAGES:** Record Swift's revised or new average and link it to the correct signal, but do not buy again, alter the pending limit, or overwrite our actual average cost (which comes only from our confirmed fills).
7. **FULL EXIT ON PARTIAL EXIT:** Any `SOLD HALF`, `SOLD 1/3`, `TRIM`, `SCALED OUT`, `RUNNERS OUT`, `ALL OUT`, or other explicit partial/full sell command means try to exit 100% of our *matching filled quantity*. If entry is still pending, cancel remaining quantity first and sell any already-filled part.
8. **EXACT MATCHING:** Associate exits and average updates using source message/reply relationships and full option identity (underlying, call/put, strike, expiration) plus signal lineage, not ticker alone. If ambiguous, alert for review; never close unrelated holdings.
9. **IDEMPOTENCY:** Never duplicate opening or closing orders because Telegram redelivers, a message is edited, a process restarts, or Robinhood times out. Persist logical order IDs and reuse an idempotency key for a retry of the *same* intended order.
10. **SAFETY / BROKER REQUIREMENTS:** Validate exact option instrument, supported market session, account permissions, available buying power, unexpired date, configured order size, and mandatory risk ceilings. These are not price-chasing or LOTTO exclusions. If absent, reject with an explicit reason. Do not bypass Robinhood trade-approval requirements.
11. **CLOSE POLICY:** Provide configurable sell-to-close order mechanics and follow-up alerts. A submitted exit is not a completed exit; continue tracking until filled. Do not assume an unfilled limit sale makes the position flat.
12. **MARKET HOURS:** All timestamps in UTC internally; present Eastern market time on dashboards. After-hours untradable entries do not silently turn into next-day buys. A market-close forced cancellation is legitimate. Never replay an old entry as new after reconnect.
13. **NO LIVE UNTIL APPROVED:** Default to paper-only; all integration/CI tests use mock broker responses. Require separate human permission and local credential enrollment to activate real brokerage writes.

### Acceptance examples

- `SPY 680C 11/13 @ 1.20` → one buy-to-open limit order at 1.20 (provided that symbol, expiration year, and contract are confirmed).
- `SPY LOTTO 680C 11/13 @ 1.20` → same behavior as the normal option entry.
- `SPY adding more, new avg .95` → update Swift metadata only; zero new buys; our entry limit stays unchanged.
- `SPY SOLD HALF` → full close of our matching filled position; cancel any pending entry remainder.
- `SPY RUNNERS OUT` / `ALL OUT` → same full-close behavior; duplicate exits are no-ops.
- `BUY 20 SHARES SPY` → ignore; zero option orders.
- Duplicate alert/restart → no duplicated buy, cancel, or sell.
- A 59-minute-old pending entry remains open; a 60-minute-old accepted entry triggers cancellation verification.
- At expiry, if 2/3 contracts were filled, cancel only 1 unfilled and manage 2 held contracts. There must be tests for a fill racing with cancellation.

## B. Architecture

Python 3.11+; `uv` or pinned pip dependency lock; `Hydrogram` Telegram MTProto client; `mcp` Python SDK as the candidate official Robinhood MCP connector; SQLite in WAL mode with migrations and robust backup/recovery; FastAPI/Starlette optional localhost-only dashboard; structured JSON logs; `pytest` and optional property-based tests. Verify each dependency's actual version, source, license, and API before coding. Prefer small modules, finite timeouts, backoff, and minimal CPU/RAM.

Flow: `telegram_listener → raw_event_store → deterministic_signal_parser → signal_linker → decision/risk_gates → durable_order_state_machine → paper_or_mcp_broker_adapter → reconciliation → notifications/dashboard`.

### Suggested repository layout

```
swift-options-autotrader/
  AGENTS.md
  README.md
  pyproject.toml
  uv.lock                 # if uv used
  .env.example            # names/placeholders only
  .gitignore
  .github/workflows/ci.yml
  docs/PROJECT_SPEC.md
  docs/IMPLEMENTATION_STATUS.md
  docs/SECURITY.md
  docs/WINDOWS_RUNBOOK.md
  docs/LIVE_ACTIVATION_CHECKLIST.md
  docs/OPEN_BLOCKERS.md
  docs/PHASE_REPORTS/
  src/swift_autotrader/
    config.py
    app.py
    telegram/             # client, events, checkpoint, reconnect
    parser/               # rules, patterns, ambiguous message handling
    signals/              # identity, lineage, context, transitions
    database/             # schema, repository, migrations, backups
    orders/               # commands, state machine, deadline watchdog
    broker/               # interface, paper, robinhood_mcp, mock
    analytics/            # metrics, replay, simulated fills
    dashboard/            # local status, activity, stop controls
    notifications/        # informational alerts only
  tests/fixtures/         # synthetic or approved sanitized snippets
  tests/unit/
  tests/integration/
  tests/replay/
  deployment/windows/     # install/uninstall/start/stop/healthcheck, not run yet
  scripts/                # smoke and verification CLI
```

### Operational requirements

- Only the designated Swift channel and authorized Telegram user session are monitored. Verify channel/chat identifiers before ingesting. Do not work around paid access, rate limits, or Telegram restrictions.
- Telegram credentials, session files, token caches, account IDs, and raw paid content must never be committed. Use `.env.example` with placeholders and a `secrets` scanner. Never output secrets in GitHub Actions logs.
- Configuration is validated on startup; all broker writes disabled by default, including on reconnect and software upgrade. No unattended credential creation in CI.
- Durable, monotonically progressing state machine: `SEEN → VALIDATED → ENTRY_READY → ENTRY_SUBMITTED → OPEN/PARTIAL/REJECTED/CANCEL_PENDING/CANCELED → CLOSE_REQUESTED → CLOSE_SUBMITTED → CLOSED/PARTIAL_CLOSE/ALERT_REQUIRED`. Exact implementation may vary, but no duplicate submission and no false fill claims.
- Persist submission key before issuing a broker write. After network ambiguity, reconcile before deciding whether retry is safe.
- Dedicated Robinhood external-agent account only, subject to Robinhood approval. Support read-only and mock modes without credentials. Live connection/auth and allowed actions cannot be assumed until tested on the intended machine.
- Local Windows process supervisor: Task Scheduler or a reviewed Windows service wrapper, with auto-start, retry, single-instance lock, stop/status health commands, SQLite durability, logfile rotation, and controlled shutdown.
- Keep this completely separate from any existing Kalshi/Phoenix or other trading applications. No shared database, credential store, process controls, or production runtime changes.
- Report status as `NOT_STARTED`, `CODE_COMPLETE`, `CI_PASSED`, `REQUIRES_LOCAL_AUTH`, `FORWARD_TESTING`, or `LIVE_APPROVED`. No claiming live validation from mocks.

## C. Phased GitHub workflow, 48-hour goal

Submit **one prompt at a time** to the *same ChatGPT Work conversation*, connected to GitHub. Phase 0 creates the repository and commits the permanent spec; later phases read that spec instead of relying on conversation memory. Once a phase ends, review its GitHub commit/PR/CI links before sending the next prompt. Only merge if the phase's acceptance tests are actually green and changes stay paper-only. All phases are timeboxed collectively to a 48-hour **target**, not independently assigned several days.

### PHASE 0 — GitHub repository and foundation

**Required output:** Private repository under the user's authorized GitHub account, README/AGENTS/spec, Python package skeleton, test + Windows/Linux CI, security boundaries, phase tracker; verified commits. If repository creation isn't supported by the available tools, ask for the single missing action and do all other feasible work without pretending it exists.

**Prompt:**

> Act as the lead software engineer and repository maintainer for `Armaan-Mohapatra/swift-options-autotrader`. You are operating in ChatGPT Work with GitHub available. Begin the 48-hour-target, phase-based development sprint. Start PHASE 0 ONLY. Check whether a repository of that name already exists; preserve existing work and create it as PRIVATE only if missing and the tools permit. Never modify Phoenix/Kalshi or any other repo. Implement a Python 3.11+ repository skeleton and the full immutable requirements in `docs/PROJECT_SPEC.md`: ONLY options; LOTTO permitted; ignore add/average-down purchases; store NEW AVG separately from fill basis; SOLD HALF/TRIM/RUNNERS OUT/ALL OUT means close all matching filled contracts; exact original buy-limit price, no chase filter; unfilled remainder cancel and verify 3600 seconds after broker acceptance; safe handling of partial fills, message edits, out-of-order exits, account/broker rejection; no live exchange writes. Create `AGENTS.md`, `README.md`, `.env.example`, `.gitignore`, `docs/IMPLEMENTATION_STATUS.md`, `docs/OPEN_BLOCKERS.md`, `docs/SECURITY.md`, `docs/WINDOWS_RUNBOOK.md`, configuration schema, typed interface stubs, a trivial CLI smoke command, initial pytest tests, and `.github/workflows/ci.yml` for Linux and Windows. Prefer audited, licensed dependencies; verify actual current package APIs. Create feature branch/PR and run CI. Do not authenticate to Telegram or Robinhood and do not place any trade. At the end give exact repository link, branch, commit SHA, PR URL, CI URLs/status, files created, remaining blockers, and acceptance results. Stop after PHASE 0.

### PHASE 1 — Telegram listener, persistent event store

**Required output:** Legitimate authorized Telegram user client, precise chat filter, read-only message capture, durable message/edit records, checkpoints, reconnect and deduplication, synthetic test fixtures. No broker orders.

**Prompt:**

> Continue the GitHub Swift AutoTrader project with PHASE 1 ONLY. First read `docs/PROJECT_SPEC.md`, `AGENTS.md`, last phase report, and current code. Implement an authorized Hydrogram-based Telegram MTProto listener with configurable allowed Swift channel ID, new messages, edits, replies and source metadata. Store raw messages, timestamps, update versions and unique IDs in SQLite with migrations; implement deduplication, reconnect/backoff, restart recovery, and bounded event processing. Do not pull or commit raw paid Telegram data into GitHub; use synthetic/redacted fixtures and a mocked Telegram test client in CI. The listener must be read-only and must NEVER send messages, make trades, authenticate with real secrets in GitHub, or replay old alerts as new. Implement health and backlog indicators, graceful shutdown, structured logging and test cases for duplicate delivery, reconnect, edits, out-of-order messages, empty messages and database restart. Connect it to a dry-run CLI. Open a phase PR, run Windows and Linux CI, fix defects, and publish a phase report with exact commit/PR/CI links, test counts, limitations and next-phase handoff. Stop after PHASE 1.

### PHASE 2 — Swift-specific parser and signal lifecycle

**Required output:** Deterministic, tested signal interpretation, options identity, average updates, context links, exits to 100%, ambiguity handling, no add trades.

**Prompt:**

> Continue PHASE 2 ONLY of the private `swift-options-autotrader` GitHub repo. Read and obey its permanent specification, implement the actual Swift-specific parser and message-to-position association engine, and add a large set of synthetic examples. Parse new options BUY signals with symbol, put/call, strike, expiration, limit premium, alert category and reply/message IDs; establish the year by validated contract existence rather than blind guessing. Ignore all non-options. LOTTO must be treated as a normal eligible options category. ADDING MORE/ADD/AVERAGING DOWN/SCALE IN are metadata only and never place a new buy. NEW AVG/NEW AVERAGE updates Swift's reported average without touching our cost basis or original resting limit. SOLD HALF, SOLD 1/3, TRIM, SCALED OUT, RUNNERS OUT, ALL OUT and equivalent explicit exits map to CLOSE_100_PERCENT for our exact matching position. Match by option identity and message lineage; ambiguous ticker-only follow-ups go to REVIEW_REQUIRED, never the wrong position. Implement message edits, corrections, duplicates, multiple simultaneous contracts in one ticker, delayed events, regex tests, and explicit human-review reasons. No external LLM may make a final ambiguous execution decision. Run CI, fix failures and show actual normalized parsing fixtures and a full phase report. No live orders. Stop after PHASE 2.

### PHASE 3 — Exact-price order engine and paper execution

**Required output:** Entry/exit state machine, 60-minute accepted-order deadline and cancellation verification, partial fill support, durable idempotency, a realistic mock paper brokerage, crash recovery and kill switch.

**Prompt:**

> Continue PHASE 3 ONLY after reading repository spec, code and previous phase reports. Implement a durable single-leg option order-state machine using an abstract Broker interface and a deterministic PAPER broker. New eligible signals submit one buy-to-open LIMIT at the *exact* Swift alert price, even if the market is above or below it; do not add chase percentage filters or category exclusions for LOTTO. Never silently round an invalid tick, invent an option contract, exceed configured caps, or bypass the brokerage's requirements. Persist an order intent/idempotency key before submission. When broker acceptance is confirmed, schedule cancellation for remaining unfilled quantity at accepted_at_utc + 3600 elapsed seconds; verify final state and handle fill/cancel races. GFD/market closing may cancel earlier; do not recreate orders next day. If an exit message arrives while entry is open, cancel its remainder and close 100% of confirmed fills. If Swift says SOLD HALF/TRIM/RUNNERS OUT/ALL OUT, close all matching filled contracts, not a fraction. Test full/partial fills, no fills, double callbacks, disconnected network, stale state, restart in mid-timer, duplicate alert, order rejection, manual pause, kill switch and order reconciliation. Implement configurable safe sell-to-close policy and alert on unfilled exits. All tests must use paper/mock; real broker writes must be impossible in CI. Create a phase PR, run/fix CI, provide proof and stop after PHASE 3.

### PHASE 4 — Official Robinhood MCP adapter and preflight checks

**Required output:** Actual code implementing an MCP client adapter with mock contract tests, read-only account discovery, exact option lookup, quotes, order review, lifecycle methods, safe auth scaffolding; no actual sign-in or live order in GitHub.

**Prompt:**

> Continue PHASE 4 ONLY. Inspect Robinhood's current official Agentic Trading / Trading MCP documentation and the installed MCP Python SDK; do not use unofficial Robinhood scraping or private APIs. Implement `RobinhoodMCPBroker` behind the same Broker interface used by the tested paper engine. Support Streamable HTTP, supported OAuth/authentication hooks, non-secret configuration, optional read-only initialization, correct option chain/instrument resolution (ticker, expiry, strike, call/put), current quotes, account and options-level checks, buying power, review_option_order, place_option_order, get_option_orders, get_option_positions and cancel_option_order as documented, with schema-validated requests/responses. Never guess tool schemas; validate actual advertised capabilities on a real client only when credentials are separately available. On unknown status, reconcile by logical idempotency key and account/order history before retrying. Respect broker approval prompts and use the designated external-agent account only. Make LIVE_ENABLED and ALLOW_BROKER_WRITES both false by default and disallow real submissions in CI and development; use mocked HTTP/MCP fixtures for every write-path test. Treat any capabilities requiring local authorization as REQUIRES_LOCAL_AUTH, not CODE_COMPLETE+VALIDATED. Document multi-leg as out of scope initially. Add contract/integration tests, a read-only diagnostics CLI, phase PR/CI proof and a detailed local-auth handoff. No real trades. Stop after PHASE 4.

### PHASE 5 — Historical replay, reporting and dashboard

**Required output:** Research and performance tools, repeatable replay of sanitized historical messages, order lifecycle visibility, local dashboard and notifications, no data fabrication.

**Prompt:**

> Continue PHASE 5 ONLY in `swift-options-autotrader`. Implement deterministic historical and real-time paper replay using the recorded messages and time-aligned prices when available. Differentiate Swift's original alert price and NEW AVG from our actual simulated fills; calculate signed P&L only from confirmed simulated trades, underlying option contract multiplier, realistic price/slippage/spread assumptions and known fees. Where exact bid/ask history is missing, show UNVERIFIED_ESTIMATE, not a fabricated precise result. Build a localhost-only read-only dashboard showing Telegram connection, paper/live mode, last message, order state, countdown to 60-minute cancel, fills, matching exits, ignored alerts, Swift averages, actual average cost, open exposure, realized P&L, skipped signals and software errors. Add notifications for rejected entries, unfilled exits, broker outage, stop conditions and service failure with secrets redacted. Test replay determinism, missing market data, time zones and daylight-saving changes, no-lookahead behavior and position/P&L reconciliation. Maintain PAPER_ONLY. Open PR, run/fix Windows/Linux CI, publish test evidence and phase report; stop after PHASE 5.

### PHASE 6 — Windows 24/7 installer and operational package

**Required output:** Safe, idempotent deployment scripts and docs for the existing Windows computer only, but DO NOT install anything on that machine during this GitHub phase.

**Prompt:**

> Continue PHASE 6 ONLY. The target runtime is the user's existing Windows PC that already stays on 24/7; no cloud hosting, VPS, or monthly infrastructure service. Do not touch the user's laptop or production apps yet. In GitHub, implement a Windows-native deployment package with PowerShell scripts for prerequisite checks, verified dependency installation, create secure app/data/log directories, run database migrations, start/stop/status/uninstall, backup/restore, log rotation, local firewall and health checks, service restart on failure, and auto-start through Task Scheduler or a justified service wrapper. Provide a dry-run mode that makes no changes. Use a configurable installation root defaulting to a separate dedicated location, never shared with Kalshi/Phoenix. Support a single-instance lock, crash recovery, graceful shutdown and resume from persisted order states. Provide `docs/WINDOWS_RUNBOOK.md`, `docs/OPERATIONS.md` and `docs/ROLLBACK.md`, including initial Telegram user-session authorization and Robinhood OAuth as manual local steps. Build Windows CI using mocked credentials and exercise install/uninstall dry-runs, without changing production or enabling live trades. Document minimal RAM/disk/CPU expectations as measured, not guessed. Open phase PR, fix CI, report and stop after PHASE 6.

### PHASE 7 — Integration audit and release handoff

**Required output:** One coherent code-complete, paper-ready application with CI evidence, documented remaining local authentication, installation and forward-validation gates. Create a GitHub pre-release/tag if tests pass; do not claim it is live production ready.

**Prompt:**

> Execute PHASE 7 FINAL INTEGRATION AUDIT for `swift-options-autotrader`. Read all code, docs, phase reports, PRs and CI artifacts. Do not begin unrelated features or change production. Run a complete simulated end-to-end scenario: receive Swift Telegram option alert → parse → exact-price single-leg limit entry → verify broker accepted timestamp → wait/replay 3600-second timeout → cancel unfilled remainder and verify status; separately test filled contract, partial fill, NEW AVG without added buy, LOTTO same as regular option, SOLD HALF closing ALL of our filled contracts, RUNNERS OUT/ALL OUT, same ticker different strikes, message edits, retries and crash recovery. Assert non-options always ignored, invalid/ambiguous contracts blocked, and LIVE writes impossible by default. Run full pytest, lint, type-check, dependency/license/security scan, Windows/Linux GitHub CI; inspect PR diffs for tokens, private paid messages, credentials and unintended files. Close only truly verified issues; list every unverified area explicitly. Finalize README, configuration reference, test results, changelog, Windows install/rollback instructions, release checklist, and `docs/IMPLEMENTATION_STATUS.md`. Create a paper-only release/tag and GitHub checkpoint commit if and only if all mandatory CI tests pass; otherwise report a blocker and don't mark the release ready. Provide repository, branch, commit, release/PR/CI links, exact passed/failed/skipped test counts, currently implemented vs mocked capabilities, outstanding local-login tasks, forward-paper validation requirements and go-live approval checklist. Do not connect real credentials, install on Windows, merge/deploy without authorization, or place live orders. Stop for user review.

## D. What to do after Phase 7 (SEPARATE later Codex/local session)

1. On existing Windows computer, use Codex to clone a verified GitHub release to a dedicated directory; inspect file hashes and release.
2. Run PowerShell install scripts in DRY_RUN then with explicit authorization. Set `PAPER_ONLY=true`, `LIVE_ENABLED=false`.
3. Authenticate to the authorized paid Telegram channel locally; place secret material in Windows-protected storage, outside the repository; never paste tokens into ChatGPT or GitHub.
4. Authenticate to official Robinhood Trading MCP for the **dedicated** externally tradable account, locally. Verify read-only connections and that real OAuth refresh survives service restarts. If the required supported headless auth isn't viable, keep paper-only and report a blocker.
5. Validate the on-machine pipeline with real incoming Swift messages but mock/paper orders, confirmed time stamps, bid/ask data, and live order review where permitted without placement.
6. Run forward paper tests over enough representative alerts. Inspect actual parser accuracy, execution timing, missed fills, cancel/fill races, stopped/restarted recovery, and daily P&L. Do not infer profitability from small samples.
7. Only after a separate explicit go-live approval, set order size and risk ceilings, permissions and emergency-stop procedure; require a guarded low-size first live test, verify status and order fills, and retain manual kill switch. There is no automatic go-live as part of this specification.

## E. Requirements for every GitHub phase report

- Phase and status: `IMPLEMENTED`, `TESTED`, `MOCK_ONLY`, `BLOCKED`, `REQUIRES_LOCAL_AUTH`.
- Exact repository, commit SHA, branch, pull request and Windows/Linux Actions URLs.
- Specific files modified, design choices, tests run and outcome with counts.
- Material safety/security/licensing findings, missing source data, and unverified claims.
- Exit criteria of current phase, evidence they passed, and specific next prompt to run.

## F. Public references (verify versions at implementation time)

- Robinhood external agent onboarding: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Robinhood agent trading capabilities and restrictions: https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Hydrogram documentation: https://docs.hydrogram.org/en/latest/intro/quickstart.html
- GitHub Actions Python CI: https://docs.github.com/en/actions/tutorials/build-and-test-code/python
- Windows Task Scheduler restart: https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-restartonfailure-settingstype-element

---
*This plan is a software engineering deliverable. It does not assert that trades have been placed or that any trading strategy is profitable.*
