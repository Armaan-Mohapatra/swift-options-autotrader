# Swift Options AutoTrader

**Status: PHASE 2 SWIFT-STYLE PARSING + DURABLE SIGNAL MATCHING (SYNTHETIC CI PASSED) — NOT A RUNNING TRADING BOT.**

This private Python 3.11+ project is a **paper-only**, future Telegram-to-options-trading application. Phase 0 provides a secure, independently testable foundation. Phase 1 provides **optional read-only, locally authorized Telegram ingestion** (not enrolled or field tested). Phase 2 adds **deterministic offline Swift-style parsing, SQLite signal lineage, metadata-only average updates, full-close requests and review-required states**, tested on synthetic messages only. It does **not** connect to Robinhood, interpret *real authenticated* Swift content, simulate fills, submit brokerage orders or install on a Windows computer.

The full authoritative sprint specification is preserved verbatim at [`docs/SOURCE_SPRINT_PLAN.md`](docs/SOURCE_SPRINT_PLAN.md). Its permanent behavioral contract is [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md). Never silently change the behavior described there.

## Safety invariants

- `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`. Attempts to enable live trading **fail startup**. There is no working external brokerage adapter or live broker login path in Phases 0–2.
- Only long, single-leg **options** may eventually be eligible. A **LOTTO** label does not exclude an otherwise valid option.
- Never buy again on `ADDING MORE` / averaging-down instructions. `NEW AVG` changes Swift metadata only, not actual fill cost or original limit.
- `SOLD HALF`, other partial sales, `RUNNERS OUT` and `ALL OUT` all require **close 100% of our exact matching filled position**, after safe cancellation of unfilled entry quantity.
- Entry limit must be Swift's **exact original numeric premium**. Do not chase the market, silently round invalid tick sizes, or assume a fill.
- An **accepted** entry's unfilled remainder is subject to cancel-and-verify **3,600 seconds after broker acceptance**, not message receipt or submission. Races/partial fills must be reconciled.
- Contract identity requires symbol, option type, strike, expiration and verified lineage; ambiguity blocks execution.

