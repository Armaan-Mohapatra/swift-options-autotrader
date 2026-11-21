# Final reconciliation remediation — independent review handoff

## Exact baseline and scope
- Base: 2d98e288fa0177b1df73984d14c2123ea848996b (Gemini production-integration candidate; reported 665 test passes, not rerun here).
- Review branch: fix/final-reconciliation-review-20261116
- Source/tests checkpoint: 77e8e1a2b343db881c567716a1206414a4e094.
- Prior installed candidate: c0fe3e9c9f7c5f28c9e294cf5dd3674d678ff2e8; Windows acceptance evidence 27db137486dbcc6ac75c9b47dead046fef86b96b.
- This is a draft review, not an approved release. No brokerage contact, real order, host deployment, real activation, or GitHub Actions execution.

## Changes
1. Pure exact quantity validator: rejects missing, boolean, fractional, nonfinite and inexact quantities, and conflicting processed/fill aliases. Integrates into the broker adapter; when no echoed reference exists requires a known broker identity plus reported requested quantity and limit; rejects a fill missing an individual timestamp. Full broker-returned leg identity and positive-fill schema remain unverified.
2. Guarded placement now performs provider review and rejects an ambiguous, warned or approval-required response before calling placement. This is not an invented confirmation phrase or proof that provider permission is permanently granted. The actual underlying_type mapping and approval objects/races need independent verification. No real writes tested.
3. Durable identity persistence/recovery raises on missing journal, conflicts and database failure; persists recovered IDs. Never infer submission success from an uncertain response. Production intent linkage across a crash immediately after provider submission remains an acceptance issue.
4. Engine reserves outstanding partial-fill remainder and unresolved-order exposure; refuses to proceed when a live broker order is UNKNOWN; duplicate strike comparison uses Decimal (including equivalent string formats) and blocks canceled buy with positive filled quantity.
5. Live service rejects real-client use of a paper-shaped installation or shared paper database; requires separate production journal path and matching pinned local account before attempting actual authentication. No production deployment/credential manifest support has been added: existing managed private-path validation still only accepts the paper manifest. This intentionally remains fail-closed, NOT a working production activation route. Proposed production journal name is data/live.sqlite3; it has not been created. No automatic migration from paper to real-money ledger. Independent approval/account-specific journal provenance is still required.
6. Health distinguishes STARTING, DEGRADED, FAILED and STOPPED rather than marking failed market-data cycles successful; simulation and certification fields are separate. Removed full account IDs from new service messages. Pre-dispatch connection failures, persistent stops and health freshness still require integrated testing.
7. Added adversarial repository tests for normalized response strictness, provider reviews, durable identity conflict, partial pending exposure, duplicate contracts, paper journal rejection and health states. Existing synthetic fixtures previously omitting processed quantity now supply explicit synthetic values.

## Actual executed evidence
Linux Python 3.13.5. 35 isolated tests passed (25 pure validation cases and 10 extracted-method checks). No full latest private-repo clone or 665-test rerun. The published pure validator/test files match independently tested local Git blob IDs:
- src/swift_autotrader/broker/review_validation.py: 25e8beb36f099de97e40e8051d181b881fd60187
- tests/unit/test_review_validation.py: d472c50057e88f5b0dda6a2d952d9f6a11186f2c
- src/swift_autotrader/broker/robinhood_mcp.py: 36b8d9d01b25573fa57cc38c47227d829f64d992
- src/swift_autotrader/orders/engine.py: 862068d391632bcb156ddfe16ff01ef7b2b6b669
- src/swift_autotrader/live_service.py: f043acb481e67d2ddf34cad7eecb595a4103c188
- tests/unit/test_robinhood_response_compatibility.py: b962167e1c4b37cb782226241273461a785eb355
- tests/unit/test_live_service.py: 9454fb98b2c68a96d02d5823b94c92d696ca206b
- tests/unit/test_delayed_exits_and_risk.py: a276f0b40f9b618e58cd67d204da7cb8b780eeb6

Only the pure validation files and extracted _outstanding_risk / write_live_health methods were executed locally; full integration tests are authored, not claimed to pass. A clean-clone independent baseline/full candidate suite is mandatory before field installation. Review any failures rather than weakening behavior or skipping substantive assertions.

## Remaining gates and independent review priorities
- Independently run the baseline and candidate suites with external brokerage and Telegram writes blocked; verify source blobs, new cases, and no regressions. Inspect all failure cases against actual source.
- Verify actual provider response contract read-only using official tool schemas and sanitized known no-fill evidence; do not fabricate positive-fill shapes. Review canonical identity, account, option leg, side, ratio, requested and processed/canceled/pending quantities, price, provider-required approval outcomes and durable time anchors. Existing adapter review hardcodes equity; do not infer index/equity from symbol. Ensure provider-required approval cannot be bypassed or confused with the local write configuration.
- Verify live journal isolation, binding/provenance, single writer, no paper events/positions/order identities crossing into production, restart/unknown reconciliation, pending order exposure and fee-excluding net realized threshold. Review delayed exit lineage for old messages predating the position and partially canceled orders in active quote coverage. Evaluate existing manual positions and unknown account evidence.
- Review separate Windows production launcher design end-to-end in simulation. Actual supported setup.ps1/common.ps1/service.ps1/windows_runtime.py remain paper-only. Credential path validation accepts only PAPER_ONLY manifests. Do not use a direct live-dispatch --once as a diagnostic, manually flip flags, reuse the paper DB, or disable credential guards to claim readiness. The versioned installer uses a clean clone and exact commit; avoid git checkout inside installation directory. Existing task is disabled per last host acceptance; use authorized task controls and preserve owner/interactive DPAPI binding.
- Verify process identity, worker health and correct dashboard endpoint /api/status. Storage was critically low (0.48 GiB C:); recheck before operation, obtain owner permission for OS changes. Keep source/cache test temp on E:. No indiscriminate cleanup, no unauthorized system changes. No GitHub Actions billing.
- Preserve agreed strategy; no new confirmation phrase, no penny order assumed nonfilling. The $750 net realized price-P&L daily threshold excludes fees and is not a guaranteed loss maximum. One contract default, LOTTO eligible, 10% max entry slippage, no averaging down, full exits only for matched owned positions, $10,000 exposure design ceiling subject to verified buying power.
- Distinguish four evidence levels: offline unit/simulation, native Windows operations, authorized supervised broker lifecycle, and actual production authorization. No real-money canary orders during development. Any supervised real-money acceptance requires explicit specific authorization and provider permission.
- Return an exact ordered current-state-to-goal checklist with command names verified against code, prerequisites, expected outputs, stop conditions, who must perform each action, and any unavoidable manual/broker approval. Report unresolved issues precisely. Keep production disabled until genuinely validated.

