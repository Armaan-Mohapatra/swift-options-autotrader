# Project Specification — Immutable Trading Contract (v1)

**Project:** `Armaan-Mohapatra/swift-options-autotrader` (intended PRIVATE). **Source of authority:** [`SOURCE_SPRINT_PLAN.md`](SOURCE_SPRINT_PLAN.md), read in full. This document preserves all normative behavior and the phase boundaries. Changes to this contract require explicit owner approval and a reviewed change record. No implementation may be inferred from this contract.

## 1. Purpose and execution boundaries

Capture incoming options alerts from the **authorized** Swift Telegram channel, persist raw events and edits, deterministically interpret source signals, match them to an exact option contract and prior signal, apply strict account/risk checks, and (after Phase 3) create paper-only exact-price option orders with verified lifecycle tracking. Only after future separate user approval and supported official broker authorization can real orders even be considered.

Dataflow: `read-only Telegram → durable event + edit store → parser → signal lineage + contract identity → validation / decision / risk gates → durable order intents → paper or official broker adapter → reconciliation → status/notifications/dashboard`.

Initially **long single-leg calls and puts only**. Multi-leg spreads, naked shorts, shares, futures and crypto are out of scope. The dashboard is local-only. There is no cloud infrastructure requirement. Local auth and installation will occur **only after** GitHub development and separate approval.

## 2. Binding behavior and examples

| Rule | Required behavior | Explicit prohibition |
| --- | --- | --- |
| Asset class | An entry must explicitly identify a valid listed long call/put, with verified expiration year and contract. `SPY 680C 11/13 @ 1.20` is an illustrative syntactic entry pending validation. | Never convert stock/share/crypto/futures alerts or commentary into option orders. `BUY 20 SHARES SPY` is ignored. |
| LOTTO | `SPY LOTTO 680C 11/13 @ 1.20` has the exact same eligible option entry behavior as above. | Do not reject or reprice just because the label is LOTTO. |
| Exact entry price | Buy-to-open LIMIT exactly Swift's initial numeric alert premium (`Decimal`, retain precise value), after broker-required tick/contract checks. May fill immediately at or below limit. | No chase filters, current-quote replacement, silent rounding, fabricated fill, or assumed valid contract. |
| Entry deadline | Persist broker-confirmed `accepted_at_utc`; set `expires_at_utc` to acceptance + exactly 3,600 elapsed seconds. On expiry cancel the **unfilled remainder**, then verify broker status and handle cancel/fill races; preserve the filled part. A 59-minute pending order remains; at 60 minutes cancellation becomes due. | Do not time from Telegram receipt. No unilateral fake cancellation or next-session automatic resubmission. Earlier session/market close may terminate order. |
| Adds | `ADD`, `ADDING MORE`, `AVERAGING DOWN`, `SCALE IN`, similar text become no-buy metadata. | No second purchase just because Swift averages down. |
| NEW AVG | Save Swift's reported/revised average against correct originating signal. | Never change user's fill-weighted cost basis, original pending limit, or quantity. |
| Exits | `SOLD HALF`, `SOLD 1/3`, `TRIM`, `SCALED OUT`, `RUNNERS OUT`, `ALL OUT`, and explicit partial/full sell messages request **close 100% of our matching actually filled quantity**. First cancel outstanding entry remainder. Confirm whether sale fills, continue monitoring; alert if unfilled. | Do not sell half just because alert says half; do not assume submit = closed; do not close unrelated holdings. |
| Exact linkage | Use source message ID/reply/edit relationships, stable signal IDs, underlying, call/put, strike, expiration. If a symbol-only update matches multiple active signals, `REVIEW_REQUIRED`. | Never assume ticker is sufficient or close wrong strike/expiry. |
| Idempotency | Persist logical order and unique idempotency key before external submission; same incoming alert, edit, restart or timeout cannot create duplicate buy/sell/cancel. Reconcile uncertainty before retry. | No retry that could duplicate exposure, no terminal state on unknown broker status. |
| Broker/risk preflight | Check exact option instrument, expiry, order size, permissions, market session, available capital, risk ceilings, broker review and necessary human approval. Reject with explicit reason when missing. | Do not bypass Robinhood review/authorization or skip gates in pursuit of a fill. |
| Market hours | UTC internal timestamps, Eastern presentation. Avoid stale signal replay on reconnect. | Do not silently move after-hours or expiring alerts into next session. |
| Live safety | Phase 0-7 and CI remain paper-only with mock transport; external writes off. `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`. | No live orders, unreviewed switching, session creation in CI, credential uploads or production installation. |

