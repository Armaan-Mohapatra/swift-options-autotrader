# Swift AutoTrader: cross-lineage safety and acceptance plan

Date: 2026-11-18. Engineering handoff only; not live authorization.

## Verified GitHub lineage

- Windows rollback/installed baseline: 86828420f2544f942b64d723910fa194af58f8d9.
- PR #32 safety reconciliation: b9c505e771ed60f69b15d9177714314553b74d3a. Compared with installed baseline: 10 commits ahead, zero behind.
- PR #34 live-broker integration: 40ecf6fe882118bb0384f706671ab0189696c430. Compared with PR #32: 27 candidate-only commits, 107 protected-lineage-only commits.
- PR #34's Linux/Windows 3.11/3.13 regression and isolated Windows install workflows succeeded on the last tested SHA. The actual Windows installation was rolled back.
- GitHub reports repository PUBLIC. A tracked-file-name scan found no obvious stored credential/session artifacts; this does NOT prove history/content was free of secrets. Run local history scanning without printing secrets.

Never overwrite the 107 protected-lineage-only commits by copying PR #34's source tree. PR #32 is the safer integration baseline; it carries the installed lineage plus reconciliation improvements. Do not auto-resolve auth, broker, config, dispatcher or journal conflicts using an indiscriminate "theirs" strategy.

## Critical incompatibilities

1. PR #32 has RobinhoodLiveBroker in broker/robinhood_mcp.py, live_service.py, live_dispatch.py, dedicated unattended-approval receipt, acceptance_scope.py, exit interlocks and credential hardening.
2. PR #34 has a separate LiveBroker and live_runtime.py, durable order journal, shared OAuth session, realized-plus-unrealized P&L attribution, kill switch, read-only rehearsal and Windows packaging fixes.
3. PR #32 enforces an arbitrary aggregate exposure cap and calculates realized-only daily loss in its dispatcher. Owner contract is no fixed trade-count or aggregate cap other than buying power; one contract per original ENTRY, max $999 premium, max $750 NY-day realized+unrealized loss latch.
4. PR #32 disallows positive SLIPPAGE_TOLERANCE_PCT to avoid price chasing. Desired 10% quote tolerance is a preflight check only; every order must retain exact alert limit.
5. Preserve supervised entry authorization, dedicated Robinhood agentic account, pre-write pause, no blind retries, journal-first idempotency, and refusal to sell manually held/unverified inventory.

## Read-only gate

Run locally:
python scripts/release_lineage_guard.py --repo . --protected-ref 86828420f2544f942b64d723910fa194af58f8d9 --candidate-ref HEAD

This guard blocks missing protected commits/modules. Its output PASS_LINEAGE_ONLY does not certify broker behavior, tests, secrets, permission or production readiness. Synthetic tests are in tests/unit/test_release_lineage_guard.py.

## Engineering sequence for Claude Code

1. Inspect PRs #32 and #34 and preserve complete protected ancestry. Build a consolidated candidate on top of PR #32, not the earlier PR #24 baseline. One canonical live broker and single order journal; eliminate competing paths.
2. Port only genuinely missing PR #34 protections with conflict-aware patches: separate 10% tolerance (no price chase), $750 daily net P&L latch, account pinning, shared OAuth, idempotent unknown-outcome reconciliation, read-only rehearsal, kill switch, true cost-basis/overnight safeguards, accurate dashboard and Windows PowerShell 5.1/Telegram service fixes.
3. Add negative-path tests for duplicate/replayed entry, partial/late fill, disconnect/unknown order, stale source/quote, same-day vs overnight cost basis, account mismatch, missing history, manual mixed positions, parser exit reply, expiry, insufficient buying power, kill-switch outage and daily P&L boundaries. Do not bypass failed gates.
4. Run Python 3.11 and 3.13 suites, Ruff, compileall, dependency and historical secret audits, SHA-bound ZIP validation and Windows native PowerShell 5.1 dry run. Prefer local tests and do not incur paid GitHub Actions. Mark inaccessible checks BLOCKED.
5. Confirm exact Windows install root; preserve only Swift data with verified SQLite backups and hashes. Install candidate PAPER-only. Do not touch Phoenix, unrelated tasks, credentials or old recovery assets.
6. Read-only field-check Robinhood agentic-account pin, actual MCP tool schemas/pagination, options orders/positions/quotes, realized P&L, token refresh and original Telegram event freshness. Never upload account identifiers, paid source bodies, tokens, private database or DPAPI files.
7. Deliver evidence by SHA and a strict GO/NO-GO. Fix real bugs and repeat all affected gates until results are demonstrable. Do not describe synthetic CI as production certification.

## Live activation boundary

Engineering and GitHub changes do not authorize placing real orders. After all read-only gates pass, get a separate owner signoff bound to the installed SHA and dedicated account. Begin entries PAUSED; conduct at most ONE separately approved supervised live test, verify broker order ID, fills, 60-minute expiry, account P&L, full owned-position exit and kill switch, then pause.

After another explicit approval for unattended mode and successful uptime/restart and failure-drill acceptance, allow automatic entries with NO arbitrary trade-count cap while preserving one contract, $999 entry limit, $750 loss latch, buying power, exact alert price, quote tolerance, daily risk reconciliation, account pinning, kill switch, safe exits and idempotency. Unrestricted does not mean bypassing risk limits or broker permissions.

Final report must include: candidate SHA, full audit lineage, changed files, tests by platform, Windows root/backups, masked account proof, read-only MCP schema and rehearsal evidence, monitored worker/restart results, supervised results only if separately authorized, and explicit GO/NO-GO.
