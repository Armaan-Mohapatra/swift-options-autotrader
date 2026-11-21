# Swift AutoTrader: project guide for Claude Code

Binding rules live in `AGENTS.md` and `docs/UNIFICATION_ACCEPTANCE_PLAN.md`. This file is the
short architecture and workflow summary. Nothing here authorizes a real order.

## Owner trading contract (never weaken)

- Only original Swift options ENTRY alerts buy. LOTTO is eligible. ADD / ADDING MORE / AVERAGING
  DOWN / SCALE IN never buy. NEW AVG is informational and never changes an open limit.
- One LONG contract per valid entry. No trade-count cap and no aggregate-exposure cap: available
  buying power bounds exposure. Never short naked options.
- Entry limit is the EXACT alert price. A 10% ask deviation is a REJECTION guard inside
  `LiveBroker`, never an allowance to raise or chase the limit. `SLIPPAGE_TOLERANCE_PCT > 0` is
  refused at startup.
- Premium ceiling $999 per entry including the multiplier. LIVE has no daily-loss latch and no P&L
  entry gate (owner decision, Issue #42; `NO_DAILY_LOSS_STOP`). PAPER keeps its $750 breaker.
- Unfilled entry remainder is cancelled 60 minutes after BROKER acceptance. Matching SOLD HALF /
  TRIM / SCALED OUT / RUNNERS OUT / ALL OUT callouts sell the full owned filled quantity of the same
  contract and signal ancestry, never manual inventory inferred from ticker.
- Only the dedicated Robinhood MCP agentic account, pinned locally (DPAPI). Never the Individual
  account. Never bypass Robinhood's own pre-trade review, confirmation or permission flow.

## Architecture

- ONE live writer: `broker/live_broker.py::LiveBroker`. It owns the journal-first placement
  (separate live journal), unknown-outcome reconciliation, loss latch, quote guard, kill switch and
  exact-contract exits. The read adapter `robinhood_mcp.py::RobinhoodMCPBroker` can never write.
- ONE live service: `live_service.py` + `live_dispatch.py` (`app live-dispatch`). It adds the
  PR #32 protections: unattended-approval receipt (v3 for LIVE; v2 is refused), supervised
  acceptance scope, exit interlocks, credential-path hardening, stop markers. `LiveBroker.submission_guard` re-checks the
  scope at the write boundary. `paper-dispatch-service` refuses live mode.
- One shared authenticated MCP client (single OAuth session); `LiveBroker` runs on that loop via
  `live_runtime.BorrowedMCPLoop`.
- Order history is read to exhaustion (cap 100 pages) as the coverage proof; otherwise it fails
  closed. Real response shapes are in `LiveBroker`'s docstring and `tests/fixtures/live_mocks.py`.
- Hard maxima live in `risk_limits.py`. Settings in `config.py`; Windows deployment in
  `deployment/windows/` (PowerShell 5.1 compatible).

## Workflow and safety

- Python 3.11 and 3.13 on Windows. Run tests with machine env isolated: `tests/conftest.py` strips
  ambient `PAPER_*`/`LIVE_*` variables (another bot sets `PAPER_ONLY=0` machine-wide).
  Never change machine-wide environment variables.
- Defaults stay `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`.
- Never place or cancel real orders, start a live scheduled task, print secrets, account numbers
  or paid Telegram text, or merge/deploy without separate explicit owner approval.
- Missing evidence fails closed. Do not edit a failing test to go green unless the test is
  demonstrably wrong. Do not use paid GitHub Actions; test locally.
- Useful: `/ecc:orch-fix-defect` (failing test first), `/ecc:python-review`, `/code-review`,
  `/ecc:security-scan`, `/ecc:test-coverage`.
