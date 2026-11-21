# Swift AutoTrader — independent release-readiness review (2026-11-14)

**Private repository:** `Armaan-Mohapatra/swift-options-autotrader`  
**Current draft PR:** #24, `audit/windows-certification-remediation-20261113`  
**Runtime source independently certified on Windows:** `8249d1fcd0864bf4283eef209998d862493c9f79`  
**Follow-up CI-only commit:** `983d9c28e83902aec2dda6bdff5d2c43935b1179`  
**Source-code difference between those two commits:** none. Only `.github/workflows/ci.yml` changed. Resolve latest PR head afresh during the independent review. `main` is an early stub and the development PRs are stacked/unmerged.

## Safety and scope

Read-only code review first. No merge, GitHub release, production installation, scheduled job, Telegram account authorization, real Robinhood order, broker cancellation, or changes to Phoenix/Kalshi without separate owner approval. Keep `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`. Never upload or expose paid Telegram messages, session data, OAuth tokens or private account identifiers.

Review `docs/PROJECT_SPEC.md` and `docs/SOURCE_SPRINT_PLAN.md` for exact option identity, Swift's original limit price, LOTTO support, no ADD purchases, NEW AVG as metadata only, broker-acceptance +3600-second entry timeout, and closing the user's entire actually filled position when any explicit trim/sell alert is received.

## Verified by owner-executed native Windows certification on exact runtime SHA

- **Windows Python 3.11.9:** 449 pytest passed, 0 failed; Ruff, compile, Phase 7 paper invariants, pinned dependency audit, secret heuristic, Telethon import, market-SDK import and CLI smoke passed.
- **Windows Python 3.13.17:** 449 pytest passed, 0 failed; same associated checks passed.
- `uv lock --check`, pinned dependency installation and package identity passed. Local package build and independent ZIP hash/manifest/source-commit verification passed.
- An isolated, SHA-verified `-NoSchedule` installation using Python 3.11 succeeded. It installed 42 runtime packages, applied five database migrations, verified PAPER_ONLY configuration, and returned successful application smoke/health.
- An online SQLite backup was created and its SHA-256 matched evidence. WAL checkpoint returned `(0,0,0)`; restoration returned `RESTORED`; SQLite `integrity_check` returned `ok`; application remained NOT_STARTED.
- Protected data purge without explicit dual authorization was refused. Data-preserving uninstall succeeded, kept the database byte-identical, and left no scheduled task.
- No live trading, real Telegram authentication, or real broker account operations occurred in these tests.

These are console/log results executed on the owner's Windows laptop, not independently recreated by the reviewing AI. No complete official GitHub Actions CI results are claimed.

## Additional independent Linux review on exact Windows runtime source SHA

- Linux CPython 3.13.5: **449 pytest passed, 0 failed**, 1 warning using locally available Python dependencies (not the full pinned environment).
- Source compilation passed; lock inventory verified **50 packages and 711 hashed artifacts**; heuristic secret scan and offline paper-only invariant script passed.
- `uv lock --check --offline --python 3.13` passed.
- The full pinned `uv sync --offline --extra dev --extra telegram --extra paper-market` was **blocked** because a locked tzdata wheel is not available in the offline dependency cache. This is a test-environment limitation, NOT a passing full Linux dependency matrix.
- No Linux Python 3.11 full test run has been completed here.

## CI workflow correction

The PR workflow previously omitted `--extra paper-market`, unlike the Windows certification. CI-only commit `983d9c28e83902aec2dda6bdff5d2c43935b1179` corrected all nine uv commands and added fail-closed env variables for broker writes, live mode, paper dispatch and Telegram. This change has **not** produced a green GitHub Actions run. Existing runner-allocation/billing restrictions should be checked by the owner before triggering hosted jobs.

## Highest-priority independent review requests

1. **Actual MCP contract and read-only OAuth feasibility (blocking pilot):** Verify official Robinhood MCP endpoint/tool names, response schema and permissions. The paper data provider expects exact year-resolved options, 100x multiplier, tick size, bid/ask/depth, option quote timestamps, external account permissions, buying power, and read-only order review. Public overview may not guarantee all fields. A syntactically imported SDK does not prove this works. Do not bypass missing evidence or use undocumented fallback.
2. **Security/code review (blocking release):** inspect broker-write guards, fail-closed data freshness, dispatch watchdog, REST timeouts, event/order deduplication, cancel/fill race, SQLite transaction handling, replay lookahead, local secrets/DPAPI, log redaction and package ZIP integrity. Report exact file/line findings with reproducible tests.
3. **Linux Python 3.11 and 3.13 exact-source matrix:** run pinned locked dependencies, Ruff, compilation, all 449 tests, lock audit, paper-only invariants and secret checks. Report exact OS/Python, package hashes, test counts and skips. Do not conflate global-dependency Linux passes with fully pinned Linux certification.
4. **Owner-authorized read-only Telegram field test:** on the owner's Windows user account only, verify legitimate Swift channel access, exact signed peer identity, new/edit/reply/reconnect/restart and parser coverage, and no outgoing Telegram/source-channel messages.
5. **Operational pilot on a separate approved installation:** monitor paper-only behavior during real market sessions, including missed/stale quotes, watch-dog cancellations, opportunity coverage, data-provider throttling, CPU/RAM/disk use and data integrity. Do not claim simulated P&L is live-executable P&L.
6. **Code-only rollback across two compatible installed versions:** pending a second safe release; a first-ever installation cannot exercise previous-release rollback. Also test data recovery with synthetic sentinel rows to prove logical preservation beyond SQLite integrity.

## Release decision

**NO-GO for unattended broker-connected paper execution or a production release.** Local Windows implementation and basic recovery gates passed, but official read-only provider schema, authorized field data, Linux locked-dependency matrix, independent security review, and forward paper validation remain unresolved. Keep PR draft, do not merge, and do not infer profitability.

For any second-AI audit, provide authorized private GitHub access or a sanitized archive from this exact PR, and require a separate evidence-backed GO/NO-GO report.