The **message-classification and matching rules** now exist in Phase 2, but **order placement, fill reconciliation and cancellation rules are requirements for later phases**, not working execution logic. See [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

## Quick start (Python 3.11+)

```bash
python -m venv .venv
# Windows: .venv\Scripts\python.exe -m pip install -e ".[dev]"
# Linux/macOS:
.venv/bin/python -m pip install -e ".[dev]"
python -m swift_autotrader smoke
python -m swift_autotrader config-check
python -m swift_autotrader dry-run
python -m pytest -q
python -m ruff check .
python scripts/scan_secrets.py
```

The three diagnostic commands are side-effect free. For an **explicit local database** initialization (creates a new SQLite database at `DB_PATH`):

```bash
python -m swift_autotrader migrate
```

The migration creates tables for raw events, signals, order intents, fill records, state history and checkpoints. WAL mode is requested and migration hashes prevent silently editing previously applied migrations. No real data is imported. Never point `DB_PATH` at an existing unrelated database.

A universal `uv.lock` now pins Python dependencies with artifact hashes; the optional read-only Telegram adapter uses pinned Telethon. CI uses `uv sync --locked --extra dev --extra telegram` on Linux/Windows Python 3.11/3.13. Signed supply-chain attestations, offline-hermetic rebuilding and independent advisory audits remain open (see [`docs/OPEN_BLOCKERS.md`](docs/OPEN_BLOCKERS.md)). [Phase 2 CI evidence](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43291903449).

## CLI

| Command | Effect |
| --- | --- |
| `smoke` | JSON diagnostic; no filesystem/network writes |
| `config-check` | Validates configuration, outputs nonsecret settings |
| `dry-run` | JSON report confirming no order execution or external broker writes exist |
| `migrate` | **Explicit local write** to a new or existing dedicated SQLite database |
| `telegram-health` / `telegram-listen` | Phase 1 read-only local-source diagnostics / authorized opt-in listener |
| `parse-replay --limit 500` | Phase 2: interpret stored Telegram revisions offline; persist classifications and human-review metadata, **no orders** |
| `parse-health` | Phase 2: read parser metadata-only counters from local SQLite |

All commands fail closed on unsafe environment flags. `.env.example` contains only placeholder keys and safe values. Environment loading is through the process environment; automatic `.env` parsing is not enabled.

## Modules / boundaries

| Area | Phase 0 | Full implementation target |
| --- | --- | --- |
| `telegram` | Phase 1 read-only authorized MTProto listener, synthetic tests | Requires later local authorization/forward verification |
| `parser`, `signals` | Phase 2 executable deterministic parser, strict signal lineage, SQLite decisions/review queue | Real Swift grammar and option listings still require forward validation |
| `orders`, `broker` | Order types, deadline calculation, deny-all real broker | Phase 3 paper state machine, Phase 4 MCP adapter |
| `database` | Phase 0/1/2 WAL migrations: Telegram revisions, signals, averages, close requests and reviews | Phase 3+ confirmed executions and order reconciliation |
| `analytics`, `dashboard`, `notifications` | Nonoperational namespaces | Phase 5 verified paper reporting |
| `deployment/windows` | Explicit no-install healthcheck stub | Phase 6 installer and supervisor |

## Development rules

1. Work on a **new feature branch and pull request per phase**; review before merging. Do not merge, deploy, authenticate or turn on live trading automatically.
2. Preserve existing source and any previous work; do not touch Phoenix, Kalshi, or other repositories.
3. Keep CI mock-only and paper-only, including after tests are rewritten or dependencies updated.
4. Each phase publishes an evidence-based report with exact commit SHA, PR, Windows/Linux CI conclusions, test counts, limitations, licensing and next phase handoff.
5. Maintain `docs/PROJECT_SPEC.md` as a fixed behavior contract; changes require explicit authorization and explanation.

## Documentation

[`AGENTS.md`](AGENTS.md) · [`PROJECT_SPEC`](docs/PROJECT_SPEC.md) · [`CONFIGURATION`](docs/CONFIGURATION.md) · [`SECURITY`](docs/SECURITY.md) · [`IMPLEMENTATION_STATUS`](docs/IMPLEMENTATION_STATUS.md) · [`PHASE_TRACKER`](docs/PHASE_TRACKER.md) · [`OPEN_BLOCKERS`](docs/OPEN_BLOCKERS.md) · [`ISSUES`](docs/ISSUES.md) · [`WINDOWS_RUNBOOK`](docs/WINDOWS_RUNBOOK.md) · [`LIVE_ACTIVATION_CHECKLIST`](docs/LIVE_ACTIVATION_CHECKLIST.md)

**Important:** No strategy's profitability has been validated. No fills or account activity are implied by the CLI, database tables or mocks.

## Phase 1 read-only listener

See [Telegram operator guide](docs/TELEGRAM_INGESTION.md) and [Phase 1 checkpoint](docs/PHASE_REPORTS/PHASE_1.md). The optional `Telethon==1.45.0` client requires an already-authorized local user session, never CI credentials. `telegram-health` reads local metadata; `telegram-listen` requires manual opt-in plus migration. The Telegram/event-store and deterministic parser/signal-linkage rows are now functional on synthetic tests; **Phase 3+ execution remains out of scope**.

## Phase 2 deterministic interpreter and evidence

See [Phase 2 parser guide](docs/SWIFT_PARSER_PHASE2.md), [checkpoint](docs/PHASE_REPORTS/PHASE_2.md) and [81 fully normalized synthetic examples](docs/PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json). Phase 2 passed 208 tests/job across Linux and Windows Python 3.11/3.13 (https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43291903449). `LOTTO` is eligible under the same parser rules; `ADDING MORE` never buys again; `NEW AVG` never changes confirmed fill cost; all partial/runner exits record **CLOSE_100_PERCENT requests**, not completed sales. Without a verified options catalog, entry candidates go to `REVIEW_REQUIRED` rather than auto-trade. No real paid Swift content was used.
