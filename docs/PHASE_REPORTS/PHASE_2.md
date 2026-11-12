# Phase 2 — Swift parser and signal matching checkpoint

**Status:** IMPLEMENTED / LOCAL_TESTED / REMOTE_CI_PENDING / SYNTHETIC_ONLY / REQUIRES_LOCAL_AUTH.

## GitHub provenance (to be finalized after remote verification)

- Private repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader
- Branch: `codex/phase2-swift-parser`, based on Phase 1 `1705885eddebd14538b80027a6dd029a5d85da45`.
- Stacked PR: pending new PR; Phase 1 PR #10 remains draft/unmerged.
- Full Git commit: pending commit API response.
- GitHub Actions Ubuntu/Windows results: pending.

## Work delivered

- Deterministic option-only `parse_message` grammar with exact `Decimal` premium, expiration hint, full contract right/strike and standard/LOTTO trade category. Unsupported/conflicting content goes to REVIEW_REQUIRED.
- Strict `resolve_entry` checks yearless expiry against supplied verified catalog candidates; absent or multiple instruments cannot become eligible. Static catalog is synthetic-test-only, not a genuine broker contract check.
- `SignalMatcher` links to the Phase 1 SQLite message/revision database and creates durable decisions, option signals, immutable source identities, message ancestry, average histories, idempotent full-close request markers and review queue. No order calls.
- `0003_signal_linkage.sql` additive migration, `parse-replay` and `parse-health` CLI, 81 labeled synthetic cases and extensive unit/integration tests.
- Changed entry edits/corrections require review; stale/recovered, duplicate and superseded messages never create late actions; follow-up averages cannot overwrite actual average fills.

## Initial local verification

Locally reconstructed Phase 0 + Phase 2 test workspace: 158 tests passed, 0 failed, 0 skipped before final incremental test additions. This workspace does not contain the complete Phase 1 test suite; only remote CI can validate all three phases together. Local Ruff unavailable; GitHub CI will provide lint evidence. Updated total to be verified at final checkpoint.

Synthetic fixture classification benchmark: 81/81 (100% agreement with manually assigned in-sample labels; see `docs/SWIFT_PARSER_PHASE2.md`). This figure must **not** be reported as real Swift parser accuracy or trade accuracy.

## Operational safety

- No actual Telegram session, private Swift data, user computer, broker session or external API used.
- `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`, no simulated or live orders.
- Signal exit status records only a **request**, not a verified sale; future Phase 3 must handle actual quantities, fills and cancel races.
- Previously saved Phase 0/1 branches/PRs and original `main` untouched; Phase 2 is stacked and unmerged.

## Unverified and outstanding work

1. Supported grammar vs actual Swift paid messages: no authorized sample dataset; require separately reviewed real-message tests before trusting execution.
2. Live option instrument listing/year validation: not available until verified Phase 4 read-only brokerage adapter; default parser-replay keeps entries in review.
3. Human review workflow and actual trading state machine are future phases. Retried out-of-order unknown replies are not auto-resolved; they stay review-required.
4. Remote CI links, passed/failed counts and final SHA must be independently verified and filled before claiming CI_PASSED.
5. Independent signed provenance/SBOM/hermetic lock gaps remain issue #2; Phase 1 used pinned dependencies and Windows/Linux matrix.

**Stop after Phase 2.** Do not merge, deploy, authorize accounts or trade. Next separately authorized phase is Phase 3, durable PAPER-only order state machine.
