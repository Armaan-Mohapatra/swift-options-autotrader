# Issue list / planned GitHub issues

**GitHub issues now created:** [#2](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/2) is dependency/reproducibility debt. Future work is tracked separately: [Phase 1 #3](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/3), [Phase 2 #4](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/4), [Phase 3 #5](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/5), [Phase 4 #6](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/6), [Phase 5 #7](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/7), [Phase 6 #8](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/8), [Phase 7 #9](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/9). The detailed subtasks below remain a planning checklist, not one remote issue per line.

- **P0-01 [DONE] Private repository + PR:** Create `Armaan-Mohapatra/swift-options-autotrader`, preserve any preexisting work, feature branch, verified full commit, PR. Acceptance: remote URL and SHA independently fetched.
- **P0-02 [CI_PASSED] Python foundation + docs:** package, README, AGENTS, source/permanent spec, configuration, security, status, tracker and source preservation. Acceptance: all committed and reviewed.
- **P0-03 [CI_PASSED] SQLite + CLI:** migration, safe defaults, strict config errors, smoke/dry-run, no network. Acceptance: idempotent WAL migration tests and zero broker calls.
- **P0-04 [CI_PASSED / LOCK_DEBT_#2] CI and supply chain:** Windows/Linux GitHub Actions Python matrix, lint, tests and heuristic scanner PASSED; full transitive lock and immutable SHA pins remain OPEN as issue #2. Acceptance: real green job conclusions with URLs.
- **P1-01 Telegram MTProto read-only allowlisted ingestion:** edited/reply IDs, checkpoints, bounded queues, reconnect; sanitized fixtures; no trade interface.
- **P1-02 Durable raw event store and deduplication:** event updates, replay guards, restart and backpressure tests.
- **P2-01 [CI_PASSED / SYNTHETIC ONLY] Deterministic options-only/LOTTO parser:** exact contract grammar, right, strike, expiration hint and limit premium; real expiration year requires an independently verified instrument catalog (not yet connected); ignore non-options.
- **P2-02 [CI_PASSED / SYNTHETIC ONLY] Signal updates/exit matching:** no-add, Swift average metadata only, 100% close request on any sell, ambiguous ticker-only follow-up requires review. Phase 3 must implement actual fills/execution.
- **P3-01 Order state machine:** intent-before-write, idempotency, exact original limit, acceptance + 3600, no chase, all risk gates.
- **P3-02 Paper broker and race tests:** accepted/partial/rejected/canceled states, cancel-fill race, stop/restart, repeat signals, seller reconciliation and safe exits.
- **P4-01 Official MCP interface and read-only review:** actual documented auth protocol and tool schemas only; mock tests.
- **P4-02 Broker preflight and no-live guarantees:** expiry/session/size/buying power/permissions/approvals; write paths impossible in CI.
- **P5-01 Deterministic historical replay & performance:** time-aligned quotes, spread/slippage/fees, provenance, `UNVERIFIED_ESTIMATE` on incomplete data.
- **P5-02 Local read-only dashboard & notifications:** order lifecycle, last message, deadline status, redacted notices.
- **P6-01 Windows installer/rollback:** dry-run-first PowerShell, single-instance, protected secrets, Task Scheduler, backup/restore, health.
- **P6-02 Restart and resource testing:** CI-only dry-run, no production changes, resource measurements.
- **P7-01 Integration audit:** all spec acceptance scenarios, security/third-party licenses, transitive dependency lock, Windows/Linux checks.
- **P7-02 Paper-only pre-release:** verified commits, release artifacts and explicit local-auth/forward-testing blockers; no go-live.
