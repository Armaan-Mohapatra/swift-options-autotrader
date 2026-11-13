# Phase 4 — Official Robinhood MCP adapter (2026-11-12)

**Status: IMPLEMENTED / LOCAL_MOCK_TESTED / CI_PENDING / REQUIRES_LOCAL_AUTH.**

## Source and scope

Read all normative documents and previous Phase 0–3 reports. Phase 4 is stacked on reviewed Phase 3 GitHub branch; existing paper engine remains isolated. See [Robinhood Phase 4 guide](../ROBINHOOD_MCP_PHASE4.md) for official source links, exact safety boundaries and remaining validation.

## Code

- Official SDK v2.3.0 Streamable HTTP + externally injected OAuth provider; local interactive Windows DPAPI TokenStorage and PKCE redirect/callback scaffolding. **Never authenticated** in GitHub or CI.
- Read-only MCP schema discovery, account and options permission, buying power, exact chain/contract lookup, quotes/freshness, option order review and alerts, order history and positions, status/fills/partial/cancel pending/reject reconciliation.
- Stable UUIDv5 `ref_id` and single-leg exact-priced buy-open/sell-close *future-write payloads*, with actual `submit_order` / `cancel_order` hard-disabled. PaperEngine remains concrete-PaperBroker-only.
- Offline `mcp-diagnostics` command; explicit opt-in Windows-only local `--connect` path, blocked in CI. No code calls a real trading tool.
- Mock contract and safety regression tests; no Robinhood personal account, real API tokens, Windows host or exchange orders accessed.

## GitHub checkpoint and CI

- Repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader (PRIVATE).
- Base: `codex/phase3-paper-order-engine` at `1988ff6a142b429b6dea90915a52c52424a827b5`.
- Phase branch: `codex/phase4-robinhood-mcp`.
- PR: PENDING.
- Final commit SHA: PENDING.
- GitHub Windows/Linux CI run: PENDING.
- Verified local MCP/order-engine subset: 63 pass at initial checkpoint. Final counts to be taken from GitHub after upload.

## Unverified and hard blockers

- Actual Robinhood MCP `tools/list` and output schemas, OAuth/DCR/client registration, refresh after restart and interactive browser callback are **NOT VERIFIED** on a real authenticated user session.
- Agentic option permissions, buying power, session/tick data and approval requirements are not validated for the user. Real option order review, fill and order history remain **mock contract examples**, not live field observations.
- `mcp==2.3.0` is an optional local Windows dependency and not in the existing universal dependency lock. Signed SBOM and fully locked SDK installation remain open.
- No live trading, no real cancels, no automatic post-reconnect orders, no deployment. The application **cannot** enable write tools during Phase 4.

## Next phase

Phase 5 only after owner review. Continue synthetic historical paper replay/reporting/dashboard. Do not link personal accounts or deploy without separate approval.
