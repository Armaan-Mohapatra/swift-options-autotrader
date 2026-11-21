# Phase 2 — deterministic Swift-style option signal interpreter

**Status:** Implemented and validated on synthetic test cases only. **PAPER_ONLY**; no order execution, no Telegram credentials, no live contract catalog and no paid channel content.

## Execution boundary

The Phase 1 Telegram listener *only stores* messages. The opt-in `parse-replay` command examines stored revisions and creates durable parser decisions/metadata. The command has **no broker integration** and may not place, cancel, or sell orders. Phase 3 will build a separate paper order-state machine; Phase 4 will validate instrument lookup against the supported official brokerage tool. The `option_signals.state='CLOSE_REQUESTED'` marker is an **interpreted request**, not proof any quantity was sold.

CLI example (only after local SQLite migrations and a configured source channel):

```bash
python -m swift_autotrader migrate
python -m swift_autotrader parse-replay --limit 500
python -m swift_autotrader parse-health
python -m swift_autotrader dry-run
```

`SWIFT_TELEGRAM_CHANNEL_ID` must be set for parser commands. `TELEGRAM_ENABLED` can remain `false`; **no Telegram connection is made** by the parser commands. Output is count-only, never private message text or IDs. No account authentication is required for synthetic tests.

**Important fail-closed catalog rule:** `parse-replay` has no live broker instrument validation and deliberately passes no catalog, so syntactically valid entry alerts are recorded as `REVIEW_REQUIRED / INSTRUMENT_CATALOG_REQUIRED`. The `StaticInstrumentCatalog` allows in-process synthetic fixtures to verify format, date, full option identity and ambiguity behavior. Its use in unit tests does **not** mean real option contracts have been verified. Automatic entry eligibility is not available until Phase 4 supplies an independently verified instrument lookup and later paper execution gates.

## Accepted deterministic grammar

Supported long single-leg entry formats require a full contract, date, premium, and exact underlying:

```text
SPY 680C 11/13 @ 1.20
SPY LOTTO 680C 11/13 @ .95
LOTTO SPY 680C 11/13 @ .95
BUY SPY 680CALL 11/13/2026 @ 1.20
QQQ 500P 2026-11-13 @ $1.20
```

Supported option rights: `C`/`CALL`, `P`/`PUT`. Expiry: `MM/DD`, `MM/DD/YY`, `MM/DD/YYYY`, `YYYY-MM-DD`. Decimal parsing uses `Decimal` and preserves an entry's numeric premium exactly; it does **not** snap to ticks, replace it with bid/ask, assume expiration year or place an order. Trade category is `STANDARD`, `LOTTO`, `SWING`, `SCALP`, or `DAYTRADE` as explicitly labeled.

`MM/DD` **must** resolve to precisely one verified, unexpired catalog entry; zero matches or several year matches require review. Even an explicitly supplied expiration year is checked against catalog membership. In the absence of catalog data, all new entries require review. Real-world Swift message variations not represented by tested grammar must also be reviewed, not guessed.

## Follow-up semantics and strict matching

| Synthetic alert | Interpreted intent | Safety consequence |
| --- | --- | --- |
| `SPY ADDING MORE` | `METADATA_ONLY` | Never buy additional contracts |
| `SPY adding more, new avg .95` | `METADATA_ONLY` | Save `swift_reported_avg=0.95`, preserving original `1.20` limit and actual cost basis |
| `SPY NEW AVERAGE .95` | `METADATA_ONLY` | Link to a unique prior signal, never alter fill prices or quantities |
| `SPY SOLD HALF` | `CLOSE_100_PERCENT` | Record a full-close **request** for the matching signal, not a completed fill |
| `SPY SOLD 1/3` / `TRIM` | `CLOSE_100_PERCENT` | Same as above, never trim only our fractional quantity |
| `SPY RUNNERS OUT` / `ALL OUT` | `CLOSE_100_PERCENT` | Same as above; later duplicate exit is an idempotent no-op |
| `BUY 20 SHARES SPY` | `IGNORE_NON_OPTION` | Never treat shares/stock as options |
| `SPY 680C 11/13 @ 1.20 and QQQ ...` | `REVIEW_REQUIRED` | Cannot silently select one alert out of multiple |
| `SPY correction: 680C to 681C` | `REVIEW_REQUIRED` | Never silently modify the original option identity |

