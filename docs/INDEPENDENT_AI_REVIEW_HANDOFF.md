# PR #24 — Independent AI review handoff

**Private repo:** `Armaan-Mohapatra/swift-options-autotrader`
**Target:** DRAFT PR #24 on `audit/windows-certification-remediation-20261113`, based on DRAFT PR #23. Always resolve the CURRENT full PR head SHA; `main` is only a stub and earlier phases are unmerged.

## Safety restrictions
Read-only code review first. Do not merge, tag, release, install, connect personal Telegram/Robinhood credentials, launch a worker, modify Phoenix/Kalshi, or submit/cancel real orders. Preserve `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`.

Review the permanent source specification for exact options contract matching, original Swift entry limit, normal LOTTO eligibility, no ADD buys, NEW AVG metadata only, broker acceptance +3600-second unfilled cancellation, and 100% of matching filled quantity closed on explicit trim/sell signals.

## Owner's independent Windows baseline
- Pre-remediation: Python 3.11 **382 passed / 62 failed**; 3.13 **382 passed / 62 failed**; 53 Ruff errors.
- With candidate MCP + tzdata dependencies and safe Ruff changes on an *isolated local working tree*: **396 passed / 48 failed per version**; 34 Ruff errors.
- 44 replay tests failed on SQLite temporary-file close `WinError 32`, associated CLI failure, 2 LF/CRLF Git-fixture mismatches, 1 dispatcher status expectation mismatch.
- Independently tested local reconstructed source after the remaining fixes: **444 passed, 1 expected duplicate ZIP warning** on Linux Python 3.13. This is NOT an exact HEAD Windows pass, official CI or field validation.
- GitHub Actions runner allocation blocked; do not claim CI green.

## PR #24 changes
1. Use explicit SQLite connection closing, preserving transaction commit and rollback, across simulated broker, Telegram store, signal matcher, replay, analytics, notifications, dispatcher.
2. Git fixture writes exact LF bytes; strict Git source-blob/ZIP verifier remains unchanged.
3. Cancellation regression expects PAPER_ONLY and no provider RPC once there are no active contracts, rather than falsely claiming an unavailable-data lookup occurred.
4. Reviewed Ruff formatting/import/unused symbol fixes.
5. Add paper-only read-only MCP market dependencies and timezone definitions to `pyproject.toml`; Windows setup selects the `paper-market` extra as well as Telegram.

## REQUIRED before certification
- **LOCKFILE TRANSFER COMPLETE:** PR #24 now includes the exact independently Windows-resolved candidate `uv.lock`, Git blob `6d2472dffc4e4213444c711fbe7237393e7d05fe`, together with matching `pyproject.toml`, Git blob `a14e0a5bdc3f80153302eb3e3952c806d3e3026c`. These bytes are verified, but a fresh `uv lock --check` and full exact-HEAD Windows/Linux suite remain REQUIRED; this is not proof of runtime compatibility.
- Re-run exact HEAD clean Windows Python 3.11/3.13: `uv sync --locked --extra dev --extra telegram --extra paper-market`, pytest, Ruff, compile, paper guard, lock audit and secret scan. Log zero failures without hiding skipped checks.
- Run native disposable Windows `-NoSchedule` installer, package Git blob / SHA256 verifier, backup/restore/rollback/uninstall. No production environment.
- Linux Python 3.11 and 3.13 exact HEAD matrix.
- Audit ordering, idempotency and cancellation/fill race, data freshness and fail-closed behavior. Verify actual official MCP SDK response fields and legitimate Telegram authorization only in a separately approved local field test; synthetic mocks and successful import checks do not prove API compatibility.
- Review third-party package license/security for any new dependency.

## Return an evidence-based independent verdict
Latest full PR head SHA; precise files/lines; critical/high/medium/low findings; executed commands and test counts by OS/Python; ZIP hashes; blockers; `GO` or `NO-GO`. Default remains **NO-GO** until every release gate is verified.

This project is private. Another AI may read it only through the owner's authorized GitHub connection or a sanitized, directly shared review bundle. Never publish paid Swift source, broker credentials, local sessions, private channel IDs or personal data.