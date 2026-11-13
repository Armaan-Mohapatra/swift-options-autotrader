# Independent in-chat source and regression certification — 2026-11-13

**Status: SOURCE FIXES REVIEWABLE; NOT RELEASE CERTIFIED; PAPER ONLY.**

## Baseline and purpose

- Private repository: `Armaan-Mohapatra/swift-options-autotrader`.
- Source reviewed: draft PR #22 at exact SHA `626ba4fc0500638c514034e11bed154f8aa040a4`.
- New remediation is based on that SHA in a separate draft branch; PR #22 and all historical PRs remain untouched.
- No source installed, personal Windows host accessed, real Robinhood/Telegram account queried, broker orders placed, live execution enabled, PR merged or release published.

## Source authenticity of targeted Python modules

A reconstructed Linux/Python 3.13 workspace was assembled from prior development artifacts. The following files were independently reconstructed and their **actual Git blob SHA-1 matched the exact GitHub PR #22 sources before fixes**:

- `scripts/build_windows_package.py`: `3e230946a162fd59cd092170bd8c36a2ba100320`
- `deployment/windows/extract_approved_package.py`: `4cbaabf0522fe8e3c015e456ec6bf95bc0a20230`
- `deployment/windows/setup.ps1`: `8aaf47a26b71d08dd21b455f1cfb778699b8c7d3`
- `src/swift_autotrader/paper_dispatch.py`: `a099728d0ffcf418980be5059624c08a5b5c0480`
- `src/swift_autotrader/database/repository.py`: `96bdbad186fc1fc0c0efcb0fdba6d68feb2fdeac`
- `src/swift_autotrader/database/migrations/0002_telegram_ingest.sql`: `4a4c907276d93833c633c8e7b40ec19d77d997f0`
- `src/swift_autotrader/telegram/events.py`: `d3a18a3ed57ab67a96ca24c67cb20597867e912d`
- `tests/unit/test_release_git_cleanliness.py`: `d713531a341557abe9125211971ffa9e0ce04ee2`
- `tests/unit/test_release_archive_verification.py`: `dcdc9b726c74c6a99ca25380c27e95c028d594b5`
- `tests/unit/test_windows_package.py`: `dd6211a7aa080ab433df47244ca099b83afbe0f3`

Other files in this reconstructed workspace were **not all Git-blob verified**. In particular, the engine and lineage source in the local reconstruction had small nonidentical differences from official PR #22 before the remediation. Its full-suite evidence cannot be called a pristine exact-final-commit result.

## Actual independent tests

```text
PYTHONPATH=.:src pytest -q --disable-warnings
378 passed, 1 warning in 8.23s

PYTHONPATH=.:src pytest -q tests/integration/test_paper_dispatch.py
15 passed, 0 failed

python -m compileall -q src
PASS
```

This is **Linux CPython 3.13 only**, using a reconstructed checkout with synthetic sources and mocked option quotes. No native Windows/PowerShell execution, actual broker OAuth, real account data, authenticated market feed, real Swift signal history or forward paper P&L was tested. The local runtime's previous 365-test baseline was improved, but this is not official PR-head CI.

### Red-before-green findings

1. **Committed Git source bytes:** `core.autocrlf=true` can yield `git status --porcelain` empty while checked-out CRLF file bytes differ from LF `git show HEAD:path` bytes. The exact original PR #22 builder accepted these bytes and the independent test FAILED. Added byte-for-byte comparison against `git show HEAD:path` for every packaged source. Test passed after repair.
2. **Cancellation count under outage:** exact PR #22 dispatcher pre-network watchdog canceled a due order but discarded its `cancellation_requests`. The existing `test_exact_3600_cancel_still_runs_when_provider_disconnected` failed. Now the prepass count contributes to `DispatchResult`.
3. **False exit quote warning:** the pre-network `run_cycle(allow_entries=False)` also ran close processing with an empty quote map, producing an incorrect `EXIT_QUOTE_MISSING` even when valid quote data followed in that same cycle. Added a `process_closes=False` watchdog option, retaining all normal engine behavior by default. Regression now passes.
4. **Parser-to-order crash gap:** when the SignalMatcher committed a valid entry before paper intent creation, the dispatcher no longer saw its source revision and could leave it unexecuted. Added a bounded, exact-contract, fresh-quote recovery scan. Entry signal IDs are specifically allowlisted so an unrelated unverified recorded entry does not become permanently blocked. Synthetic recovery regression now passes.
5. **Stale unresolved-exposure alert:** after a later quote-enabled exit filled in full, the previous `EXIT_QUOTE_MISSING` alert remained unresolved. It is now cleared only after actual fill totals prove zero remaining exposure, not on a sell submission. Regression now passes.

Affected code: `scripts/build_windows_package.py`, `src/swift_autotrader/paper_dispatch.py`, `src/swift_autotrader/orders/engine.py`. Tests added to `tests/unit/test_independent_git_blob_identity.py` and `tests/integration/test_paper_dispatch.py`. These patches preserve the concrete PaperBroker-only gate and hard-denied real Robinhood submit/cancel methods.

## Outstanding mandatory gates

- No full exact-final-commit Windows/Linux Python 3.11/3.13 matrix executed. GitHub Actions issue #15 blocks runner allocation due to reported spending/billing restriction. No manual reruns were submitted by this audit.
- This environment has Python 3.13 only and no PowerShell, Windows Task Scheduler, DPAPI or disposable Windows machine.
- Existing `pyproject.toml` includes Telegram/dev extras only. The official MCP SDK, httpx2, exchange-calendar, Windows pywin32 and all transitive dependencies remain outside the complete reproducible release lock. `uv lock --offline` could not resolve.
- Native Windows package installation, upgrade, rollback, Task Scheduler, credential protection, resource soak, secret/license/CVE/SBOM reviews and signed source/release provenance remain unverified.
- Actual official Robinhood MCP quote depth, tick/multiplier, timestamps, permissions and review-tool schema remain unverified without authorized account connection.
- Real Swift channel formats and forward paper fills were not observed. No profitability claims.
- No official paper-only release, signed ZIP, checksum or permission to install or enable even PAPER mode exists.

**Decision: source patches for code review, NO-GO for release certification and Windows installation.**
