# Phase 5 — historical replay, paper performance and localhost dashboard

**Status:** IMPLEMENTED / LOCAL_TESTED (initial); MOCK_ONLY / SYNTHETIC_ONLY / REQUIRES_LOCAL_DATA. GitHub CI evidence and final branch SHA will be appended only after verification.

## Provenance

- PRIVATE repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader
- Base checkpoint: Phase 4 `fc7514375f506d2bff82402176f97b96b3f3ab47` (draft PR #13, unmerged).
- Feature branch: `codex/phase5-analytics-dashboard` (stacked). Do not alter earlier branches or `main`.
- PR/commit/CI: pending publication and final verification, **do not infer green from local tests**.

## Implemented

- Read-only FIFO/options-100 multiplier P&L from confirmed paper fill rows with explicit fee model, observed BID marks, spread observation, time-based mark freshness, completed-position win rate and closed-fill drawdown. Unknown input -> `null` or `UNVERIFIED_ESTIMATE`.
- Isolated temporary SQLite replay, using the existing `TelegramStore`, `SignalMatcher`, `PaperOrderEngine`, `PaperBroker` and migration chain, with explicit synthetic catalog/market-session/risk. No historical messages enter an active trading DB; no future quote lookahead.
- Exact Swift limit persists, no adds, LOTTO ordinary, Swift average separate; all matched partial or full exits request close of all filled contracts. 3,600 seconds accepted-order cancellation unchanged.
- Quote crossing honors quoted bid/ask and depth with optional slippage/strict-through sensitivity, never above buy or below sell limits. Evidence quotes persist in append-only `0005_research_monitoring.sql` alongside metadata-only notification outbox and provenance run records.
- Three independent scenarios (`touch`, `through`, `no_fills`), deterministic synthetic dataset; input hashes and `HISTORICAL_UNVERIFIED` classification separate from verified market prices.
- Local-only read-only dashboard, JSON status, metadata-only alert views, Telegram health, countdowns, positions, P&L and emergency stop. No external web assets, browser POST actions or active trade code.
- Deduplicated local critical/error notification outbox for Telegram failures, unresolved paper exits and application failure codes.

## Initial synthetic sample (NOT real Swift performance)

Sanitized fixture `tests/fixtures/swift_phase5_replay_synthetic.json` deliberately models 2 SPY LOTTO contracts with limit `$1.20`, quoted ASK `$1.15` and full exit at quoted BID `$1.36`. At standard 100 multiplier: gross simulated result **$42.00**, with explicit fees `$0.65` per contract per side: modeled net **$39.40**. Swift's amended average `.95` remains separate from actual average fill `1.15`. These are contrived input figures, not a performance claim. Market prices and actual trading fills were never retrieved.

## Current validation

- Locally new replay/metrics/dashboard/notifications regression tests pass in isolated development environment. Final exact test counts must come from GitHub CI including inherited Phase 0–4 suites.
- Local documentation and tests deliberately avoid the user's actual Swift paid content, Robinhood account, Windows computer and broker credentials.
- No outbound notifications are sent; the notification outbox is local only.

## Open limitations and later phase gates

- Source/quote history is synthetic or externally user-supplied and unverified; no real Swift classification accuracy, execution model calibration, broker fee verification, real win rate, or trade performance can be claimed.
- Assumed queue priority, displayed depth (not guaranteed executable), optional slippage and 100-contract multiplier limits are research simplifications. Some positions have unknown mark/P&L when the BID is missing or stale.
- No continuous dashboard service, Windows installation or credential/auth testing; Phase 6 remains separate. No live brokerage writes.
- Phase 4 OAuth still requires locally approved manual enrollment/validation; signing/SBOM and full optional SDK dependency lock remain outstanding.

**Stop after Phase 5** and await explicit owner review before Phase 6, merging, deployment, authentication or any real trading.
