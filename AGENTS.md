# Maintainer / agent instructions — binding

## Scope and precedence

- Read `docs/SOURCE_SPRINT_PLAN.md` **in full**, `docs/PROJECT_SPEC.md`, and the previous report in `docs/PHASE_REPORTS/` before changing code.
- The sprint source is authoritative. `docs/PROJECT_SPEC.md` is the permanent project contract. If the source and implementation conflict, **stop and report**, do not quietly reinterpret.
- Only implement the phase explicitly authorized by the user. Phase 0 provides the foundation, Phase 1 implements read-only authorized local Telegram ingestion and Phase 2 implements deterministic Swift-style parsing/SQLite signal matching with synthetic tests. Phase 3 implements a local-only synthetic paper broker, durable order engine and regression tests. Real brokerage auth, live writes, dashboard and Windows installation remain future gates. Phase 2 interpretation does **not** authorize any real trade.
- Never access a personal computer or modify unrelated trading software, repositories, data, processes, credentials or cloud resources.

## Mandatory trading semantics

1. Options only, initially long single-leg call/put. Outright shares, crypto, futures, commentary ignored.
2. LOTTO is allowed on the same terms as other eligible option entries.
3. New entries use **exact original Swift numeric limit**, not quotes or price-chasing. Invalid tick/contract/market/session/account checks must reject transparently.
4. Acceptance time is UTC; expiry is **acceptance + 3,600 seconds**; cancel **remaining unfilled** quantity and reconcile possible late fills. Never reopen next session without new authorization/signal.
5. ADD / ADDING MORE / AVERAGING DOWN / SCALE IN never buy again. NEW AVG records Swift's average separately from actual confirmed weighted fill basis; it never modifies open limit.
6. SOLD HALF, SOLD 1/3, TRIM, SCALED OUT, RUNNERS OUT and ALL OUT request **100% liquidation of own matching filled contracts**, with pending-entry cancellation and confirmation tracking.
7. Match exact symbol, right, strike, expiry, message ancestry and signal lineage. Ticker-only ambiguous updates or exits require human review.
8. Durable order intent with stable idempotency key **before** external write; network uncertainty reconciles before retry. Never infer filled/closed from merely submitted orders.
9. Risk, permissions, buying power, order size, market hours, valid option existence and human approval are **mandatory gates**, not discretionary price filters.
10. `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false` are enforced. A phase PR, CI result or model response never authorizes real trading.

## Development, verification and security

- Python 3.11+, typed modules, isolated state, finite timeouts, clear error paths, no implicit retry that can duplicate orders.
- Make migrations append-only, preserve prior schema history, test restart/idempotency, keep SQLite WAL and safe backups. Source content and credentials stay out of Git history and logs.
- Third-party packages require validated license/version/source and documented rationale. Prefer narrow interfaces and minimal runtime dependencies.
- CI uses ONLY synthetic data and mock brokers. Run unit/integration/regression tests on Windows/Linux; record passed/failed/skipped counts **and links**. Never claim CI passed without provider evidence.
- Secret-sensitive files: `.env`, MTProto `*.session`, account/refresh tokens, raw paid Telegram text, identifiable channel/account IDs, brokerage history. Do not commit them, print them, or embed them in workflow variables.
- Review all diffs for inadvertent live code paths, future phase scope creep, secret materials, dependency licenses and unrelated files.
- Never merge or deploy without separate approval. Do not install on the 24/7 Windows machine until Phase 6 and explicit user authorization.

## Phase-end gate

- Report exact repository URL, branch, full SHA, PR URL, Linux/Windows Actions URLs and actual conclusions; if unavailable mark **BLOCKED**, not green.
- Report changed files, public dependencies, verified vs stub features, local commands, test counts, security/licensing limitations, blocked requirements, and next prompt.
- Stop after the named phase and await review.

## Phase 2 verified boundaries

- See `docs/PHASE_REPORTS/PHASE_2.md` and `docs/PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json` for 81 synthetic classified alerts and GitHub CI. Synthetic test agreement is not real Swift accuracy.
- An unverified or absent option instrument catalog must produce `REVIEW_REQUIRED`, never a fabricated expiration year or trade-eligible signal. Same-ticker ambiguity is never resolved by intuition.
- A stored `CLOSE_REQUESTED` is just an intent; Phase 3 must independently verify actual filled quantity and lifecycle, with all external writes disabled.
- Maintain stacked draft PR review order: Phase 0 #1, Phase 1 #10, Phase 2 #11. Do not merge without separate explicit authorization.

## Phase 3 enforced boundaries

- Use **only** concrete `PaperBroker` with the engine. Robinhood MCP remains deny-only, and `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false` are immutable for the sprint.
- Source decisions must already be durable, fresh, uniquely matched Phase 2 events with a **verified instrument catalog**. Empty/unverified catalogs fail closed. The standalone CLI `paper-cycle` must not permanently block real messages merely because there is no provider.
- Persist logical intent and idempotency identity before any simulated submission. Unknown outcomes reconcile against persisted broker journal and never trigger a new buy, price or identity.
- Use exact source limit and exact option contract, not a current quote or percentage chase filter; LOTTO receives no altered policy. Broker acceptance, not message receipt, starts the one-hour cancel.
- On any explicit partial/full exit, cancel resting entries and verify terminal status before sending an at-most-once exit intent covering **100%** of the actually filled balance. An unfilled exit stays exposed and alerts.
- Strong stop/pause guards, local OS single-cycle lock, bounded fresh quote policy and fail-closed risk ceilings are required. No fake fills, no claim of live market accuracy.
- Keep Phase 3 PR #12 draft and stacked on unmerged Phase 2 PR #11; no merges, account auth, user-PC installs, or deployment. Phase 4+ needs separate explicit authorization.

