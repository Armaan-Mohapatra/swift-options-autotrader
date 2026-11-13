# Phase 4 — Official Robinhood Trading MCP integration contract

**Scope:** read-only MCP discovery, account/options data, order review, reconciliation, local interactive OAuth *scaffolding* and future-write request construction. **Real submission and cancellation are intentionally impossible in this phase.** PaperOrderEngine continues to accept **only** the concrete `PaperBroker`.

## Verified public sources (2026-11-12)

- Robinhood official external agent onboarding: <https://robinhood.com/us/en/support/articles/agentic-trading-overview/>, including the exclusive `https://agent.robinhood.com/mcp/trading` Streamable HTTP endpoint, dedicated external-agent/MCP account and the fact that the agent can read other accounts but trade only inside its MCP account.
- Robinhood official trading help: <https://robinhood.com/us/en/support/articles/trading-with-your-agent/>. External options tools include `get_accounts`, `get_portfolio`, `get_option_chains`, `get_option_instruments`, `get_option_quotes`, `review_option_order`, `get_option_orders`, `get_option_positions`, `place_option_order` and `cancel_option_order`. Single-leg long options and limit orders are supported subject to approval. MCP trade-approval controls are still rolling out and some trades may require manual approval even if toggled off.
- Official SDK <https://github.com/modelcontextprotocol/python-sdk> (MIT); v2.3.0 release <https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0>; v2 Streamable HTTP `Client`, `OAuthClientProvider`, client metadata, PKCE, dynamic registration and refresh behavior. <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md>.

These sources establish public *tool names and supported product features*, **not** authenticated, live account responses for this user. Param and response shapes were independently examined using the installed first-party Robinhood connector's tool descriptions (metadata only, never executed). An actual MCP session may expose a different or newer schema. The adapter therefore discovers and validates advertised tool input schemas before read calls, and fails closed for unknown/missing data.

## Files and architecture

- `src/swift_autotrader/broker/mcp_transport.py`: official MCP SDK v2.3.0 transport (Streamable HTTP to pinned Robinhood HTTPS origin), injected OAuth provider, finite connect/read timeouts, no implicit credentials or connection.
- `broker/mcp_local_auth.py`: optional, *not tested with Robinhood*, Windows interactive PKCE callback on `127.0.0.1`, SDK OAuth provider and user-scope DPAPI-encrypted token **and registered client info** outside checkout. Requires local human enrollment and Windows access. The endpoint will **not** be contacted during GitHub CI, module import, `dry-run` or default diagnostics.
- `broker/robinhood_mcp.py`: injectable read-only `MCPAsyncClient` adapter for tool discovery, agentic account validation, buying power, exact chain/instrument/expiration/year resolution, fresh quotes, broker order review, order/position history, partial-fill normalization, order-history reconciliation by stable `ref_id`, future order/cancel request payload builders; no actual calls to write tools.
- `broker/robinhood_mcp.py`: `read_only_preflight()` returns a `Preflight` snapshot with broker buying power and instrument identity but **market_session_open=False** and premium tick unknown by default. Cannot drive automatic paper entries without separate verified market-hours/tick inputs. No live adapter is injected into `PaperOrderEngine`.
- `app.py`: `mcp-diagnostics` is side-effect-free offline by default. `--connect` can only be used on a separately authorized Windows user session with `ROBINHOOD_MCP_INTERACTIVE=true`, not in CI. It uses manual browser authorization and returns **counts only**, never account IDs, tokens, quotes or order content.

## Exact order and risk behavior remains unchanged

1. `future_order_payload()` builds one single-leg `type=limit`, `quantity` exact, `price=str(intent.limit_price)` from Swift's initial alert; `time_in_force=gfd`, `market_hours=regular_hours`. It never substitutes bid/ask or changes a LOTTO order.
2. `BUY_TO_OPEN` maps to `side=buy,position_effect=open`; `SELL_TO_CLOSE` to `side=sell,position_effect=close`; full-exit quantity is determined solely by the tested Phase 3 confirmed-fill engine. Adds and NEW AVG cannot create orders.
3. Future order payload `ref_id` is a stable UUIDv5 derived deterministically from the persisted `OrderIntent.idempotency_key` (not regenerated on retries). Submission by actual MCP tool is **disabled by code**, even if environment variables are flipped. The cancellation builder similarly never calls the MCP tool.
4. `review_order()` calls read-only `review_option_order` with exact terms. Any missing approval flag, unexplained alert, unsupported instrument, account permission, buying power or invalid data blocks future execution. Review is **not a placed order**.
5. `reconcile_order()` queries option order history and matches a *single* order with exact persisted `ref_id`. On missing identity it returns `UNKNOWN` rather than resubmitting or claiming no exposure. It validates acceptance time, fill quantities/prices and rejects unsupported broker states. `pending_cancelled` is **not cancelled**, and filled quantity is still tracked until confirmed.
6. `reconcile_position()` distinguishes confirmed open, explicitly zero-quantity closed and unknown (not found) positions. No live order, cancellation, manual exercise or approval-changing tool is callable.
7. Phase 3 cancellation timeout is still `accepted_at_utc + 3600` seconds with cancel/fill race verification. This module does not start a watchdog or force real cancellations. Phase 4 cannot bypass the emergency stop, market time guards or account/risk restrictions.

## Local-only use / validation (NOT performed)

- Ensure Robinhood MCP availability for your profile and register a **dedicated Agentic/MCP account**. Confirm option level, instrument permissions, the account's `agentic_allowed` flag, cash/settlement behavior and whether approvals are enabled/required; **do not** reuse a primary investing account.
- On a separately authorized local Windows host, install the optional SDK from `requirements/robinhood-mcp-local.txt` in its dedicated venv; verify pinned version/host certificate, licenses, dependency hashes and signing. `mcp==2.3.0` is *not yet included in the Phase 0–3 universal uv.lock*; the mock-only GitHub CI intentionally does not install or authenticate it. This is a reproducibility and field-validation blocker, **not** proof that auth works.
- Review DPAPI-protected local secret path, Windows account ACLs, backup/recovery and OAuth redirect registration compatibility. The SDK uses OAuth discovery and PKCE; Robinhood's support page does **not** guarantee that independent Python clients can register/refresh unattended. If DCR/OAuth unsupported, report blocked and keep paper-only.
- In an interactive local session *only*, run `python -m swift_autotrader mcp-diagnostics --connect` after setting `ROBINHOOD_MCP_INTERACTIVE=true` and providing explicit separate approval. This may open Robinhood's browser OAuth flow, and **has not been executed**. Default `mcp-diagnostics` is offline/no network.
- Inspect actual `tools/list` input schema, pagination/response data, `approval_required`, option tradability and tick rules, quote timestamps/session indication, GFD cancellation behavior, fill IDs and order-history idempotency-key visibility. Unknown/ambiguous data must become `MCPContractError`, not an invented value.
- No transport or OAuth test can demonstrate live write suitability until an *entirely separate* future user-approved activation. Phase 4 explicitly cannot place an order. All existing rules remain paper-only.

## Mock-only reproduction

```sh
python -m pytest -q tests/unit/test_robinhood_mcp.py tests/integration/test_mcp_cli_safety.py tests/integration/test_mcp_paper_isolation.py
python -m swift_autotrader mcp-diagnostics
python -m swift_autotrader dry-run
```

Synthetic examples do not include real account identifiers, private Swift content, passwords, credentials, session cookies or broker responses. No endpoints are called by CI tests. Reconnection is safe because read calls may fail and be retried manually, but ambiguous write/cancel failures are **never automatically retried**; they require read-only reconciliation and separate human authorization.
