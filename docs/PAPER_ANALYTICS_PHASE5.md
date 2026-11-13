# Phase 5 — offline paper replay, provenance-aware metrics and localhost monitoring

**Status:** synthetic implementation, not an authenticated Swift/Robinhood field study and **not a source of verified trading performance**. This phase adds no live-trading authorization or broker write interface. Permanent strategy rules remain in [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

## Core engineering boundaries

1. The actual deterministic `SignalMatcher` consumes messages recorded by the authorized-format `TelegramStore`, then the unchanged `PaperOrderEngine` creates at most one durable entry/close intent per resolved signal. Replay runs in a **new temporary SQLite database**, never in the configured production `DB_PATH`. Historic source text is simulated as incoming only inside that disposable database; historical messages are never reintroduced to the real listener's freshness checkpoint. The replay output omits message bodies.
2. The original numeric Swift premium is always the LIMIT price. The replay never replaces it with a quote or chases price. LOTTO has identical entry rules. ADD/AVERAGING DOWN creates no new buys. NEW AVG modifies only Swift metadata; actual average fill cost comes from confirmed simulated buy fills.
3. Any correctly linked SOLD HALF/SOLD 1/3/TRIM/RUNNERS OUT/ALL OUT requests to close **100% of our filled quantity**. The existing engine first cancels a pending entry remainder, waits for confirmation, and uses a fresh quoted BID for a sell-to-close LIMIT. A requested exit is not proof of a sale. A resting sale remains an open exposure.
4. Synthetic paper fills require **explicit dated bid/ask observations** and available displayed quote size. The model does not invent an intrabar high, low, midpoint fill, or quote. No historical quote => no simulated fill. Quotes are ingested at their stated timestamps only; an order created after an observation cannot retro-fill on that observation. The existing paper broker's price-crossing model is reused, with optional *nonnegative* modeled slippage and a stricter crossing threshold. A buy can never fill above its original limit and a sell cannot fill below its sell-to-close limit.
5. Quotes, option existence, permissions, session-open flags, buying power, fee schedule and tick size in the **replay input are user-supplied assumptions**. This is not a live verified options catalog. The normal `paper-cycle` command's fail-closed no-real-catalog gate remains untouched. The Robinhood MCP adapter remains write-disabled.

## Historical replay input format

Only local JSON files are accepted (up to 4 MB; max 20,000 ordered events and 500 explicit option contracts). Supply a source label of `SYNTHETIC` or `HISTORICAL_UNVERIFIED`; the program rejects claims of live/verified results. Do not store paid Telegram content or account identifiers in GitHub. Keep any authorized paid-history inputs **only** on the separately approved local machine. Example sanitized fixture: [`tests/fixtures/swift_phase5_replay_synthetic.json`](../tests/fixtures/swift_phase5_replay_synthetic.json).

```json
{
  "source_type": "SYNTHETIC",
  "channel_id": -1001234567000,
  "catalog": [{"symbol":"SPY", "right":"CALL", "strike":"680", "expiration":"2026-11-13", "tick":"0.01", "options_permission":true, "market_session_open":true, "buying_power_usd":"10000"}],
  "risk": {"contracts_per_signal":2, "premium_per_signal_usd":"1000", "total_exposure_usd":"3000", "open_signal_count":5},
  "costs": {"fee_per_contract_side_usd":"0.65", "slippage_premium":"0.00", "mark_max_age_seconds":120},
  "events": [
    {"kind":"message", "at_utc":"2026-11-12T15:00:00Z", "message_id":1, "text":"SPY LOTTO 680C 11/13 @ 1.20"},
    {"kind":"quote", "at_utc":"2026-11-12T15:00:30Z", "quote_id":"synthetic-q1", "contract":{"symbol":"SPY", "right":"CALL", "strike":"680", "expiration":"2026-11-13"}, "bid":"1.10", "ask":"1.15", "bid_size":10, "ask_size":2},
    {"kind":"message", "at_utc":"2026-11-12T15:01:00Z", "message_id":2, "reply_to_message_id":1, "text":"SPY new avg .95"}
  ],
  "end_at_utc":"2026-11-12T15:05:00Z"
}
```

`kind=edit` requires an earlier same-message source event. Reply IDs must link to a uniquely known source; uncertain ticker-only follow-ups must enter the parser review queue. Event timestamps must be timezone-aware and input-sorted; naive/out-of-order timestamps fail closed. The synthetic per-contract catalog is an explicit test source, **not proof** of a contract on Robinhood or elsewhere. Quote `source_reference` may be included; only its SHA-256 is recorded, not a private URL or content. For explicit session transitions, add `kind=session_close` or `kind=session_open` at the recorded UTC time. GFD cancellation on close is modeled; no canceled prior entry is reopened next session.

```bash
# Run on a dedicated test checkout, with no real credentials or production DB:
python -m swift_autotrader paper-replay --input tests/fixtures/swift_phase5_replay_synthetic.json
python -m swift_autotrader paper-replay --input tests/fixtures/swift_phase5_replay_synthetic.json --compare
python -m swift_autotrader paper-report
python -m swift_autotrader paper-notify-scan
python -m swift_autotrader paper-dashboard --host 127.0.0.1 --port 8765
```

`--compare` runs each of three assumptions in separate DBs: **touch** (a buy may fill when ASK + modeled slippage <= entry limit; sell may fill when BID - modeled slippage >= exit limit), **through** (requires an additional favorable tick beyond the order limit), and **no_fills** (stress control; no quote-driven execution). This is not an optimization or historical proof; results are a sensitivity comparison for an explicit, limited dataset. Price improvement does not relax the original limit. Quotes lacking depth have zero fill capacity. Market-close and 3,600-second deadlines are enforced by the existing engine.

## Metrics and evidence status

`PaperAnalytics` reads only `order_fills`, `order_intents`, `option_signals`, parser decisions and dated option quote evidence. `option_signals.swift_reported_avg` is NEVER substituted for `confirmed_avg_fill_price`. We compute first-in-first-out realized P&L and cost basis from actual simulated fill rows, with **standard equity-option multiplier 100**. Nonstandard multipliers or non-equity option contracts require additional explicit support and are not valid evidence for this model. Partial fills remain open; unfilled entries contribute no realized gain/loss. A quote used for a fill is the contemporaneous ASK for buys and BID for sells; quoted spread therefore enters modeled transaction costs. A clearly specified nonnegative slippage assumption changes modeled fill price while respecting order limits.

- `realized_gross_usd`: closed quantity times actual simulated sell-minus-buy premium times 100, FIFO.
- `realized_net_usd`: subtract modeled fee per contract per side **only when an explicit fee is supplied**. Missing fee means `null`, not zero. `fees_paid_usd` counts modeled fees on all buy/sell fills. Slippage is already reflected in modeled fill prices.
- `unrealized_gross_usd`: remaining long quantity valued at **fresh observed BID** minus FIFO acquisition basis; missing/older-than-policy mark makes this `null`, not zero. Unrealized net additionally deducts assumed acquisition/exit fees when known; this is an estimate, not a realizable fill guarantee.
- `win_rate`: percentage of completely closed simulated positions with positive net P&L if fee known, otherwise positive gross P&L. Not a Swift historical accuracy statistic and undefined (`null`) with zero completely closed positions.
- `realized_max_drawdown_usd`: closed-fill-only cumulative P&L peak-to-trough, labeled as such; not total marked-equity drawdown. Reported quote spread is the arithmetic average of observed BID/ASK differences, **not** an extra double-counted fee.
- `pending_orders`: original limit, remaining contracts, actual acceptance, deadline and countdown; an active sell with no expiration remains unresolved. `orders_by_state`, parser review/ignore counts, canceled/rejected counts and local alert codes are visible.

All exported replay performance carries `UNVERIFIED_ESTIMATE` and `USER_SUPPLIED_NOT_BROKER_VERIFIED`; even a fully populated synthetic example is *not* an authenticated backtest. If prices are absent, output includes `not_evaluable_without_quotes`. Missing/illiquid bid/ask data, unknown exchange queue position, real fee schedules and unsupported instrument multipliers are not silently approximated.

## Dashboard and local notifications

`paper-dashboard` uses only Python's standard library. It binds **exclusively to `127.0.0.1`** (default port 8765). The status API and HTML/JS have no trade submission, cancel, config mutation or Telegram posting endpoints. POST returns 405; Host validation rejects arbitrary origin/DNS rebinding; CSP, no-cache, nosniff and frame-denial headers are enabled. The UI makes no external asset or analytics calls. Source message **text is excluded from API and GUI by default**: intent, status, reason, time and synthetic identifier are shown instead. Paid data may reside only in the authorized local SQLite store and must never be deployed publicly. In this phase there is no login/auth layer for remote clients, so binding must never be broadened beyond loopback.

`NotificationCenter` persists deduplicated metadata-only notices for unfilled exits, broker/paper reconciliation errors, Telegram disconnections/degradation and explicit application failure codes. It is a **local SQLite outbox**, not an externally delivered SMS, Telegram bot, email or OS push. Phase 6 or separately authorized notification setup is required for unattended delivery. No message bodies, account numbers or bearer tokens are included in these notices. The historical replay is also not an unattended scheduler.

## Test strategy and blockers

`pytest` covers quote-based fills and nonfills, no lookahead, 60-minute cancellations, ADD/NEW AVG separation, close 100%, FIFO accounting, multiplier/fee/slippage, missing quotes, alternative assumptions, session close, malformed input, deterministic replay, localhost HTTP method/origin restrictions and local notification restart/deduplication. CI matrix is Linux/Windows × Python 3.11/3.13 using the existing locked development dependencies; no optional Robinhood SDK or live session is used. Full test evidence appears in [Phase 5 checkpoint](PHASE_REPORTS/PHASE_5.md).

**Remaining:** authentic Swift history, actual options NBBO/depth, execution timestamps/fees and approved market contract catalogs are not supplied. So this deliverable cannot estimate a reliable real-world win rate, P&L, missed fills, strategy quality or profitability. Phase 6 Windows 24/7 installation/supervision and Phase 7 full audit are not started. No live trading, local host access or automatic PR merges are authorized.
