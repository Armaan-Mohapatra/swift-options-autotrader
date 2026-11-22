# Maintainer / agent instructions — binding

## Scope and precedence

- Read `docs/SOURCE_SPRINT_PLAN.md` **in full**, `docs/PROJECT_SPEC.md`, and the previous report in `docs/PHASE_REPORTS/` before changing code.
- The sprint source is authoritative. `docs/PROJECT_SPEC.md` is the permanent project contract. If the source and implementation conflict, **stop and report**, do not quietly reinterpret.
- Only implement the phase explicitly authorized by the user. Phase 0 provides the foundation, Phase 1 implements read-only authorized local Telegram ingestion and Phase 2 implements deterministic Swift-style parsing/SQLite signal matching with synthetic tests. Phase 3 implements a local-only synthetic paper broker, durable order engine and regression tests. Real brokerage auth, live writes, dashboard and Windows installation remain future gates. Phase 2 interpretation does **not** authorize any real trade.
- Never access a personal computer or modify unrelated trading software, repositories, data, processes, credentials or cloud resources.
- **Owner live-trading authorization (see "Owner live-trading authorization" below) supersedes the PAPER-only and "live trades forbidden" statements elsewhere in this file**, within the guards that section keeps.

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
10. `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false` are the enforced defaults; only the owner's explicit local configuration change (see "Owner live-trading authorization") moves them. A phase PR, CI result or model response never authorizes real trading by itself.

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


## Phase 8 unattended PAPER safety gate

Phase 8's opt-in worker may only call PaperBroker through the existing PaperOrderEngine, with strict verified read-only contract/quote evidence. Unavailable market data must preserve source events; allow_entries=False must still reconcile/cancel existing resting orders. Never run the CLI's diagnostic paper-cycle without a verified provider. Do not treat mocked MCP schemas or synthetic quote data as evidence of authentic broker market fields. The supervisor enables the paper worker only after explicit local configuration; live trades always forbidden. Final full CI, locked optional SDK and native Windows tests remain release blockers. Refer to docs/PRE_CODEX_HANDOFF.md.

## Owner live-trading authorization

The repository owner authorizes live trading of this system on the dedicated Robinhood MCP (Agentic) account, through `broker/live_broker.py` (`LiveBroker`) only. This supersedes the "paper only" and "live trades forbidden" statements in the sections above. Valid copy-trade commands should execute; do not add discretionary price filters, extra confirmations or manual-review steps beyond the guards below.

- A **valid command** is an original ENTRY alert that the real parser and lineage recorded as `ENTRY`/`RECORDED` with a verified contract, entered at the exact original Swift limit for one contract; or an explicit exit that liquidates 100% of the confirmed filled quantity of that signal's own entry. Ticker-only ambiguity, ADD / ADDING MORE / AVERAGING DOWN / SCALE IN and NEW AVG never buy.
- **Guards that stay mandatory and may not be removed or bypassed:**
  1. The **kill switch**: the engine's `paper_engine_control` pause / emergency stop blocks every new entry (`engine_kill_switch`); exits and cancels of owned orders stay allowed. Reset only with explicit manual approval.
  2. **Duplicate-order protection**: a durable journal written before any write, stable `ref_id` from the idempotency key, one entry per signal, reconciliation before any write, and **no automatic resubmit** after an uncertain outcome.
  3. LIVE has **no daily net-loss breaker** (owner decision, Issue #42; a v3 receipt must acknowledge `NO_DAILY_LOSS_STOP`). LIVE losses may exceed $750 and existing positions are not closed by this policy. The $999 premium ceiling and one-contract limit, read from `risk_limits.py`, stay mandatory. Buying power is the only other bound on trade count. PAPER keeps its separate $750 daily circuit breaker.
  4. Entry expiry at 3,600 seconds from acceptance (cancel the unfilled remainder), dedicated-MCP-account pinning, tick/multiplier checks and the broker's own order review.
- Live writes are controlled **only by the owner's local configuration** (`LIVE_ENABLED`, `ALLOW_BROKER_WRITES`, `PAPER_ONLY`); an agent must not flip them on its own, and the owner's explicit go-ahead is required for each change. CI and tests remain synthetic-only with fake brokers and never contact Robinhood.
- Keep the existing safety rules that this section does not name: exact original limit (no price chasing), 100% matching exits, no adds, secrets out of Git and logs.

## AI token-efficiency rules (additive, subordinate to the safety contract)

These rules reduce wasted context, not required diligence. They never override the trading, security, testing, provenance, approval, or release gates elsewhere in this repository.

- Follow only the current explicitly authorized scope. Do not start a new Goal, spawn agents, run autonomous continuations, or expand the task without specific approval.
- Read mandatory authority documents when required, but avoid re-reading unchanged material within the same task. Use `docs/AI_HANDOFF.md` as a navigation aid, **not** as proof of current state or a replacement for the authoritative project files.
- Locate relevant symbols/files first. Prefer scoped search and file ranges to dumping entire directories, histories, JSON payloads, logs, or test output.
- Keep routine commentary and the final report concise. Preserve exact errors, exit codes, failure evidence, original logs, and relevant SHA/path references.
- Run targeted validation first. Run the full regression/security/release matrix whenever the existing contract requires it, against the required exact SHA. Never skip a mandatory safety gate or relabel untested work as passed.
- Do not rerun unchanged, already-verifiable checks merely to generate more output. Re-run after changes that invalidate that evidence.
- Do not create paid GitHub Actions activity. Do not install hooks, local packages, proxies, model routers, cloud services, or token-saving tools unless separately authorized and verified in the actual execution environment.
- Track efficiency by tokens per **correct, verified completed task**, including reruns. See `docs/AI_TOOLING_SETUP.md` for optional local installation and measurement; these repository files do not install software.
- No optimizer may hide safety-critical diagnostics, change risk limits, change broker access, interfere with existing workers, or weaken operational controls.
