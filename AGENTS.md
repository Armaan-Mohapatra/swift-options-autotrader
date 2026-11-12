# Maintainer / agent instructions — binding

## Scope and precedence

- Read `docs/SOURCE_SPRINT_PLAN.md` **in full**, `docs/PROJECT_SPEC.md`, and the previous report in `docs/PHASE_REPORTS/` before changing code.
- The sprint source is authoritative. `docs/PROJECT_SPEC.md` is the permanent project contract. If the source and implementation conflict, **stop and report**, do not quietly reinterpret.
- Only implement the phase explicitly authorized by the user. Phase 0 is **only foundation**, not live Telegram, option parsing, order execution, paper fills, Robinhood auth or Windows installation.
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
