# Phase 5 — historical replay, paper performance and localhost dashboard

**Status:** IMPLEMENTED / LOCAL_TESTED / CI_BLOCKED / MOCK_ONLY / SYNTHETIC_ONLY / REQUIRES_LOCAL_DATA. This is NOT CI_PASSED or forward-validated.

## Provenance

- PRIVATE repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader
- Base checkpoint: Phase 4 `fc7514375f506d2bff82402176f97b96b3f3ab47` (draft PR #13, unmerged).
- Feature branch: `codex/phase5-analytics-dashboard` (stacked). Do not alter earlier branches or `main`.
- Draft PR #14: https://github.com/Armaan-Mohapatra/swift-options-autotrader/pull/14
- Last locally tested code commit: `4191310ddc196c8874584302eb53f4fb493dfb26`. Documentation commit may advance HEAD; check latest ref.
- GitHub CI (PR) https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43367076794 **FAILED BEFORE RUNNER START**; earlier push https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43367053314 similarly failed. 4/4 jobs had 0 steps, no assigned runner; no test failures were executed and no Windows/Linux pass can be asserted.
- Earlier Phase 4 baseline was green: https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43358530766, 4/4 jobs, 275 passed/job. This does NOT validate Phase 5 code.

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

- Locally **87 passed, 0 failed, 0 skipped**, Python 3.13, Phase 3 engine and Phase 5 replay/metrics/dashboard/notification/CLI focused suite. Test command: `PYTHONPATH=src pytest -q tests/integration/test_paper_order_engine.py tests/integration/test_phase2_to_paper.py tests/integration/test_phase5_replay.py tests/unit/test_phase5_analytics.py tests/unit/test_phase5_notifications.py tests/integration/test_phase5_dashboard.py tests/integration/test_phase5_cli.py`. This uses temporary Phase 1 placeholders in an isolated reconstructed local development environment, NOT a complete clean-room regression of all repository files. The authoritative Phase 1 files remain unchanged in GitHub.
- `python -m compileall` passed on local Phase5 code. GitHub Ruff, lock/license audit, secret heuristic and the complete inherited suite have NOT executed due zero-runner CI failures. There is no valid Phase5 Windows/Linux test count.
- Local documentation and tests deliberately avoid the user's actual Swift paid content, Robinhood account, Windows computer and broker credentials.
- No outbound notifications are sent; the notification outbox is local only.

## Open limitations and later phase gates

- Source/quote history is synthetic or externally user-supplied and unverified; no real Swift classification accuracy, execution model calibration, broker fee verification, real win rate, or trade performance can be claimed.
- Assumed queue priority, displayed depth (not guaranteed executable), optional slippage and 100-contract multiplier limits are research simplifications. Some positions have unknown mark/P&L when the BID is missing or stale.
- No continuous dashboard service, Windows installation or credential/auth testing; Phase 6 remains separate. No live brokerage writes.
- Phase 4 OAuth still requires locally approved manual enrollment/validation; signing/SBOM and full optional SDK dependency lock remain outstanding.

**Stop after Phase 5** and await explicit owner review before Phase 6, merging, deployment, authentication or any real trading.

## Execution evidence and external CI blocker

Final Phase5 feature branch contains five additional defensive repairs at `4191310ddc196c8874584302eb53f4fb493dfb26`: enforce original entry/exit limit consistency on ledger reads, persist quote provenance, show quote-coverage uncertainty for unfilled entries, preserve session boundaries and surface unresolved alerts without outbound notifications. These passed the 87 local tests above.

GitHub Actions failure is **prior to runner allocation**, as observed from the GitHub REST jobs endpoint (four failed jobs with empty `steps`, `runner_id=0`, ~0ms billed). The exact root cause cannot be established from accessible logs (log download returns BlobNotFound). Repo owner should inspect the run's banner/annotation and GitHub Actions settings, usage budgets and billing, then re-run the PR matrix. Do not assert CI success, merge or release until all mandatory checks actually pass.

Source quality caveat: the replay accepts explicitly labeled `HISTORICAL_UNVERIFIED` third-party evidence without independently verifying it; synthetic option quotes/depth, modeled slippage/fees, simplified 100 option multiplier and close-only drawdown do not reproduce exchange queue priority or total portfolio return. The illustrative `$42` gross/`$39.40` net numbers are fabricated **as fixtures** and must never be reported as real Swift returns. Private source content remains local.
