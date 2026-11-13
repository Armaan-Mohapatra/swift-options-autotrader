# Changelog

Changes are described in developer/source terms, not as validated market trading or deployment. **No Phase 7 GitHub release tag has been issued while mandatory GitHub Actions and Windows runner checks remain blocked.**

## Unreleased — Phase 7 final integration audit

- Added synthetic end-to-end regression tests through the real message-store, deterministic parser, signal lineage, paper engine, simulated broker and analytics pipeline; tests cover ordinary and LOTTO entries, exact limit, 3,599/3,600-second boundaries, revision/duplicate handling, explicit full exits, distinct same-symbol contracts, missing prices, paper restart and stop controls.
- Corrected inherited Phase 1 test migration expectations to recognize the existing Phase 5 `0005_research_monitoring.sql` migration; *no migration was edited or downgraded*.
- Added offline paper-only safety boundary verifier for source inventory, deny-only real broker methods, exact PaperBroker engine gate, settings defaults, loopback dashboard and migration sequence.
- Expanded architecture, operator documentation, implementation status and audit evidence; retained all open blockers.
- Existing CI remains blocked prior to runner allocation. This is **not a released, signed, forward-tested, or live-ready build**.

## Phase 6 — Windows deployment source candidate (unreleased)

- Dry-run-first PowerShell installer, user-scoped Task Scheduler, isolated config/secrets/releases, SQLite backups, health, control, rollback, no-live supervision, package builder and Windows workflow. Windows CI has not run successfully.

## Phase 5 — Synthetic replay/analytics/dashboard (unreleased)

- Historical synthetic quote-based replay, P&L/fees/price provenance, localhost read-only dashboard and local notifications. Synthetic-only results; GitHub runner gate blocked.

## Phase 4 — Read-only Robinhood MCP scaffold (unreleased)

- Mocked official-tool contracts, local-only OAuth scaffolding, read-only account/option/order discovery, hard-disabled real order writes. GitHub matrix passed 4/4 in Phase 4; real authentication untested.

## Phase 3 — Durable simulated order engine (unreleased)

- Original limit, 3,600-second acceptance deadlines, partial fills/cancel races, full exits, idempotency, restart and emergency stop. Synthetic paper matrix passed.

## Phase 2 — Deterministic options interpretation (unreleased)

- Swift-style message parser, exact contract/lineage, metadata-only adds/averages and close-all requests. Synthetic fixture classification only.

## Phase 1 — Authorized read-only Telegram ingestion code (unreleased)

- Mock-tested Telethon receiver, edits, checkpoint and persistent SQLite revision storage. No real channel session used.

## Phase 0 — Foundation (unreleased)

- Project specification, paper-only skeleton, initial SQLite WAL and CI scaffolding.


## Unreleased — Phase 8 PAPER dispatcher source candidate

- Added opt-in, independently supervised Telegram → strict verified-data → PAPER execution worker, reusing existing parser, paper broker and SQLite.
- Added hard data-unavailable guard preserving source events and continuing 3,600-second cancellations; no price guessing or real broker writes.
- Added explicit optional MCP option tick, multiplier and bid/ask depth fields that remain **unknown** when absent; unapproved external brokerage data does not validate live execution.
- Added opt-in Windows supervisor settings and separated user-secret handling; no host installation or real OAuth occurred.
- Added simulated end-to-end dispatcher tests and technical release-blocker documentation.
- **Unreleased, no certification:** mandatory GitHub Actions runner allocation and optional SDK lock are blocked.
