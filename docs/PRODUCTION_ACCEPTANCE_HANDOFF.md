# Swift AutoTrader — final local acceptance handoff (GitHub authored)

**Purpose:** Claude Code should REVIEW, RUN FIELD CHECKS, and INSTALL a reviewed
GitHub commit. Do not rewrite the trading engine, repeat historical development,
or introduce new broker writes during acceptance.

## Hard boundary

GitHub CI uses synthetic/fake MCP clients only. No GitHub commit, passing test,
read-only account verification, or previous user approval automatically activates
real orders. Keep `PAPER_ONLY=true`, `LIVE_ENABLED=false`, and
`ALLOW_BROKER_WRITES=false` until the owner separately approves the final
local go-live change. Never access Phoenix, Kalshi, unrelated trading projects,
or any data outside the verified Swift install root. Never upload the live journal,
account numbers, Telegram source text, tokens, or DPAPI files to GitHub/ChatGPT.

**Source-of-truth trading contract:** `AGENTS.md`, `risk_limits.py`, and
the owner-supplied handoff. Options only, LOTTO eligible; one long contract per
valid original ENTRY; no fixed open-trade/exposure cap other than buying power;
premium <= $999; daily net-loss latch at $750 realized + unrealized (New York
trading day); original alert limit exactly; 10% current-ask tolerance without
price chasing; no ADD/averaging; NEW AVG metadata only; matching partial/full
exit callouts request full liquidation; 60-minute timeout from broker acceptance;
dedicated agent-enabled MCP account only; fail-closed idempotency and killswitch.

## What has been implemented on PR #34

- Journal-before-write, unknown-outcome reconciliation, account pinning,
  single-leg contract checks, slippage guard, genuine broker review, exact limit,
  daily-loss latch and full-owned exits.
- Shared single authenticated MCP session for broker and read-only market data,
  avoiding two sessions racing OAuth token refresh.
- Risk attribution from execution timestamps, not order-creation dates.
  Ambiguous overnight realized basis, mixed lots or missing history deny entries.
- A broker aggregate position must match journal-owned inventory before any
  exit; excess manual/unverified holdings are never adopted.
- Transport-boundary checks for pinned account, only approved order tools,
  valid option leg and a final entry kill-switch read.
- An uncertain entry remains CLOSE_REQUESTED after a close callout until the
  order is conclusively terminal. No inferred zero-fill close.
- Emergency pause/status/resume operates on the managed SQLite control row
  even if normal live settings or OAuth are temporarily invalid.
- Rehearsal is read-only: it denies stale source revisions (>120 seconds),
  unverified first-revision lineage and invalid source eligibility before
  broker preflight. No place/cancel calls.
- Dashboard live mode explicitly hides model-derived P/L cards rather than
  presenting simulated P/L as broker-account performance.
- Separate online SQLite snapshots/checksums for the control DB and configured
  live journal; cross-database restore is forbidden. Existing backups stay local.
- Reproducible Windows release ZIP contains reviewed committed blobs despite
  clean Windows CRLF checkout; dirty, ignored or untracked source is rejected.

## Acceptance order for Claude (no real orders)

1. Verify PR #34 head SHA with GitHub and that BOTH full regression and isolated
   Windows deployment workflows PASS on that SAME SHA. Recheck every changed
   file, `AGENTS.md` owner rules and current documented MCP schemas. Do not merge
   or change `main`.
2. Check repo visibility; GitHub has previously reported it as **public**.
   Verify no secrets/paid Telegram text/account data were ever committed.
   If visibility is unintended, request that the owner change it in GitHub.
3. On Windows 11, determine the actual installed Swift root. Historical notes
   mention `E:\SwiftAutoTraderCertification\read-only-pilot\installed`; do not
   assume it is still correct. Preserve other tasks/apps, verify user-scoped task,
   isolate secrets, and stop Swift only (not Phoenix/Kalshi).
4. Perform a managed control-DB backup and configured live-journal backup. Verify
   SHA-256 sidecars and SQLite quick_check. Do not overwrite an existing journal,
   replace it from an old snapshot, or delete old data without explicit approval.
5. Review the SHA-bound release ZIP, installer dry-run, exact commit and isolated
   test evidence. Deploy to the verified Swift install in **PAPER mode** only.
   Run PowerShell 5.1 syntax/operation tests on the actual host (GitHub hosted
   checks also exercise PowerShell 7, which is not equivalent).
6. Reconcile the owner's inherited `PAPER_ONLY`, `PAPER_TRADING` and
   `PAPER_TRADING_ONLY` Windows environment variables without changing global
   settings for Phoenix or unrelated software. Confirm unambiguous Swift mode.
7. Enroll Swift's own MCP OAuth via the owner's browser action and DPAPI storage,
   if not already enrolled. Never ask for tokens or upload the encrypted file.
   Confirm only the pinned dedicated agentic account is used. Read actual
   `get_accounts`, `get_portfolio`, `get_option_orders`,
   `get_option_positions`, `get_option_quotes`, `get_realized_pnl`,
   `get_trade_approval_setting`, `review_option_order` schemas and pagination.
   Confirm server response semantics against code rather than invent fields.
8. Test a real read-only source-to-rehearsal cycle on a **fresh** authorized
   Swift Telegram ENTRY. A stale source must deny; a valid fresh source can
   report WOULD_SEND only after every broker preflight passes. Check no
   `place_option_order` or `cancel_option_order` calls occurred.
9. Validate the New York trading-day P/L calculation with actual read-only
   fills/positions, prior closes, day-trade exits and overnight positions.
   Because realized P/L may reflect lifetime cost basis, the current code
   denies unverified overnight-close attribution. Do not bypass this guard.
10. Test fresh quote validity, official session hours, option tick/multiplier,
    permissions, buying power, full-exit quantity, late-fill cancellations,
    journal restart recovery, duplicate Telegram delivery, unknown broker order
    outcomes, emergency entry pause under an MCP outage, and one-session token
    refresh. Synthetic tests do not establish those real responses.
11. Confirm startup mode/account-ending banner, dashboard truthfulness, local
    metadata-only alerts, scheduled worker resilience, operator emergency
    pause/status commands and failure notifications; ensure any unresolved
    broker order creates a visible operator action and no silent retry.

**If any field gate fails:** remain in PAPER and produce a precise failure report
with sanitized response shapes and a minimal patch recommendation. Do not
silently remove a risk control to make a live check pass.

## Separate supervised live-acceptance gate

ONLY after owner action approving the local mode change: begin with entries
paused, verify configured account ending and journal backup, and deliberately
authorize at most one supervised live entry. Verify its exact contract/limit,
broker order ID, actual fills, position quantity, expiration cancellation,
and one full matching exit against read-only broker state. No automatic
activation of unrestricted unattended trading follows from this test.

Thereafter review 24/7 restart, market-data outages, intraday risk latch,
manual emergency-stop drill, status notifications, and backup/rollback
reconciliation. Only a separately approved final operational signoff can
enable unattended live operation.

## Evidence Claude should return, not new implementation

Repository SHA + PR, actual Linux/Windows CI URLs and conclusions, Windows
PowerShell 5.1 tests, selected install root, verified backup files, masked
account ending, sanitized broker schema and rehearsal results, risk reconciliation,
supervised field test result (if separately authorized), and a single explicit
GO/NO-GO readiness decision. Do not confuse synthetic CI success with live
broker certification.
