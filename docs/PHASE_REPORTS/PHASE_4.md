# Phase 4 — Official Robinhood MCP adapter (2026-11-12)

**Status: IMPLEMENTED / TESTED / CI_PASSED / MOCK_ONLY / REQUIRES_LOCAL_AUTH.**

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
- PR: https://github.com/Armaan-Mohapatra/swift-options-autotrader/pull/13 (DRAFT, stacked, unmerged).
- Last fully CI-verified **code** commit: `404c675ceeae61b97a6f1a2dbd8c12ec9d6691bf`. Subsequent report-only changes may advance branch HEAD; re-run CI before merging.
- First lint-failing CI: https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43358046793 (4 Ruff violations, fixed in code commit).\n- **Successful Windows/Linux CI run**: https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43358223441 — 4/4 SUCCESS; actual job logs inspected.
- **275 distinct tests**, **1,100 total test executions**, all passed; 0 failures, 0 skips on four runners: Ubuntu 3.11/3.13 and Windows 3.11/3.13. Ruff, compileall, pinned uv.lock install, existing dependency audit and heuristic secrets scan all pass.\n- Local Phase 3+MCP-focused subset: 63 passed. The actual SDK transport and Windows DPAPI OAuth path were **not authenticated or exercised in CI** (mock test protocol only).

## Unverified and hard blockers

- Actual Robinhood MCP `tools/list` and output schemas, OAuth/DCR/client registration, refresh after restart and interactive browser callback are **NOT VERIFIED** on a real authenticated user session.
- Agentic option permissions, buying power, session/tick data and approval requirements are not validated for the user. Real option order review, fill and order history remain **mock contract examples**, not live field observations.
- `mcp==2.3.0` is an optional local Windows dependency and not in the existing universal dependency lock. Signed SBOM and fully locked SDK installation remain open.
- No live trading, no real cancels, no automatic post-reconnect orders, no deployment. The application **cannot** enable write tools during Phase 4.

## Next phase

Phase 5 only after owner review. Continue synthetic historical paper replay/reporting/dashboard. Do not link personal accounts or deploy without separate approval.

## Verified acceptance and limitations

- [x] Exact original Decimal premium preserved in inert single-leg buy-open and sell-close request construction; no quote chase or LOTTO exclusion; 3,600-second accepted-order deadlines remain in unchanged Phase 3 engine.
- [x] Stable UUIDv5 ref_id from persistent logical idempotency key; no duplicate submission or automatic retry after unknown outcome; status normalization distinguishes pending_cancelled, partial, filled, rejected and unknown.
- [x] Strict account/option identity, price timestamp and review guards, mock read-only calls and offline CLI; tests assert all MCP trade/cancel tool invocations remain impossible.
- [x] GitHub PR #13 draft on Phase 3 branch. 17 source/doc/test paths reviewed, none outside this repository or earlier engine changes.
- [ ] Actual Robinhood OAuth/PKCE, tool schemas and permissions, live quotes and account data, successful read-only review/order-history responses and Windows DPAPI behavior remain **REQUIRES_LOCAL_AUTH**.
- [ ] A universal transitive hash lock including the optional SDK, external signed dependency attestations and real-world forward paper fills remain outstanding; do not mark as LIVE_APPROVED.

**Stop after Phase 4:** No merges, real orders, Telegram user enrollments or Windows installation. Phase 5 requires a separate user instruction.
