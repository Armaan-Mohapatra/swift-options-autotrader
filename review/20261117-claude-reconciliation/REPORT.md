# Integration reconciliation — 2026-11-17

Branch `integration/claude-reconciliation-20261117`, based on
`fix/exit-safety-interlock-20261116` @ `e995adc` (the newest branch tip).
No live orders were placed, no broker or Telegram connection was made, and no
scheduled task was registered. Everything here ran offline on Linux.

## Baseline reconciliation

- `e995adc` already contains every PR branch (#1–#31) **except** branches that
  only hold review/evidence documents: `audit/antigravity-mvp-review-20261115`
  (4 docs, PR #27 head), 5 `review/*` acceptance-report branches, and
  `codex/phase8-unattended-paper-integration` (#19, superseded by #20/#21).
  The review branches hold documents only. #19 changes `paper_service.py` and
  `windows_runtime.py` against the Phase 7 base; its approach was replaced by
  #20/#21 and was not re-applied. PR #30's fixes are included
  (`fix/windows-service-followup-20261116` is an ancestor).
- Baseline at `e995adc`: **828 passed**, 0 failed (Python 3.13.18, Linux).
- **The repository is PUBLIC.** It was meant to be private. A history scan
  (all branches) found only synthetic channel IDs/accounts and no tokens; one
  path `C:\Users\armaan` appears in docs. Making the repository private is an
  owner action in GitHub settings.

## Defects fixed

| # | Defect | Root cause | Fix | Regression |
|---|--------|-----------|-----|------------|
| 1 | Live entries chased the market | `orders/slippage.py` raised the buy limit to the current ask (10% default) and tick-rounded up — violates AGENTS.md rule 3 / owner rule | Module and setting removed; entry limit is the exact alert premium for any ask; off-grid premium → `INVALID_PREMIUM_TICK`; non-zero `SLIPPAGE_TOLERANCE_PCT` refused | `test_order_engine_live`, `test_live_dispatch`, `test_live_e2e_scenarios` scenario 2, `test_config` |
| 2 | Reply exits silently dropped | First word of `SCALED OUT`, `TRIMMED`, `GREAT ALL OUT`, `I SOLD HALF` etc. parsed as a ticker → `REPLY_CONTRACT_MISMATCH`, position left open | Exit/add/average vocabulary and filler never count as a ticker; emoji, `$TICKER`, `LOTTO:` tolerated without changing exact premium | `tests/integration/test_exit_keyword_lineage.py` (50 cases; 26 fail on the old code) |
| 3 | Live inherited caps nobody set | `LiveDispatcher` mapped a $0 premium cap to $9,999 and hardcoded $10,000 exposure | Live requires explicit positive `MAX_PREMIUM_PER_SIGNAL_USD` and new `MAX_TOTAL_EXPOSURE_USD` (premium ≤ total); used exactly | `test_config`, `test_live_dispatch` |
| 4 | Unrestricted live started on flags alone | Full multi-entry live loop (e.g. via `RegisterLiveTask`) needed no owner decision beyond config + OAuth | Requires `secrets/unattended-live-approval.json`: pinned-account hash, ≤7-day window, caps the config may not exceed; loop stops at expiry | 17 cases in `test_live_service.py` |
| 5 | Undefined names | Missing `Any`, `Mapping`, `SimpleNamespace` imports | Imports added | ruff F821 clean |
| 6 | CI never reached tests | Windows autocrlf checkout broke the release builder's blob-byte check; `ruff check .` failed on ~880 old findings | `.gitattributes` `eol=lf`; lint excludes `review/` snapshots, ignores E501, scopes test-only style rules; safe autofixes | GitHub Actions below |

## Validation (exact commands)

```
pip install -e '.[dev,paper-market,telegram]'
python -m pytest -q                  # 3.13.18: 883 passed, 0 failed
python3.11 -m pytest -q              # 3.11.18: 883 passed, 0 failed
python scripts/scan_secrets.py       # passed (heuristic)
python scripts/audit_lock.py         # 50 packages, 711 hashed artifacts
python scripts/smoke.py              # PAPER_ONLY OK
uvx pip-audit --path <venv site-packages>   # no known vulnerabilities
ruff check . --select F821           # clean
```

GitHub Actions on PR #32 @ `3b27cc4` (public-repo free runners):

| Job | Result |
|-----|--------|
| Isolated Windows dry-run and installation rehearsal | **success** (run 44045072205) |
| windows-latest / Python 3.13 tests | **882 passed, 1 skipped** |
| windows-latest / Python 3.11, ubuntu 3.11 / 3.13 tests | tests step **success** |
| Regression workflow overall | failure at `Verify Phase 7 paper-only invariants` (run 44045072316) |

Known open items, unchanged:
- `scripts/phase7_verify.py` fails identically on the baseline: it asserts the
  real adapter can never write, but later PRs deliberately added a guarded
  live write path. The gate was left as is, not weakened.
- `ruff check .` reports ~860 style findings (mostly `review/` snapshots and
  long lines); not reformatted, to keep this diff reviewable.
- The `phase7_verify` CI gate is an AGENTS.md Phase 7 control ("MCP
  trade/cancel functions remain hard-denied") that the later live-path PRs
  contradict. Per AGENTS.md this conflict is reported, not reinterpreted:
  the owner must either retire that control for the live track or keep live
  paths out of this branch line.
- The Windows *runner* tests and install rehearsal pass; the owner's own host,
  DPAPI enrollment and Task Scheduler supervision remain unverified.

## Broker reality (Robinhood Agentic Trading MCP)

Public Robinhood documentation and secondary reviews (Oct–Nov 2026) indicate:
an official MCP endpoint `https://agent.robinhood.com/mcp/trading`; agents can
place long single-leg option orders, only in the dedicated Agentic account; a
"trade approvals" setting decides whether the agent proposes or places orders,
and per-trade confirmation is not required when approvals are off.

Not verified here (needs the owner's authenticated session): actual
`tools/list` schemas, whether a standalone Python OAuth client can register and
refresh unattended, token lifetime, rate limits, tick/quote fields, and whether
order history exposes `ref_id` for idempotent reconciliation. Until those are
observed, unattended live is **provider-unverified**, not blocked by policy.

## Readiness

**PAPER_READY** (source level). Not SUPERVISED_PILOT_READY until native
Windows acceptance and an authenticated read-only broker session pass.

| Area | Status |
|------|--------|
| Parser / signal lineage | IMPLEMENTED_AND_TESTED |
| Order state machine, exact limit, 3,600 s cancel, 100% exits | IMPLEMENTED_AND_TESTED (synthetic) |
| Risk ceilings and live approval gates | IMPLEMENTED_AND_TESTED |
| SQLite durability and migrations | IMPLEMENTED_AND_TESTED (Linux) |
| Telegram ingestion | IMPLEMENTED_UNVERIFIED (mock MTProto only) |
| Robinhood adapter | IMPLEMENTED_UNVERIFIED (fake MCP only) |
| Dashboard / notifications | IMPLEMENTED_AND_TESTED (local, Linux) |
| Windows deployment | IMPLEMENTED_UNVERIFIED |

## Owner decisions still open

- Repository visibility (currently public).
- Daily loss default is $750; an earlier owner note mentions $655 gross.
- `MAX_CONCURRENT_POSITIONS` is capped 1–10 (default 3); an earlier owner note
  asks for no cap.
- Set `MAX_PREMIUM_PER_SIGNAL_USD` and `MAX_TOTAL_EXPOSURE_USD` in the live
  `deployment.json` (e.g. 999 / 9999) — live now refuses to start without them.
