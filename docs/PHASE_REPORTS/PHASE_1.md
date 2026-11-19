# Phase 1 — Telegram listener and durable event storage (2026-11-12)

**Outcome: IMPLEMENTED / TESTED / CI_PASSED / MOCK_ONLY / REQUIRES_LOCAL_AUTH.** This is GitHub code completion, **not** authorized real-channel field validation or permission to trade.

## Verified GitHub provenance

- Private repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader (admin/push permission verified).
- Phase 1 branch: `codex/phase1-telegram-ingest`.
- Draft stacked PR #10: https://github.com/Armaan-Mohapatra/swift-options-autotrader/pull/10
- Base: `codex/phase0-foundation` at `d80b16401d443b5363bba50a00ecad0cce48c50c`; original `main` remains untouched; PR #1 remains draft/unmerged.
- **Latest tested feature and supply-chain code commit:** `8f89290481b33db4a190a086154a46554c50e7eb`. Report-only commits may advance PR HEAD; refetch it before merge.
- **Locked-install CI run (push):** https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43286409414 — Windows/Linux 3.11/3.13 jobs SUCCESS after final code change.
- **Earlier green PR CI:** https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43284236342 — SUCCESS for pre-license-audit lock commit; final report commit must revalidate.

## Test evidence (source: GitHub Actions Windows/Linux jobs)

| Runner | Interpreter | Results |
| --- | --- | --- |
| Ubuntu | Python 3.11 | 73 passed, 0 failed |
| Ubuntu | Python 3.13 | 73 passed, 0 failed |
| Windows | Python 3.11 | 73 passed, 0 failed |
| Windows | Python 3.13 | 73 passed, 0 failed |

**73 distinct tests, 292 executions; no skips/failures in this CI run.** On each platform: dependency install from `uv.lock` using `uv sync --locked --extra dev --extra telegram`, Ruff, syntax compile, pinned Telethon version, static heuristic secrets scanner, and lockfile version/URL/SHA-256 audit passed. Prior CI failures were Ruff formatting and a Phase 0 test assuming only one migration; all corrected before this verified run.

## Files and behavior delivered

- `src/swift_autotrader/telegram/{events,checkpoint,reconnect,listener,client}.py`: normalized channel/message/reply/author/time identities; read-only Telethon user-session client; exact signed-channel filter; bounded async durable worker; fresh connection epochs and conservative stale/history replay guards; capped reconnect.
- `src/swift_autotrader/database/migrations/0002_telegram_ingest.sql` and `database/repository.py`: additive WAL SQLite schema, append-only revisions, content fingerprint dedupe, persisted high watermark, out-of-order edit ordering, local raw text and aggregate health.
- `src/swift_autotrader/config.py`, `app.py`, `logging_config.py`, `.env.example`: explicit local-only authorization validation, `telegram-health`, `telegram-listen`, no authorization in CI, metadata-only JSON logging.
- `tests/unit/test_telegram_*.py`, updated `tests/integration/{test_cli,test_database}.py`: synthetic-only duplicates, edits, reply lineage, stale/delayed/out-of-order updates, wrong chat, restarts, bounded overflow, reconnect and fail-closed settings. Other Phase 0 tests retained. Includes mocked Telethon client contract tests for authorized private-channel new/edit delivery and unauthorized-user denial, plus JSON logs/worker failure tests.
- `docs/TELEGRAM_INGESTION.md`, configuration/safety/dependency/status docs, `docs/SUPPLY_CHAIN.md`, `scripts/audit_lock.py`, `uv.lock`, `.github/workflows/ci.yml`: operator guide, runtime/phase boundaries, pinned build tools and immutable Github Actions hashes, reproducible artifact hash inventory.

## Phase 0 remediation performed alongside Phase 1

- Previously missing universal dependency lock: **resolved**, committed `uv.lock` with 12 packages, 37 hashed artifacts and clean install tested on all four runner combinations.
- GitHub Actions: checkout v4 and setup-python v5 pinned to verified commit SHAs; checkout `persist-credentials: false`, token permissions `contents: read`.
- Build backend `setuptools==84.2.1`, CI resolver `uv==0.10.0`, optional runtime `Telethon==1.45.0`, direct test dependencies pinned.
- All resolved packages have a documented preliminary license inventory; `scripts/audit_lock.py` enforces expected versions, registry, HTTPS artifact host and SHA-256 presence.
- **Still not hermetic/signed**: the CI resolver and build backend are version pinned but not themselves installed from SHA-locked artifacts, and independent full CVE scanning, attestation/SBOM signing, source review and commit signatures were not performed. Retain residual follow-up in issue #2.
- These Phase 0 improvements live on the **stacked Phase 1 branch**, not the unchanged Phase 0 PR #1; review/merge in dependency order when separately approved.

## Safety and limits

- No private paid Telegram content, developer API credentials, user session, channel ID, broker login or customer information was used/committed. No Telegram connection or Windows computer access occurred.
- No Telegram send/post/message history fetch, parser, orders, fills, Robinhood MCP interaction, startup Windows install or live trading. All brokerage writes fail closed; `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`.
- The persisted `eligible_for_future_processing` flag is **inert metadata** in Phase 1, not a broker/order invocation. No historical gap is replayed into a new signal.

## Required later local authorization and field validation

A human must separately authorize their Telegram account's local Telethon `.session`, developer API hash/ID, the actual signed Swift channel peer, and filesystem ACL protection, **on the separately approved Windows computer**. It remains unverified that the authenticated user account can access the paid private channel. Validate real incoming/edit delivery, restarts, gaps, rate limits, SQLite durability and source session permissions; no CI mock can establish that.

**Stop condition:** Phase 1 is code-complete/mocked; PR #10 draft/unmerged; do not begin Phase 2, install locally, submit orders or claim forward trading validation without separate owner instruction.
