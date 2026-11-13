# Phase 8 — Unattended PAPER dispatcher and pre-Codex source preparation

**Status: CODE_IMPLEMENTED / SYNTHETIC_LOCAL_TESTED / GITHUB_CI_BLOCKED / MCP_FIELD_UNVERIFIED / DEPENDENCY_LOCK_BLOCKED / WINDOWS_UNVERIFIED / NO_RELEASE.**

## Why this phase was needed

Phase 7 had an independently tested Telegram message store, deterministic parser, risk/option order engine, synthetic broker and ledger, plus a Windows supervisor for dashboard/Telegram/notifications. However, the supervisor did not connect fresh messages to quote-verified PAPER execution; `app.py` deliberately rejected `paper-cycle` without an independent instrument catalog. Issue [#18](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/18) captured this missing integration.

## What was developed without changing original strategy

- New opt-in `paper_dispatch.py` worker connecting durable, eligible stored Telegram events → `SignalMatcher` → independent verified instrument preflight → unchanged exact-priced `PaperOrderEngine` → `PaperBroker` quote-based fills → `PaperAnalytics` and notices. Provider outage preserves pending source events and runs only the order watchdog.
- `mcp_paper_data.py` official read-only adapter requiring exact unique contract/year, independently verified 100 multiplier/tick, bid/ask freshness and displayed depth, exchange-session calendar, external account/buying power and nonblocking read-only broker review. Missing data or approval halts entries, never chases or invents fills. Real Robinhood may not expose the required data; **field-readiness cannot be claimed.**
- `paper_service.py` opt-in Windows worker using stored user-scoped DPAPI OAuth tokens with noninteractive callbacks that cannot enroll or open a browser. Interactive local enrollment now shares its managed token path and a callback port distinct from the dashboard.
- Existing supervisor/PowerShell configuration gains a disabled-by-default separately restarted PAPER worker; all real broker writes remain hard-disabled, with no real brokerage order adapter admitted into `PaperOrderEngine`.
- New offline release ZIP verifier and `setup.ps1` optional exact SHA + ZIP checksum mode, with no-change preview preserved. Windows run is **unverified**.
- New unit/integration tests and documentation, without modifying migration SQL or strategy semantics.

## Evidence and external blockers

- An independent reconstructed Python 3.13 local workspace can run the paper/mocked-MCP tests and previous selected suites. The original repository's full clean source on native Windows/Linux has **not** been run since Phase 4, and the reconstructed workspace contains non-published local Phase 1 stand-ins. Local evidence must remain labeled as such.
- GitHub issue [#15](https://github.com/Armaan-Mohapatra/swift-options-autotrader/issues/15): runner jobs consistently fail before allocation owing to an unresolved spending/payment restriction. No full Windows 3.11/3.13 or Linux 3.11/3.13 final CI results; do not claim they passed. Do not repeatedly rerun blocked jobs.
- The optional SDK stack (`mcp==2.3.0`, Windows DPAPI `pywin32`, and `exchange-calendars`) cannot be downloaded/locked or runtime-tested from this DNS-restricted development environment. The Phase 7 universal lock contains only existing Telegram/dev deps. This remains a release blocker, not Codex's task to silently invent missing code.
- No Windows PowerShell 5.1 interpreter on this development host. Native startup, Task Scheduler, DPAPI, upgrade, rollback and no-secret ZIP installation remain unverified.
- Robinhood's actual MCP option quotes, depth, tick/multiplier and account response schemas have not been authenticated or checked. Official tool availability in Robinhood's published help center is **not** proof of the necessary instrument fields or hands-free capability.
- No GitHub PR merge, tag, release, user-machine install, personal credential access or real broker order has occurred.

## Release gate

**NO-GO** until all required CI jobs actually allocate runners and succeed; the optional full dependency lock/vulnerability/license inventory and ZIP checks are verified; official read-only MCP schema/quote data are validated independently; Windows installation and recovery rehearsals succeed; and the owner separately approves merging/publishing. A release artifact built locally for tests is never a certified release.

Refer to `docs/PAPER_DISPATCH_PHASE8.md` and `docs/PRE_CODEX_HANDOFF.md`.
