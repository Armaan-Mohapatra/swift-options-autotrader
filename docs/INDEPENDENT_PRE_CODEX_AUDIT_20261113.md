# Independent pre-Codex source and package audit — 2026-11-13

**Status: DRAFT / PAPER-ONLY / NOT RELEASE CERTIFIED.**
Base: Phase 8 secondary audit PR #21, commit `49f10f59c5d017d837ebb457891034562e84c21d`.
All changes are isolated to `audit/pre-codex-independent-validation-20261113`. Earlier PRs and their branch heads are preserved. No account authorization, host deployment, release tag, merge, or broker order occurred.

## Independent executable evidence

Two GitHub source blobs were copied byte-for-byte into a disposable local Python 3.13 test environment. Their Git blob hashes were independently checked against GitHub:

- `deployment/windows/extract_approved_package.py` — blob `4cbaabf0522fe8e3c015e456ec6bf95bc0a20230`.
- `scripts/build_windows_package.py` — original blob `54c9399bb1fc404e946f1e18821c0e4c9c2c3fce`, then modified and rerun; fixed blob `3e230946a162fd59cd092170bd8c36a2ba100320`.

The independent pytest run reported **30 passing focused tests** after repairs:
- 25 ZIP extraction validation cases: correct input, metadata, version, checksum, unsafe path, overwrite, and tamper handling.
- 5 disposable local Git/package integration cases: clean checkout accepted; edited tracked code, new untracked code, and Git-ignored source rejected; clean approved ZIP round-trips through the independent extractor.

Before the fix, the three unclean Git source cases **failed**: the original builder claimed the clean HEAD commit while including uncommitted code. The remediation rejects dirty worktrees and confirms every packaged path is Git-tracked. This is necessary but not sufficient for signed release provenance.

Note: The initially authored positive ZIP assertion incorrectly expected the package manifest to be omitted; the implementation correctly retains it. The test assumption was corrected and the suite rerun successfully.

## Additional dispatcher source hardening (NOT independently full-runtime executed)

The new dispatcher has a pre-network `PaperOrderEngine.run_cycle(allow_entries=False)` watchdog pass so market-data latency does not postpone due cancellation reconciliation. A conservative per-cycle cap of 48 fresh entries limits network stalls, and verified revision IDs are processed in chunks no larger than the parser's 500-ID limit. Two integration regressions are added for source-burst draining and watchdog-before-provider sequencing.

**Important: This dispatcher patch has not passed a full Python integration run against a clean checkout of the exact new GitHub commit. Do not infer release-readiness from source review.** It must be rerun in the final testing environment. If any test fails, make the correction in GitHub, not in an installed production tree.

## Confirmed release blockers

1. `uv.lock` lacks `mcp`, `httpx2`, `exchange-calendars`, and `pywin32`. `pyproject.toml` has no paper-market option group. `deployment/windows/setup.ps1` uses `--extra telegram` only. The paper-market worker is not installable as a fully locked production extra.
2. No native Windows 3.11/3.13, Linux 3.11/3.13, PowerShell 5.1, Task Scheduler, DPAPI, upgrade/rollback or installer rehearsal has passed for the exact new source commit. Hosted Actions jobs remain unable to allocate runners because of the account payment/spending restriction.
3. No authenticated, consented read-only Robinhood MCP response has been validated for displayed bid/ask sizes, tick, multiplier, official timestamps, calendar requirements, account eligibility, or review behaviors. Fail closed rather than inventing data. Check the API/tool schema and source simultaneously.
4. No authentic Swift Telegram session or forward paper fills have been verified. Synthetic tests do not prove trading performance, actual option liquidity, or strategy profitability.
5. All existing development PRs remain draft/unmerged. No signed release asset, independently published SHA-256, or official release tag exists.
6. Windows Task Scheduler setup uses an interactive per-user **at-logon** trigger. A computer that is powered on but not signed in after a reboot may not run this service. Confirm the approved unattended login/session operational design without weakening DPAPI account isolation.

## Conditions before any PAPER installation

- Finish the complete optional Python dependency lock on a network-enabled supported test host; install with `uv sync --locked`, and verify actual SDK/OAuth imports on Windows. Do not add guessed hashes.
- Re-run **all** existing and new tests from the authoritative exact GitHub commit on native Windows and Linux in isolated disposable test environments. Verify Ruff, compilation, dependency audit, packaging, unit/integration tests, and installer rehearsal.
- Perform realistic disabled-write simulations, 60-minute cancellation/fill races, disconnected feed recovery, >48 pending entries, unresolved exits, and extended supervised-worker soak/resource tests.
- Review any new defects and fix in source control. Recheck source SHA, provenance, and independent package digest after every code change.
- Obtain owner approval for reviewed stacked merges and one PAPER-only release. Only then permit local installation, followed by separate account authorization and forward paper validation.
- **Real broker writes remain disabled and out of scope.** The paper release is not a real Swift-to-Robinhood auto-execution release.

## Test evidence scope

The independent local tests validate only the two byte-identical package files and the proposed clean-checkout fix. They do not validate the complete repository. The added dispatcher tests are authored in the GitHub branch but not executed here; the GitHub Actions payment/spending restriction remains independent of code correctness.

See `tests/unit/test_release_git_cleanliness.py`, `tests/integration/test_paper_dispatch.py`, `scripts/build_windows_package.py`, and `src/swift_autotrader/paper_dispatch.py` for changes.
