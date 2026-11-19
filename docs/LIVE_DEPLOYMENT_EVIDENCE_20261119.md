# Live deployment evidence — 2026-11-19 (read-only; no order was placed)

Scope: integration of PR #37 into the consolidated branch, deterministic replay of real Swift
message 14624, read-only Robinhood authorization and P/L evidence for the pinned account
(ending 5140), and the exact owner action still required before real writes.
No `place_option_order` / `cancel_option_order` call was made. Account numbers are masked.

## 1. PR #37 integration
- Reviewed diff (parser `BARE_SOLD`, lineage ownership gate, 8 test groups): no broker, credential or
  write-permission change; ambiguity and unowned exits fail closed.
- PR #37 CI (Phase 0 checks, Ubuntu+Windows x Python 3.11+3.13): 4/4 success.
- Fast-forwarded into `release/swift-consolidated-20261118` (PR #36 branch); `main` untouched.
- Full suite on the integrated tree before the P/L fix: 1073 passed, 2 skipped (symlink and POSIX-mode
  tests that cannot run on Windows).

## 2. Deterministic replay (isolated temp DB + temp journal + `StatefulFakeMCP`, no network)
Real stored text of 14624 (never printed) was replayed through
`TelegramStore -> SignalMatcher -> LiveDispatcher -> LiveBroker`:

| Step | Result |
| --- | --- |
| 14624 parsed | `ENTRY` MO call 77.50 exp 01/15, limit 2.25, SWING (the older parser said `UNRECOGNIZED_OPTION_SYNTAX`) |
| Lineage / contract | `ENTRY / RECORDED`; year resolved from the verified chain (2027-01-15) |
| Entry intent | 1 x `BUY_TO_OPEN`, limit exactly 2.25, one fake-broker order |
| Bare `MO SOLD` before any fill | refused: `NO_MATCHING_BOT_OWNED_POSITION`, 0 sells |
| Fill, then bare `MO SOLD` | exactly one 100% `SELL_TO_CLOSE`; signal `CLOSED` |
| Repeat `MO SOLD` after close | no order |
| Journal | 2 `live_orders` rows; control DB `order_intents` BUY/SELL both `FILLED` |

Re-parse of the 16 stored real messages with the integrated parser: 14624 -> `ENTRY`; 14616
(`SMCI SOLD ...`) -> ownership-gated `CLOSE_100_PERCENT`; no message is parsed as an entry by mistake.

## 3. Robinhood authorization evidence (account ending 5140, nickname "MCP")
Read-only `get_accounts`, `get_trade_approval_setting`, `get_trade_approvals`, `get_option_orders`:

- `agentic_allowed: true`, `option_level_3`, `state: active`.
- `human_must_approve_trades: false` (updated 2026-10-28). `get_trade_approvals` -> `[]`.
- Order history (266 orders, 2026-08-31..2026-11-18): exactly **one** `placed_agent: agentic`
  order ever — XSP buy limit 0.01 on 2026-11-16, state `cancelled`, 0 filled, **no approval
  request recorded**. All 151 fills are `placed_agent: user`.

Conclusion: the account setting and the single agentic placement show no per-order app approval
for that order shape. They do **not** prove that an agentic order **fills** unattended, nor that
every order shape is exempt (Robinhood documents that certain orders still require approval).
No agentic fill has ever occurred on this account. This evidence is documented, not assumed.

## 4. Read-only P/L reconciliation
- Defect found against real data: `_today_activity` scanned the whole order history and raised
  `filled multi-leg order cannot establish daily risk` on manual SPY/CRWV/UBER spreads filled
  Sept-Oct, so the $750 breaker could never be computed (entries denied forever, fail-closed).
- Fix: `LiveBroker._settled_before` skips an order only when both `created_at` and `updated_at`
  parse and are before today's New York date. Verified on all 151 real fills that `updated_at` is never
  earlier than any execution timestamp. Missing/unparseable stamps and any activity today stay
  fail-closed (3 new tests; the daily-loss, latch and overnight-close tests are unchanged).
- With the fix, on real data: NY session 2026-11-19, realized today 0, no activity today, buying
  power 1048.89, open positions 3 (MO, KWEB, NVO: manual longs, not bot-owned, cost ~407).
  The unrealized leg needs quotes <= 30 s old, so the full production gate is **re-run at market
  hours**; after hours it correctly refuses (`stale option quote`). The three manual longs count
  toward the account-wide breaker (conservative); the bot never closes them (ownership gate).

## 5. Arming boundary (owner decision; nothing below was done)
Live writes need all of: `settings.json` + `deployment.json` LIVE, the pinned account, a distinct
`data\live.sqlite3` and journal, **and** the owner's expiring
`secrets\unattended-live-approval.json` (v2, <= 7 days, caps >= configured). The receipt is not
created by this deployment. See `docs/LIVE_OPERATIONS.md` "Going live".

## 6. Windows integration defects found while preparing the live worker (fixed here)
- `service.ps1 -Action RegisterLiveTask` threw `ValidateSetFailure`: its local `$action` overwrote the
  validated `$Action` parameter (PowerShell names are case-insensitive). Renamed to `$taskAction`; the
  `SwiftAutoTrader-Live-Local` task is now registered **Disabled** (it cannot start at logon until armed).
- `secrets.ps1` wrote DPAPI blobs with a trailing CRLF that `ConvertTo-SecureString` rejects (the working
  production blob has none): now `-NoNewline`.
- The mobile kill-switch listener (supervisor already supports it) was never given its token/chat by the
  Windows launcher. Added optional wiring: `secrets.ps1 -Action KillSwitchBotToken` (DPAPI) plus
  `kill_switch_chat_id` in the LIVE settings; the chat may not be the Swift source channel. Verified in a
  synthetic LIVE root (enabled / absent / refused).
- The LIVE settings loader threw under StrictMode when optional keys (`max_contracts_per_signal`,
  `max_daily_loss_usd`, ...) were absent although defaults exist; they are now in the in-memory optional list.
- `data\live.sqlite3` (the LIVE control DB, separate from the paper DB) shipped with entries ALLOWED; it was
  set to BLOCKED (`OPERATOR_PAUSE`) and must stay blocked until the owner resumes it.
- Legacy tasks `SwiftAutoTrader-PaperPilot-Local` and `SwiftAutoTrader-ReceiveOnly-Local` (older install root)
  and the unrelated `SwiftTradesBot` are still At-logon/Ready. The two Swift ones carry their own guard that
  throws when another `SwiftAutoTrader*` task exists (both last ended with exit code 2 = blocked); they were
  not modified. The owner should disable them to remove any doubt. `SwiftTradesBot` was not inspected further.
- Task Scheduler startup delay (previously observed 212 s) is a system-wide logon condition. The operational
  history log is disabled so it could not be re-measured without a reboot; no system setting was changed.
  The service already restarts workers with bounded backoff; expect trading to resume up to ~4 minutes
  after an interactive logon (DPAPI requires the logged-on user).
