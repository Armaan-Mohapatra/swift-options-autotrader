# LIVE path audit, 2026-11-21 (offline; no broker, Telegram or credential access)

Scope: every code path that can reach `place_option_order` or `cancel_option_order`, plus
Telegram ingestion, stale-signal handling, account pinning, idempotency, unknown outcomes,
partial fills, full exits, restart recovery and the kill switch.

## 1. Write surface

- Only `src/swift_autotrader/broker/live_broker.py::LiveBroker` calls the two write tools
  (`_call_write`). `RobinhoodMCPBroker` raises `BrokerWritesDisabledError` on `submit_order`
  and holds `WRITE_TOOLS` as a deny list only.
- `_call_write` refuses: rehearsal mode, writes disabled, any tool other than the two,
  any account other than the pinned one, multi-leg orders, and any leg that is not
  a long open (`buy/open`) or an owned close (`sell/close`).
- The kill switch is re-checked immediately before the MCP call for every entry. If it
  trips after the durable `write_attempted` marker, the order stays UNKNOWN, never released.
- `submit_order` checks the journal first and never sends a second place for the same
  logical id, idempotency key, or signal+side.

## 2. Entry gates (`LiveBroker.submit_order` path)

Verified in code and covered by `tests/unit/test_live_broker.py`:

- Entry must be verified as an original ENTRY by `signal_gate` (ADD / NEW AVG denied).
- Exactly one contract. Limit is the exact alert price; no price chasing.
- Premium `limit x multiplier <= PREMIUM_CEILING_USD` ($999).
- Current ask must not exceed 1.10 x alert limit (rejection guard, never a raised limit).
- Crossed or missing quotes denied. Off-tick prices denied.
- Buying power checked at send, with an in-journal reserve against parallel overcommit.
- Identical unknown-outcome order blocks a retry.
- `SLIPPAGE_TOLERANCE_PCT != 0` is refused at startup (`config.py`).

## 3. Telegram and stale signals

- Ingestion and parsing are covered by `tests/unit/test_telegram_listener.py`,
  `test_telegram_repository.py`, `tests/integration/test_live_dispatch.py`.
- `test_entry_pending_past_alert_age_expires_and_never_trades` and
  `test_stale_unrecognized_alert_is_closed_explicitly_and_never_trades` cover staleness.
- Only the original ENTRY buys; ADD, ADDING MORE, AVERAGING DOWN, SCALE IN are ignored.
- Ticker-only ambiguous exits are not adopted (`test_exit_never_adopts_manual_contract_inventory`).

## 4. Account pinning

- `_account()` requires exactly one pinned account, `agentic_allowed is True`.
- Individual or absent accounts are refused (`test_mcp_account_pin_rejects_individual_or_unauthorized`).
- `_call_write` re-checks the account on every write payload.

## 5. Unknown outcomes and restart

- A failed or disconnected place is never resent. Reconciliation against order history
  decides the state (`test_disconnect_never_triggers_duplicate_submission`).
- **New this session:** `tests/integration/test_live_restart_recovery.py` restarts a fresh
  LiveBroker on the same journal after (a) an accepted-but-unacknowledged send, (b) a crash
  before the send reached the broker, and (c) a re-delivered signal with a changed limit.
  None produces a second place call. Case (b) stays UNKNOWN and cancel refuses to guess an id.
- Cancel of an unknown order with no broker id is refused, not guessed.

## 6. Partial fills, exits and expiry

- Exits sell exactly the confirmed filled quantity of the owned contract
  (`test_full_exit_after_partial_fill_sells_exactly_the_filled_quantity`,
  `test_full_exit_requires_confirmed_ownership`).
- Cancel after 60 minutes from broker acceptance, once, preserving fills
  (`test_cancel_after_60_minutes_once_and_preserve_fills`).
- Inconsistent partial fills fail closed (`test_inconsistent_partial_fill_fail_closed`).

## 7. Kill switch

- Blocks new entries and allows owned exits and cancellations
  (`test_kill_switch_blocks_entry_but_allows_owned_exit`).
- Rechecked at the transport boundary (`test_kill_switch_rechecked_at_transport_boundary`).
- Telegram kill-switch commands and unauthorized-sender rejection are covered by
  `tests/unit/test_kill_switch.py`.

## 8. Findings

No defect requiring a code change was found in the write path during this audit.
The minor observations below are not defects:

- `_cancel` uses a hard-coded `option_level_2` only to construct a payload that does not
  read the level. Harmless; left unchanged.
- `_cancel` returns from inside a `with self._db()` block after `BEGIN IMMEDIATE`. The
  context manager commits on exit, so no transaction leaks.

### Contract inconsistencies found (owner decision required, NOT changed)

1. **Optional LIVE aggregate exposure cap.** `config.py` accepts `MAX_TOTAL_EXPOSURE_USD`
   in LIVE mode, and `risk.py` documents it as an "optional voluntary cap". `AGENTS.md`
   and `CLAUDE.md` say LIVE has no aggregate-exposure cap. If an operator sets the variable,
   LIVE entries become capped even though the owner contract forbids it. The owner should
   decide whether to refuse the variable in LIVE (a tightening, safe to make) or accept it
   as a voluntary operator cap.
2. **Stale "$750" statements.** Corrected in this branch: `live_dispatch.py` docstring,
   `PRODUCTION_ACCEPTANCE_HANDOFF.md`, and a superseded notice on
   `UNIFICATION_ACCEPTANCE_PLAN.md`. `LIVE_OPERATIONS.md` already states LIVE has no $750 limit.
3. **v3 receipt.** `build_receipt` writes version 3 and `unattended_approval.py` requires
   `daily_loss_policy == NO_DAILY_LOSS_STOP`. Tests reject v2 and mismatched policy. Consistent.

## 9. Not verified here

- Real Robinhood responses, OAuth, agentic permission, option quotes and fills.
- Real Telegram delivery, edit and reconnect behaviour.
- Any LIVE behaviour on the Windows laptop.
