# Phase 3 — durable synthetic PAPER order engine

**Status:** implemented and tested with synthetic data; **not deployed, not a real broker adapter, not proof of profitability.** Code: `src/swift_autotrader/orders/` and `src/swift_autotrader/broker/paper.py`. Contract authority: [source sprint plan](SOURCE_SPRINT_PLAN.md) and [permanent specification](PROJECT_SPEC.md). Do not change these contractual rules for an easier fill.

## Architecture and trust boundary

`TelegramStore → SignalMatcher → option_signals/signal_close_requests → PaperOrderEngine → PaperBroker → SQLite order/fill audit`.

Phase 3 consumes **only** a previously recorded, fresh, uniquely linked Phase 2 parser decision with status `RECORDED`, intent `ENTRY`, reason `ENTRY_VALIDATED_NOT_EXECUTED` and a validated exact option contract. It will not interpret raw Telegram text, select a contract by ticker, resolve uncertain expiration year, trade shares/futures/crypto or infer missing quote data. Pending/ambiguous/outdated parser records do not open paper entries. LOTTO is not a separate risk or price rule. ADD, SCALE IN, AVERAGING DOWN never produce another opening order; revised Swift averages only change parser metadata and never overwrite the original limit or confirmed fill-weighted cost basis.

The engine constructor accepts **only the concrete local `PaperBroker`**. The official Robinhood adapter remains a deny-all placeholder; no network or account credentials exist in this phase. No new runtime dependencies were added, and the universal lock remains in use.

## Durable identity, submission and reconciliation

- `0004_paper_orders.sql` adds `paper_broker_orders`, `paper_broker_fills`, `paper_engine_control`, `paper_engine_decisions`, `paper_engine_alerts` and `paper_quote_observations`. All prior migrations remain immutable.
- Each signal gets one immutable opening identity `<signal_id>:entry` and, if necessary, one closing identity `<signal_id>:close`. The intent, exact Decimal limit and unique derived idempotency key are committed **before** the simulated broker call.
- A paper broker submission with the same ID/key and identical details reuses the durable order. A duplicate key with changed contract/side/quantity/limit is rejected. Crash/network ambiguity remains alert-worthy; never mint a different key to retry.
- Broker acceptance and fills are maintained in a separate SQLite paper broker journal. Engine reconciliation validates contract/right/strike/expiry, side, quantities, limit, idempotency, acceptance timestamp, legal state transition, unique immutable fills and exact cumulative quantities. Conflicting details fail closed with `RECONCILIATION_REQUIRED`.
- `confirmed_avg_fill_price` is recalculated solely from accepted buy fill quantities and prices, never from `swift_reported_avg`. Restart resumes the journal/intent state without a new source signal.
- Engine cycles acquire an OS-local nonblocking single-instance lock on Linux/Windows and serialize SQLite transactions. Never store the SQLite database on a network file share.

## Entries and the acceptance-based timer

For a valid, fresh and unique entry, require an exact verified test instrument, tick size, options permission, market session, positive modeled buying power and explicit finite risk ceilings. Default premium and total exposure limits are **$0**, so no buys pass until the local test harness explicitly configures them. There is **no** current-price distance/chase filter. A $1.20 original premium remains a $1.20 buy-to-open LIMIT whether a synthetic market quote is higher or lower. Invalid tick is rejected, never rounded without permission.

Only a broker-confirmed `accepted_at_utc` starts the timer. Entry deadline is `accepted_at_utc + timedelta(seconds=3600)` in elapsed UTC. At 59 minutes the unfilled remainder rests. At 60 minutes the engine requests cancellation and checks current status. It may observe `CANCEL_PENDING`, `CANCELED`, `FILLED`, or partial fills; late fills before final cancellation are kept and managed. A session-close cancellation may happen earlier. A canceled order is never reopened automatically next session.

## Full exits and safeguards

Any correctly linked `SOLD HALF`, `SOLD 1/3`, `TRIM`, `SCALED OUT`, `RUNNERS OUT`, `ALL OUT` etc. is a **100% exit request for our confirmed net quantity**, not Swift's percentage. If a buy order is still active, request cancellation of its unfilled remainder and wait for final confirmation; a fill racing cancellation increases the quantity that must be closed. Submit at most one durable matching sell-to-close intent for exactly `buy_fills - prior_sell_fills` when it is positive.

A sell price is **never guessed**: require an explicitly supplied contemporaneous BID for the same exact contract and valid option tick, then use that BID as the sell-to-close LIMIT. No market orders, no forced stale quote, and no assumption that an unfilled sell order made the position flat. Missing/stale quote, rejection, partial close or unknown broker state raises a persisted alert for manual review. The paper model has no realistic slippage, fees, liquidity queue priority or full intraday bid/ask history; synthetic fill results are **not realized brokerage P&L**.

`paper_engine_control` persists pause and emergency-stop flags across restarts. Pause blocks new entry orders; emergency stop persists both stop and pause and requests cancellation of remaining resting opening orders. It does **not** automatically liquidate unrelated holdings. Reset requires explicit manual approval and remains paused. No unattended service/task is installed.

## Commands and reproduction

Run isolated smoke/migrations only in a dedicated test environment:

```bash
uv sync --locked --extra dev --extra telegram
uv run --locked --extra dev --extra telegram python -m swift_autotrader migrate
uv run --locked --extra dev --extra telegram python -m swift_autotrader paper-health
uv run --locked --extra dev --extra telegram python -m swift_autotrader paper-pause
uv run --locked --extra dev --extra telegram python -m swift_autotrader paper-emergency-stop
uv run --locked --extra dev --extra telegram python -m pytest -q
```

`DB_PATH` must point to the dedicated local SQLite file. The operator CLI `paper-cycle --confirm-simulation` **intentionally rejects** absent verified catalog/preflight instead of permanently marking authentic source signals blocked. Actual simulated cycles are exercised by Python tests with supplied `PaperPreflight`, `RiskCeilings`, fresh `ExitQuote` and a controlled clock. Do not change this gate without independent validation.

## Test evidence and remaining work

GitHub Actions runs Linux/Windows × Python 3.11/3.13 with a locked install, Ruff, pytest, compilation, dependency lock/URL/SHA audit and heuristic credential scan. [Phase 3 checkpoint](PHASE_REPORTS/PHASE_3.md) documents actual full SHAs, the final run and test totals.

Synthetic tests assert original limit, LOTTO parity, no adding, Swift average separation, unique contracts, persisted accepted+3600 timeout, 59/60-minute boundary, cancel/fill racing, no fills, partial and complete fills, duplicate messages/order IDs, unknown statuses, broker rejections, local lock, restart and synthetic parser-to-order-to-exit flow. These tests **cannot validate** live Telegram formats, a real options catalog, broker permissions, actual market prices/fills, production uptime or profitability.

**Phase boundaries:** Phase 4 is read-only official Robinhood MCP contract checks and local-auth planning; Phase 5 is provenance-aware replay/performance/dashboard; Phase 6 is a separate reviewed Windows installation package; Phase 7 is comprehensive audit. No phase is automatically advanced, and nothing authorizes real orders.
