# Phase 2 — Swift Trades interpreter and signal matching (2026-11-12)

**Outcome: IMPLEMENTED / TESTED / CI_PASSED / SYNTHETIC_ONLY / REQUIRES_LOCAL_AUTH.** These labels mean the deterministic parser and durable matcher passed synthetic tests, **not** that live Swift messages were validated or real/paper trading was performed.

## Verified provenance

- Private repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader
- Branch: `codex/phase2-swift-parser` based on Phase 1 checkpoint `1705885eddebd14538b80027a6dd029a5d85da45`.
- Draft stacked PR #11: https://github.com/Armaan-Mohapatra/swift-options-autotrader/pull/11 ; base is `codex/phase1-telegram-ingest` (draft PR #10). Phase 0 PR #1 is also unmerged. Original `main` is untouched.
- Last **independently CI-verified** code + normalized-snapshot commit: `32df698c14205263d3a821e69d2b733036a8821d`; subsequent documentation-only checkpoints may advance branch HEAD.
- CI run: https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43291903449 — **SUCCESS, 4/4 jobs** on Windows and Ubuntu with Python 3.11 and 3.13.
- Earlier CI runs: [43290631636](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43290631636) failed 14 inherited fixture assertions expecting only 2 migrations; [43291140483](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43291140483) failed a Ruff E501 line. Both defects were fixed. [43291404857](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43291404857) passed 207 tests/job, followed by the final snapshot-test run above.

## Verified checks

| OS | Python | pytest | Ruff / syntax / pinned dependency audit / secrets heuristic |
| --- | --- | --- | --- |
| Windows | 3.11 | **208 passed** | PASS |
| Windows | 3.13 | **208 passed** | PASS |
| Ubuntu Linux | 3.11 | **208 passed** | PASS |
| Ubuntu Linux | 3.13 | **208 passed** | PASS |

**208 distinct tests; 832 executions; 0 failures or skips** in final GitHub run. Locked `uv` install, compilation, Python lint, license/lock inventory script and heuristic secret scan passed. No CI credentials. Heuristic scan is **not** a guarantee against all credential exposure.

## Actual files and features

- `parser/patterns.py`, `parser/rules.py`: deterministic normalized option-entry schema; Decimal premium, explicit call/put, strike, expiration hint, original message ID and trade label. LOTTO treated normally. Non-options ignored. Competing contracts, contradictory instructions, unsupported dates/prices and invalid or missing information are review-required. Parser has no AI-based guesses.
- `signals/lineage.py`: contract/catalog verification, stable signal identity, reply lineage, strict ambiguity handling, duplicate/restart/edit/recovered-message protection and monotonic metadata updates. Without verified instrument candidates, even syntactically valid entries are `INSTRUMENT_CATALOG_REQUIRED` reviews, never trade-eligible.
- `database/migrations/0003_signal_linkage.sql`: additive SQLite tables for decisions, signal metadata, message links, Swift average audit trail, deduplicated 100%-close **requests** and review queue. Existing migrations remain immutable; `confirmed_avg_fill_price` is not overwritten.
- `app.py`: count-only `parse-replay --limit N` and `parse-health` diagnostics; no broker imports, Telegram network activity or orders.
- `tests/unit/test_swift_parser.py`, `tests/integration/test_signal_matching.py`, updated Phase 1 migration fixture and CLI/database tests, deterministic 81-example fixture generator and reproducible normalized-output snapshot test.
- Documentation: `docs/SWIFT_PARSER_PHASE2.md`, `docs/PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json`, `scripts/export_synthetic_parser_results.py`.

## Synthetic classification evidence

**81/81 (100%) intent agreement on the intentionally designed and labeled synthetic fixture, NOT a real-world accuracy result.** By class: 20 ENTRY, 16 METADATA_ONLY, 16 CLOSE_100_PERCENT, 14 IGNORE_NON_OPTION and 15 REVIEW_REQUIRED. Full per-case normalized fields are at [PHASE_2_PARSED_EXAMPLES.json](PHASE_2_PARSED_EXAMPLES.json). Regenerate with `python scripts/export_synthetic_parser_results.py`; the snapshot test guards consistency.

Illustrative (all synthetic, not authenticated Swift messages):

| Message | Parsed outcome | Safety effect |
| --- | --- | --- |
| `SPY 680C 11/13 @ 1.20` | ENTRY / SPY / CALL / strike 680 / hint 11-13 / 1.20 / STANDARD | Await verified listing/year before eligibility |
| `SPY LOTTO 680C 11/13 @ .95` | ENTRY / LOTTO / exact Decimal .95 | Same rule as ordinary entries |
| `SPY adding more, new avg .95` | METADATA_ONLY / Swift average .95 | No new buy; preserves fill-weighted cost |
| `SPY SOLD HALF` or `SPY RUNNERS OUT` | CLOSE_100_PERCENT | Deduplicated full-close **request**, never a claimed fill |
| `BUY 20 SHARES SPY` | IGNORE_NON_OPTION | No position or order |
| Ticker-only update with two SPY signals | REVIEW_REQUIRED | Never pick a contract by ticker alone |

## Hard execution boundary and remaining blockers

- **No actual Swift grammar validation**: the synthetic benchmark cannot estimate precision/recall, false-positive rate or profitability on real paid-channel messages. Actual timestamp/edited/reply behavior remains untested in the intended authorized environment.
- **No real option-chain verification**: entries are not eligible in `parse-replay` without a verified instrument catalog from a future official read-only broker adapter (Phase 4). Ambiguous expired/yearless/unknown listings need human review.
- **No execution**: Phase 2 only classifies and records; the paper broker, cancel/fill race handling, true close verification, risk gates and notification/dashboard are future phases. An SQL `CLOSE_REQUESTED` state is never proof of sale. Out-of-order unmatched replies remain review rather than retroactive execution.
- **No access/deployment**: no private Swift content, paid Telegram messages, user session, Windows device or brokerage account accessed; no paper/live orders; all real broker writes disabled.
- **Residual supply-chain gap**: independent signed attestations/SBOM, offline hermetic rebuild and advisory audit remain issue #2.

**Gate:** Phase 2 is CI_PASSED on mocks/synthetic cases, still requires local authorization and forward validation. Draft PR #11 is unmerged. Stop here; Phase 3 requires a new explicit instruction.
