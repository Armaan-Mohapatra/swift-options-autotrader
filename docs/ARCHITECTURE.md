# Swift AutoTrader architecture and trust boundaries — Phase 7 audit

**Status: PAPER-ONLY SOURCE CANDIDATE; NOT RELEASE-CERTIFIED.** Authoritative strategy contract: [PROJECT_SPEC.md](PROJECT_SPEC.md), read with [SOURCE_SPRINT_PLAN.md](SOURCE_SPRINT_PLAN.md). Source is in stacked, unmerged draft pull requests. This document must not be treated as permission to authenticate, trade, install, or enable a live broker.

## Actual execution graph

```text
Authorized local Telegram user session (read-only, one allowed channel)
  -> Telethon event normalization, reconnect/freshness checkpoint
  -> SQLite WAL raw_events + append-only telegram_message_revisions
  -> deterministic parser + exact contract/catalog/year verification
  -> durable option_signals, signal_message_links, review queue
  -> independently enabled PaperOrderEngine (PAPER BROKER TYPE ONLY)
  -> persisted order_intents and deterministic PaperBroker journal
  -> matched order_fills, cancel/fill-race reconciliation, stop controls
  -> PaperAnalytics / local outbox -> localhost read-only dashboard
```

**Important production limitation:** The Phase 6 Windows supervisor currently runs the *read-only dashboard*, an **optional authorized read-only Telegram listener**, and metadata-only notification scans. It does **not** continuously wire a verified live option catalog/quotes and parser decisions to automatic paper-order cycles. The standalone `paper-cycle` CLI deliberately refuses to run with an unverified catalog rather than mark authentic signals blocked. The full pipeline is validated with synthetic fixtures in disposable SQLite databases, **not** as unattended live-market paper trading. Do not silently change this safety boundary while installing.

## Domain invariants

- **Contract identity:** underlying, CALL/PUT, Decimal strike, expiration date/year verified against a separately supplied instrument catalog, original source signal ID and Telegram reply/edit lineage. Ambiguity -> `REVIEW_REQUIRED`, never a guessed trade.
- **Entry:** long single-leg option BUY-to-open LIMIT uses the exact numeric Swift premium from the *first eligible alert*. Current quote divergence does not change that limit. LOTTO follows the same rule. No second buys on ADD/ADDING MORE/AVERAGING DOWN.
- **Swift average vs. actual cost:** revised average is metadata; confirmed fill-based cost basis is independently computed from paper brokerage fills. Neither the resting entry limit nor quantity is rewritten.
- **Exit:** any explicit partial/full exit means close **100% of exactly matching confirmed open quantity**, after cancelling and confirming the remaining unfilled entry. A submit or cancel request is not a fill or cancellation confirmation.
- **Deadline:** broker acceptance timestamp in UTC + exactly 3,600 elapsed seconds. Cancel unfilled remainder, poll/reconcile late fills, preserve filled exposure, do not next-day resubmit.
- **Auditability:** unique persisted logical order IDs and idempotency keys, separate paper broker journal, append-only fill identities, monotone state transitions; restart cannot manufacture fills.
- **Unknown markets:** missing quotes/depth/permissions/tick/expiration/external account are reasons to block/review, not to invent an option price or transaction.

## Network and real-broker controls

`PaperOrderEngine` enforces `type(broker) is PaperBroker`. The Robinhood MCP adapter is for **read-only** discovery/reconciliation plus inert payload construction; actual `submit_order`, `get_order`, and `cancel_order` always raise `BrokerWritesDisabledError`. The MCP client maintains a read-only tool allowlist and never invokes `place_option_order` or `cancel_option_order`. Defaults are `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`; the settings validator rejects otherwise.

The official MCP SDK OAuth path is only an untested future local Windows opt-in and is **not** authenticated in GitHub development. Approval/permissions/schema compatibility are unverified. No Robinhood password, real private Telegram session, actual market quote or paid message fixture is in the repository. Do not claim real-world trade performance from mock fills.

## Storage and data lifecycle

SQLite WAL migrations `0001`–`0005` cover Telegram identities, signal lineage, paper intents/orders/fills, historical quote evidence and local notifications. Historical replay uses a *separate temporary SQLite file* with explicit synthetic catalog, quotes, account/risk assumptions and provenance `UNVERIFIED_ESTIMATE`. Production DB is not historical-replayed into new actions. The supervisor protects one process with an OS-local lock and keeps DB, credentials, backups and log files outside release-code directories. SQLite online backup API and integrity checks reduce WAL corruption risk; restore requires a stopped service and verified offline state.

Optional metadata-only outbound Telegram status notifications must use a separate operator bot/chat, disabled by default and never point to the paid Swift channel. The dashboard binds exclusively to `127.0.0.1` (default port 8765), exposes no order or stop mutation endpoints, and does not display raw paid message bodies by default.

## No-cloud Windows model

An isolated release directory under the configured managed root holds pinned Python 3.11 runtime dependencies installed from `uv.lock`, with `uv==0.10.0` bootstrap version pin (its own supply-chain hash remains unverified). Windows Task Scheduler runs with a restricted per-user token **at logon**, not before logon. The process supervisor uses bounded child restarts, journal/status, log rotation, conservative resource limits, integrity-checked backups, and code-only rollback. It must never alter Phoenix/Kalshi/other applications' files, tasks, process trees or credentials.

## Release gates (as of this audit)

1. Mandatory Ubuntu + Windows, Python 3.11 + 3.13 full tests, lint, compilation, lock audit and secret scanning must **execute** on actual GitHub runners and pass on one final SHA.
2. Isolated Windows PowerShell parse, non-mutating preview, NoSchedule install, backup, uninstall and ZIP verification must execute on a Windows runner and pass on that SHA.
3. All inherited tests must agree on migration count five (at least one stale assertion was identified in the Phase 6 branch and repaired for Phase 7).
4. Optional SDK (`mcp==2.3.0`) complete dependency lock, software-supply-chain attestations, permissions and OAuth test are **not** established; do not describe the build as hermetic or live-ready.
5. Verify final tree/PR diff, no credential files, full release commit hash and all remaining issues; only then may a **paper-only** version tag/release be considered. Do not tag while CI is unable to allocate runners.
6. Actual Windows installation, real Telegram subscription/MTProto read-only field tests, verified option chains/quotes, automatic paper-cycle wiring and forward runs remain separate, explicitly authorized work.

See [Phase 7 audit checkpoint](PHASE_REPORTS/PHASE_7.md), [Windows runbook](WINDOWS_RUNBOOK.md), [operations](OPERATIONS.md), and [rollback](ROLLBACK.md).
