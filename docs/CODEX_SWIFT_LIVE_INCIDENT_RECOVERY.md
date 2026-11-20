# CODEX: Swift LIVE incident recovery and release acceptance

**Status: review-only. Not a release approval or trading authorization.**

## Verified inputs

- User's Nov 19 incident report: Windows LIVE worker running local deployed SHA
  `41cb04642b2f3ca7c9e5b419807423271bd74e3a`; cycles completed, entries
  paused under `OPERATOR_PAUSE_SWIFT_LIVE_REPAIR`, and no Swift orders.
- Telegram USO 130P 1/15 @ 6.00, message `14635`, failed originally. It is
  expired and **must never be replayed**.
- Local report says 1,149 tests passed / 2 skipped on that deployed revision.
  Those numbers are **not** test results for this GitHub PR.
- GitHub PR #36 head `release/swift-consolidated-20261118` was behind the
  user's local installed SHA when this branch was prepared. GitHub could not
  retrieve the full `41cb0464` commit at the time. **Resolve this first.**
- Three account-held manual options (HOOD, MO, KWEB, Jan 2027) are NOT Swift
  orders, but are included in the existing account-wide daily P&L risk gate.
  Their option quotes go stale after market close. Do not pretend otherwise.
- A prior-day NVO close may have lifetime instead of same-day realized P&L.
  Existing code fails closed. **Do not reinterpret it without broker evidence
  and an explicit owner risk-policy decision.**

## Changes authored in this repair branch

1. `live_dispatch.py`: terminal, transactional `SKIPPED` decisions for expired
   ENTRY or IGNORE revisions without requiring a successful market snapshot,
   preserving delayed exits and ambiguous review messages. 120-second ceiling.
2. `live_dispatch.py`: 30s snapshot timeout, response-time validation, distinct
   timeout/unavailable notifications, critical exit-blocked notification when
   open Swift positions lack verified market data, per-cycle scan of engine and
   Telegram notifications, daily P&L denial notification/recovery.
3. `robinhood_mcp.py` + `mcp_paper_data.py`: preserve vendor two-tier option
   increments, select the tier at the original entry limit or held-position
   bid, and use a response-time quote clock for LIVE reads.
4. `live_service.py`: inject LIVE quote response-time clock.
5. Synthetic regression tests in `tests/unit/test_live_incident_hardening.py`
   and `tests/unit/test_mcp_paper_data.py`.

These edits are a starting patch against PR #36, **not an assertion they can be
deployed directly**. The local installed repair likely already implements some
of them differently. Resolve diffs, do not overwrite newer fixes.

## Mandatory execution by Codex on the Windows owner workstation

1. **Reconnaissance (read-only):** Identify exact managed install root,
   `active-release.txt`, `previous-release.txt`, Windows task definitions,
   full running Swift process command lines and parent IDs, LIVE pause state,
   approval receipt expiry, current branch/HEAD, and both SQLite journals.
   Confirm there is ONE live worker. Never print credentials or account IDs.
2. **Recover release lineage:** Locate the full installed `41cb0464` Git commit
   or exact source bundle and compare with remote PR #36 and this repair branch.
   If it only exists locally, preserve and push the complete nonsecret history
   to a protected review branch. If impossible, **stop deployment** and report
   the missing artifact. Never reinstall from older GitHub main.
3. **Integration:** Port only the missing tested repair hunks onto the recovered
   installed-release lineage. Keep the locally validated two-tier tick fix,
   30s timeout, response-time freshness, notifications, stale expiry and LIVE
   upgrade fix where already implemented. No duplicative implementation.
   Reconcile tests and the Windows setup script. Re-test `14635` as a
   **never-send** archived historical event.
4. **Local offline tests:** Python 3.11 and 3.13 hermetic test suites; Ruff;
   compileall; risk/permission/secret checks; paper replay; Windows PowerShell
   5.1 parse; SHA-pinned ZIP verification; isolated installer dry-run. Do not
   use or pay for GitHub Actions; do not issue real orders. Inspect all skips
   and new tests. Compare test counts and explain any regression.
5. **Read-only account checks:** Verify dedicated DPAPI-pinned agentic account,
   tool schemas, broker confirmation policy, risk order-history pagination,
   positions, buying power, quote timestamps and timezone, clock
   synchronization, option tick tiers/depth, NY session calendar, Telegram
   connectivity, LIVE daily P&L and notification persistence. Collect only
   redacted, no-secret metadata.
6. **Policy decisions:**
   - Default stays **account-wide** daily P&L with a **30-second fail-closed**
     held-option mark freshness rule. Do not exclude manual positions, relax
     quote age or silently discount risk.
   - Default stays **NVO overnight close = DENY NEW ENTRIES** when daily P&L
     attribution is unverifiable. A prior-close correction needs verified
     daily broker math plus explicit owner approval.
   - Push notifications: local-only until owner provides bot token/chat ID
     through secured local enrollment. Do not ask for tokens in GitHub.
   - Keep `$750` cumulative realized+unrealized NY-day loss latch,
     `$999` premium max per signal, one LONG contract per eligible entry,
     exact original entry premium, 10% ask **rejection** (never price-chasing),
     60m cancellation, no ADD, full matching exits, dedicated account only,
     and unknown-outcome idempotency.
7. **Deploy only if green:** With entries paused, stop the exact Swift LIVE
   task, perform SQLite **online** backups plus SHA checks, verify source
   package and configuration, run `setup.ps1` in dry-run mode first, apply
   the proven LIVE upgrade path, then start only the original managed LIVE
   task. Verify active SHA, unique worker and consecutive health cycles.
   Roll back code (not unsafe DB downgrades) if acceptance fails; leave
   entries paused.
8. **Market-hours acceptance:** Friday Nov 20 2026 at/after 09:30 ET, or the
   next regular trading session. A separate read-only check at 10:15 ET was
   described in the incident; retrieve its ACTUAL output, do not infer it
   fired. Recheck the three manual option marks in a valid exchange session.
   If they have stale quotes, `DAILY_PNL_UNVERIFIED` remains a **real BLOCK**.
   Verify a fresh real eligible Telegram revision reaches a durable parser
   decision (or an explicit risk rejection) without any stale replay.
9. **Rearm only with unambiguous authorization:** Restarting a paused LIVE
   service is NOT permission to place real orders. Do not call
   `ResumeEntries` while P&L, confirmation, health, approval receipt or
   field checks are unresolved. Obtain explicit owner authorization for
   rearming after acceptance. Never fabricate a trade to prove execution.
10. **Final report:** Installed and remote commit SHAs; exact files/reasons;
    pass/fail/skip test counts; process count; source revision and Telegram
    lag; account-wide P&L gate; notifications; policy blockers; whether LIVE
    is running/paused/armed; whether any real trade occurred (and evidence).
    If no genuinely new alert arrives in market hours, mark end-to-end
    order execution **UNVERIFIED**, not successful.

## Hard stop conditions

Missing installed source, nonmatching account, unsynchronized clock, stale or
missing marks, inconsistent daily risk, unexhausted broker pagination,
confirmation required per order, duplicate workers, invalid receipt, unknown
broker submission outcome, pending old entries, or failed regression tests.
Never clear a safety latch or issue/cancel real orders merely to get a green
status. No destructive cleanup or secret uploads.

## Codex task

Execute the above steps sequentially on the real Windows installation. Make
the code corrections and tests, preserving the actual deployed lineage, then
attempt a **paused LIVE service restart and read-only verification** when
safe. Explicitly identify blockers to live rearming and do not claim
production readiness before they are resolved.
