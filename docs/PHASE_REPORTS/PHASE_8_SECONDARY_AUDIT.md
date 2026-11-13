# Phase 8 secondary audit — competing PR reconciliation, dispatcher repair and release NO-GO

**Status:** SOURCE_REMEDIATION_PUBLISHED / LOCAL_RECONSTRUCTED_TESTED / GITHUB_CI_BLOCKED / WINDOWS_UNVERIFIED / DEPENDENCY_LOCK_INCOMPLETE / NO_RELEASE. No broker writes, real account access, Windows user-machine deployment, merge or tag.

## Authority and PR selection

Based strictly on Phase 7 source commit `8c33e5d85b4abe2bfd0c07e059247096ef68b310` and both Phase 8 candidates:
- PR #19 (`codex/phase8-unattended-paper-integration`, `c02fd7c797b9d2f79039a915a365806d46d60d63`) and PR #20 (`codex/phase8-unattended-paper-development`, `636fdb3b3d66751f2aec6a8585187433435b69b6`) are parallel draft PRs based on Phase 7, not additive upgrades.
- **Selected baseline: PR #20** for its separately packaged ZIP verifier, release-file manifest, Windows rehearsal and broader tests. Core source `paper_dispatch.py`, `mcp_paper_data.py` and `paper_service.py` was identical in both candidates.
- Preserve useful independent PR #19 test `tests/integration/test_phase8_paper_dispatch.py` and architecture addendum. Never merge both PR branches; both remain intact.
- New separate remediation branch is stacked on PR #20, draft/unmerged.

## Reproduced faults and regression-first repairs

| Fault | Before | Repair and executable regression |
|---|---|---|
| Invalid pending BUY starved an unrelated SOLD HALF/ALL OUT | Reproduced failing exit test | Selectively interpret independently actionable follow-ups; never consume unresolved entry as REVIEW_REQUIRED |
| Canceled zero-fill leg kept requesting quotes | Reproduced failure | Derive active contracts from live resting entry state OR positive bought-minus-sold quantity |
| Stale unverifiable entry starved new valid signals | Reproduced failure | Prioritize fresh eligible source revisions independent of older unresolved entries, retain source provenance |
| 13+ pending entries exceeded provider's 12-contract cap | Reproduced failure | Batch and bisect read-only contract validation, allowing other entries to progress without a fabricated contract |
| Data outage checkpoint and duplicate-order risk | **Guard test already passed on PR #20**; no baseline duplicate found | Preserve durable idempotency; isolate matched exits while keeping watchdog active even if market-data provider fails |
| Missing matching exit quote blocked exit recording | Reproduced failure | Persist close request and `EXIT_QUOTE_MISSING`/equivalent unresolved-exposure alert; no invented sell fill |
| Quote timestamp used as unique quote identity | Reproduced evidence-aliasing failure | Include actual bid, ask and displayed sizes in SHA-256 identity; identical observations remain idempotent |
| Approved ZIP installer copied management scripts from arbitrary launching directory | Reproduced source-policy failure | PowerShell checks owner-approved archive SHA, extracts verifier from that checked archive, copies installed scripts from verified extracted source |
| Missing package version validation | Reproduced failure | Bind version in manifest to `pyproject.toml`; reject mismatch after file SHA verification |
| Unsupported `System.IO.File::CreateNew` used by draft ZIP verifier | Regression-first source test | Use supported `System.IO.File::Open(..., FileMode.CreateNew, ...)` overload |

The six specifically named dispatcher tests were written first, then run against the original PR #20 module: **5 failed and 1 passed** (the already-correct duplicate-on-recovery guard). Replacing that original module with the remediation implementation gave **6 passed**. The quote timestamp collision and ZIP provenance tests were also observed failing before their fixes. All regressions were subsequently green against a reconstructed local Python 3.13 workspace. Names added to `tests/integration/test_paper_dispatch.py`, `tests/unit/test_mcp_paper_data.py` and `tests/unit/test_release_archive_verification.py`. Extra PR #19 tests copied without rewriting strategy semantics.

## Local evidence and strictly limited interpretation

- Reconstructed local Python **3.13** full suite: **376 passed, one expected duplicate-ZIP-entry warning**. This workspace is not a pristine checkout of the authoritative GitHub HEAD: it still has stale/missing `pyproject.toml` and `uv.lock` from earlier reconstruction. Do **not** claim these counts represent exact-final-commit clean checkout.
- Source bytecode compile: **passed**.
- Ruff: **not installed**, no lint pass claim.
- Linux interpreter available: 3.13 only; native Windows 3.11/3.13, Linux 3.11 and PowerShell 5.1 **not run**.
- GitHub Actions issue #15: all recent Phase 5 onward workflows failed prior to runner allocation and test steps. User reports a payment/spending-limit restriction; connector evidence shows zero steps, and GitHub web UI must establish exact billing message. **No new manual reruns authorized or requested.**
- No official broker OAuth, paid Swift message content, real exchange quote, actual forward-paper P&L or Windows target environment was accessed.

## Open release gates (mandatory, do not bypass)

1. `pyproject.toml` and existing universal `uv.lock` cover only basic Telegram and developer dependencies. The current installer still runs `uv sync --locked --no-dev --extra telegram`, omitting the opt-in paper-data worker's `mcp==2.3.0`, `httpx2>=2.10.0`, exchange-calendar and Windows-only pywin32 requirements. **COMPLETE LOCK NOT PRODUCED.** Offline `uv lock` cannot resolve missing cached artifacts; never invent registry hashes. The operator must **not** enable `paper_dispatch_enabled` or install a release claiming this is ready.
2. Actual Robinhood read-only tool response fields (bid/ask depth, timestamps, tick, option multiplier and session identity), OAuth approval/refresh behavior and permissions remain unverified. Unavailable fields correctly stop paper trading. No undocumented workaround.
3. GitHub Actions Windows/Linux Python 3.11/3.13 full matrix plus isolated Windows ZIP/Task Scheduler/backup/upgrade/rollback/uninstall rehearsal must allocate real runners and pass on one exact reviewed commit.
4. Runtime stress, real-market quote collision and out-of-order timestamps, full continuous worker soak, independent dependency CVE/license/SBOM check, Windows DPAPI and per-user Task Scheduler authority still need native validation.
5. All stacked PRs are draft and unmerged. No official signed release asset, digest or tag is certified, and owner approval is required for a merge or published release.

**Decision:** NO-GO for any Windows Codex installation or release certification. Review the new isolated remediation PR. Maintain `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`; no real Robinhood order/cancel path.
