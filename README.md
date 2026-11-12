# Swift Options AutoTrader

**Status: PHASE 0 FOUNDATION ONLY — NOT A RUNNING TRADING BOT.**

This private Python 3.11+ project is a **paper-only**, future Telegram-to-options-trading application. Phase 0 provides a secure, independently testable foundation. It does **not** connect to Telegram or Robinhood, parse live signals, simulate order fills, submit broker orders, or install on a Windows computer.

The full authoritative sprint specification is preserved verbatim at [`docs/SOURCE_SPRINT_PLAN.md`](docs/SOURCE_SPRINT_PLAN.md). Its permanent behavioral contract is [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md). Never silently change the behavior described there.

## Safety invariants

- `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`. Attempts to enable live trading **fail startup**. There is no working external broker adapter or login code in Phase 0.
- Only long, single-leg **options** may eventually be eligible. A **LOTTO** label does not exclude an otherwise valid option.
- Never buy again on `ADDING MORE` / averaging-down instructions. `NEW AVG` changes Swift metadata only, not actual fill cost or original limit.
- `SOLD HALF`, other partial sales, `RUNNERS OUT` and `ALL OUT` all require **close 100% of our exact matching filled position**, after safe cancellation of unfilled entry quantity.
- Entry limit must be Swift's **exact original numeric premium**. Do not chase the market, silently round invalid tick sizes, or assume a fill.
- An **accepted** entry's unfilled remainder is subject to cancel-and-verify **3,600 seconds after broker acceptance**, not message receipt or submission. Races/partial fills must be reconciled.
- Contract identity requires symbol, option type, strike, expiration and verified lineage; ambiguity blocks execution.

These behaviors are **requirements for later phases**, not assertions that execution logic already exists. See [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

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

Development dependencies are exact direct pins; the current foundation uses **Python standard library only at runtime**. A fully resolved transitive dependency lock and environment-level reproducibility validation remain open; see [`docs/OPEN_BLOCKERS.md`](docs/OPEN_BLOCKERS.md). CI will install the pinned development tools on Linux and Windows once the repository is published.

## CLI

| Command | Effect |
| --- | --- |
| `smoke` | JSON diagnostic; no filesystem/network writes |
| `config-check` | Validates configuration, outputs nonsecret settings |
| `dry-run` | JSON report confirming no parser/paper execution/live writes exist |
| `migrate` | **Explicit local write** to a new or existing dedicated SQLite database |

All commands fail closed on unsafe environment flags. `.env.example` contains only placeholder keys and safe values. Environment loading is through the process environment; automatic `.env` parsing is not enabled.

## Modules / boundaries

| Area | Phase 0 | Full implementation target |
| --- | --- | --- |
| `telegram` | Event types and nonoperational scaffolding | Phase 1: authorized read-only MTProto listener |
| `parser`, `signals` | Policy markers, strict identity type | Phase 2: deterministic parser and linkage |
| `orders`, `broker` | Order types, deadline calculation, deny-all real broker | Phase 3 paper state machine, Phase 4 MCP adapter |
| `database` | Working SQLite migration and constraints | Phase 1+ durable event store and repositories |
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