## Phase 4 read-only MCP safety

`RobinhoodMCPBroker` may inspect supported official tool data and build inert
future-write request payloads; it must **never call** `place_option_order` or
`cancel_option_order` during GitHub phases. OAuth must remain local, opt-in and
outside CI. Do not weaken the Phase 3 concrete-PaperBroker guard or existing
entry/exit rules. Consult `docs/ROBINHOOD_MCP_PHASE4.md` for known blockers.

## Phase 5 safe research and monitoring

- Any historical replay MUST initialize a brand-new temporary SQLite database and pass messages through existing TelegramStore → SignalMatcher → PaperOrderEngine → PaperBroker. Never import historical fixtures into an active runtime or turn recovered messages into fresh production orders.
- Fill assertions require contemporaneous option BID/ASK evidence and modeled size under a declared execution assumption, never alert text or option high/low alone. No synthetic quote becomes a real exchange fill. Preserve full FIFO trade/fill history.
- Keep exact entry limit, broker acceptance plus 3,600 elapsed seconds, no adds, normal LOTTO handling, Swift NEW AVG separate from actual fill basis and 100% matching exits unchanged. Enforce stop/paused state and risk ceilings.
- Missing fees, quotes, timestamp provenance or fill evidence must produce unavailable or UNVERIFIED_ESTIMATE, not invented precise profits.
- Local dashboard binds only loopback and stays read-only, escaping sensitive content and hiding private paid Telegram bodies; all notifications are metadata-only and local, never outbound.
- User host, Robinhood OAuth, production credentials, real paid alerts and deployment are outside Phase 5. GitHub CI currently cannot start runners; mark Phase5 CI_BLOCKED until actual jobs execute.

## Phase 6 Windows deployment and authorization boundary

- Scope is **only** a Windows package and synthetic runner-rehearsal tests. Never run the installer against a person's real Windows host, alter Phoenix/Kalshi tasks/processes/files or enroll real credentials during GitHub phases.
- Preserve the package's `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false` and no real broker write invariants, and require a clean reviewed full SHA before any future `setup.ps1 -Apply`.
- `-DryRun` must never create files/tasks/virtualenvs; `-NoSchedule` tests must stay in fresh isolated directories. Stop rather than overwrite an unrelated scheduled task or any existing data. Use Windows-user DPAPI and restrictive local NTFS ACLs; never commit or log session/token details.
- The Windows supervisor starts only the private read-only dashboard, optional legitimately authorized read-only Telegram client and optional metadata-only outbound operator status. It MUST NOT automatically enable a paper order cycle that would mark authentic alerts blocked without a verified instrument catalog, fresh quotes and risk preflight. No cloud services required.
- CI for Phase 6 is **BLOCKED** (runner allocation fails before steps); 24 local focused Python tests are not a substitute. Do not merge, tag, deploy, claim release-ready, sign off Windows compatibility or progress to Phase 7 until separately instructed and all release/field gates pass.


## Phase 7 final audit controls

- See `docs/ARCHITECTURE.md`, `docs/PHASE_REPORTS/PHASE_7.md`, and `CHANGELOG.md`; do not label Phase 7 CI_PASSED or RELEASED while GitHub Actions jobs have zero runner allocation or no executed steps.
- Run the full inherited Windows/Linux Python 3.11/3.13 matrix, isolated Windows PowerShell/NoSchedule install, and offline audit on the **same exact final SHA**; earlier Phase 4 green checks cannot validate Phase 5–7 source.
- Preserve exact Swift original limit, broker acceptance + 3,600 seconds, no adds, Swift average separation, close 100%, and no market-price invention. The paper engine must accept only a concrete PaperBroker; MCP trade/cancel functions remain hard-denied.
- No source-content, credential, host-machine, task-scheduler, release-tag, deployment or live-trading side effects during the GitHub audit. Local operator steps remain separately approved.
- The Windows supervisor does not automate source-to-paper execution; do not claim unattended 24/7 paper trading until independently verified instrument/quote/preflight wiring and forward validation are reviewed.


## Phase 8 review-only source boundary

The optional `paper_dispatch.py` is a separately supervised PAPER-only worker, disabled by default. It must never connect a Robinhood live order/cancel method to the engine. The only execution broker permitted is concrete `PaperBroker`. Unavailable contract/quote/tick/depth/calendar/approval evidence must preserve unparsed alerts and run only the pending-order watchdog. Never call the parser with an empty catalog during provider outages. No strategy rule changes, hidden quote size defaults or guaranteed fill claims. Code is a candidate until native Windows/Linux CI, optional MCP transitive lock, official real read-only tool schemas and owner approval exist. See `docs/PAPER_DISPATCH_PHASE8.md`.