## 3. State and durability

Monotone, audited order lifecycle target: `SEEN → VALIDATED → ENTRY_READY → ENTRY_SUBMITTED → OPEN/PARTIAL/REJECTED/CANCEL_PENDING/CANCELED → CLOSE_REQUESTED → CLOSE_SUBMITTED → CLOSED/PARTIAL_CLOSE/ALERT_REQUIRED`. Exact transitions to be defined and tested in Phase 3, including asynchronous fills after cancel request and network uncertainty. Do not declare `CLOSED` until exact quantity actually sold, and do not lose partial fills or pending open remainder.

Database: SQLite WAL, append-only versioned migrations, processed-event dedupe keys `(channel_id, message_id, edit_version)` and unique submission/idempotency identifiers; sensitive private content remains on authorized Windows machine only. All intent and fill timestamps UTC. Phase 0 creates the schema, but does **not** implement the state transitions, durable repositories or race reconciliation.

## 4. Integration architecture

- **Telegram (Phase 1):** legitimate authorized Hydrogram MTProto user session; designated channel allowlist; new messages/edits/replies/checkpoint/reconnect/backpressure; read-only, no paid-access bypass; only synthetic fixtures in git.
- **Parser (Phase 2):** deterministic strict validation with symbol/right/strike/date/year/premium plus lineage; robust handling of ambiguous, edited, duplicated, old and out-of-order alerts; reject/flag for review rather than letting a language model choose.
- **Order engine (Phase 3):** exact limit price, durable idempotency, risk, stop, lifecycle, cancels, partial fills, mock paper order simulation; no live broker.
- **Broker (Phase 4):** official documented Robinhood Trading MCP via MCP Python SDK; read-only account/chain/quote/order status/review functions; write functionality only in a guarded future local-approved environment. No undocumented APIs. Any unverified official tool schema/capability is a blocker.
- **Analytics/dashboard (Phase 5):** deterministic replay of *authorized* messages and time-aligned option market data, no lookahead, actual simulated fills/fees/multiplier; missing quotes produce `UNVERIFIED_ESTIMATE` instead of false precision. Local read-only dashboard, error/exit notices without paid text/secrets.
- **Windows (Phase 6):** separate dedicated install directory on existing machine, reviewed PowerShell scripts, dry-run-first, Task Scheduler or service, single-instance lock, supervised restarts, backups, health, rollback, log rotation. No access/install during GitHub phases.
- **Audit (Phase 7):** simulated end-to-end validation, Linux+Windows CI, dependency/license/secrets scanning, release only if full gates pass; **still not live**.

## 5. Runtime and credential constraints

Python >=3.11, minimal maintained/license-reviewed dependencies, structured redacted logs, finite timeouts/backoff. No unauthorized Telegram reads, no shared credentials with Phoenix/Kalshi, no paid Telegram raw content in git, no actual brokerage data or credentials in CI. Local Robinhood enrollment requires dedicated external-agent account and the platform's documented approval scheme; assumptions about capabilities must be verified later. No profitability guarantee.

## 6. Current Phase 0 acceptance scope

Create Python package skeleton and typed interfaces; fail-closed config; initial WAL SQLite migration; smoke/dry-run CLI; automated unit and schema tests; offline secret-scanning heuristic; Linux/Windows workflow; README, AGENTS, security/configuration/deployment docs, dependency review, phase/issue tracker, and phase report. Push feature branch and review PR only when an authorized private repository **actually exists**. Tests for exact matching, real message parsing, fills and races are **future phase gates**, not Phase 0 claims.

## 7. Reporting and release gates

Every phase ends with phase status (`IMPLEMENTED`, `TESTED`, `MOCK_ONLY`, `BLOCKED`, `REQUIRES_LOCAL_AUTH` as appropriate), repository/branch/full commit/PR links, actual Windows/Linux CI job URLs and outcomes, test counts, file changes, security and licensing review, limitations, open blockers and next prompt. Internal statuses `NOT_STARTED`, `CODE_COMPLETE`, `CI_PASSED`, `REQUIRES_LOCAL_AUTH`, `FORWARD_TESTING`, `LIVE_APPROVED` must not be confused. No phase auto-start or merge without user review.