Matching uses exact source channel + original message ID, reply lineage, full underlying/right/strike/expiry, and signal ID. Explicit-contract follow-ups may match a unique existing signal even without a reply. A **ticker-only** update is allowed only if precisely one active signal from that channel matches. Conflicting reply ancestry, multiple same-ticker/same-contract signals, missing replies or incompatible contracts are `REVIEW_REQUIRED`; no external LLM decides identity.

Edits to an entry cannot create a second entry or silently change a strike/limit; changed entries are reviewed. An edit of an already-linked average can refresh only Swift's reported average using monotonic message effective times. An edited alert cannot invent a new exit intent. Recovered, stale, non-actionable or superseded Telegram events never open or close positions. Decisions are persisted once per revision and restart-safe, with unique lineage records, append-only average history and deduplicated close requests.

## SQLite objects from `0003_signal_linkage.sql`

`signal_metadata` persists original source identity/category; `signal_message_links` persists exact Telegram message lineage; `signal_parser_decisions` stores every processed revision's classification; `signal_average_history` stores reported averages separately; `signal_close_requests` is an idempotent request marker; `signal_review_queue` preserves ambiguous cases for human action. Existing `option_signals.confirmed_avg_fill_price` is never updated by the interpreter. Existing schema migrations remain untouched.

## Synthetic benchmark — clearly not real Swift accuracy

Curated labeled fixture: `tests/fixtures/swift_phase2_synthetic.json` (81 entirely synthetic alerts). Manually assigned expected intent, independently of parser output. Regenerate via `scripts/build_synthetic_phase2_fixture.py`. All data refer to fictional hypothetical signals, not actual past paid Swift messages.

| Class | Test cases | Expected examples |
| --- | ---: | --- |
| `ENTRY` | 20 | ordinary/LOTTO, calls/puts, numeric/ISO dates |
| `METADATA_ONLY` | 16 | add-only, NEW AVG, revised averages |
| `CLOSE_100_PERCENT` | 16 | SOLD HALF/1/3, TRIM, ALL OUT, RUNNERS OUT |
| `IGNORE_NON_OPTION` | 14 | stock/crypto/futures, ordinary commentary |
| `REVIEW_REQUIRED` | 15 | conflicts, unsupported syntax, invalid price/date |
| **Total** | **81** | **synthetic classification agreement 81/81 = 100%** |

This **in-sample synthetic classification result is not a measured real-world precision, recall, parsing success rate, trade accuracy, win rate, or profitability estimate**. Actual Swift syntax, paid-channel access, contract catalog freshness, live message edits and the timing of follow-up alerts are not validated. A representative, authorized forward-paper dataset is required for real measured accuracy. Tests also cover ambiguity, replay/restart, change history, DB dedupe, matching, average monotonicity, immutable fill basis, and no broker activity.

## Review and future execution gates

Every `REVIEW_REQUIRED` revision stays in SQLite until explicitly resolved. Do not automatically replay it into a later trade. Downstream Phase 3 must independently check option instrument, risk and permissions, real fills, idempotency, accepted order timeout, and matching position quantity. Neither this parser nor its test catalog is authorization for brokerage writes. No broker imports/calls exist in the matcher.

**Full inspectable normalized outputs:** [`PHASE_2_PARSED_EXAMPLES.json`](PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json) records all 81 synthetic messages, their expected intent, observed structured fields, reason codes, and match flags. Regenerate with `python scripts/export_synthetic_parser_results.py`; `test_swift_parser_report.py` prevents snapshot drift.
