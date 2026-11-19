# Pre-Codex checklist — Swift AutoTrader PAPER only

**DO NOT INSTALL YET.** GitHub Actions spending-limit failure, full transitive SDK lock, native Windows checks and official real read-only MCP field validation are outstanding. This is a configuration-only handoff for the time **after a certified release exists**. A source branch or local synthetic test is not a release certificate.

## Development statuses

| Subsystem | Code written | Local tests | Release certified | Local authorization | Forward paper tests |
| --- | --- | --- | --- | --- | --- |
| Telegram read-only + source SQLite | Yes | Synthetic | NO | REQUIRED | REQUIRED |
| Swift parser and full exit matching | Yes | Synthetic | NO | Source formats | REQUIRED |
| Exact-price PAPER broker/engine | Yes | Synthetic | NO | Paper risk limits | REQUIRED |
| Robinhood MCP read-only interface | Scaffold + conservative adapter | Mock | NO | REQUIRED | REQUIRED |
| Continuous PAPER dispatcher | Yes (opt-in) | Mock | NO | Auth + verified quote fields | REQUIRED |
| Dashboard, analytics, notices | Yes | Synthetic | NO | No by default | REQUIRED |
| Windows install/supervisor/rollback | Source authored | Portable tests | NO | Windows identity + DPAPI | REQUIRED |
| Real Robinhood order execution | Permanently disabled | Deny-path tests | NOT AUTHORIZED | N/A | N/A |

## Mandatory release engineering before Codex may install

1. Restore GitHub Actions runner availability; verify one runner actually starts, then pass the **complete exact-commit** Windows/Linux CPython 3.11/3.13 pytest, Ruff, compile, lock audit, security audit and safe-invariant checks. Review genuine code failures rather than reducing assertions.
2. Resolve/publish a platform-specific **complete, hashed transitive lock** for the official MCP SDK v2.3.0, `httpx2`, `pywin32`, `exchange_calendars` and its deps. Audit licenses, advisories and build provenance. Confirm SDK actual import and OAuth callback interfaces on disposable Windows runners.
3. Run a disposable Windows PowerShell installer rehearsal: no-change dry-run, exact-commit release ZIP checksum verification, installation without scheduling, smoke, stop/start, backups, offline restore, rollback and uninstall. Inspect ACLs, task logon behavior, launch parameters, code/package hash and resource limits.
4. Audit the exact staged PR diff and protected branches; obtain owner approval for merging the stacked PRs and publishing one **PAPER-only**, commit-bound signed/reviewed release ZIP. Verify the published SHA-256 and tag independently.
5. Validate whether official Robinhood's read-only endpoint actually yields **bid/ask depth, tick, multiplier, session, timestamp and approval evidence** needed for unattended paper fills. If not supported, source work may need an explicitly reviewed alternative official read-only feed. Do not substitute an undocumented or fabricated broker API.

## Configuration requirements after certification

- Windows x64, official Python 3.11 and approved PowerShell 5.1 environment, Git optional for exact ZIP mode, internet connectivity, local user account to run Task Scheduler at logon.
- A new dedicated install root, conventionally `%LOCALAPPDATA%\\SwiftAutoTraderPaper`, away from Phoenix/Kalshi paths, with free disk capacity for bounded SQLite, logs, 7 backups and dependency environments. Check CPU/RAM/storage before and after starting.
- Verified approved release ZIP, independent SHA-256 digest, full source commit SHA, and trusted reviewed installer script. No floating branch or local developer working tree.
- At installation: mode `PAPER_ONLY`; all live/broker writes off; no Telegram or paper-dispatch enabled by default. Optional status bot off.
- Separately, on the **same Windows user account**, authorize the legitimate Telegram source session and signed peer ID; enroll its API ID/hash and session via private local prompts/ACL. No paid source data in GitHub or logs.
- Separately enroll official read-only Robinhood Trading MCP OAuth credentials in managed `secrets/oauth.dpapi`, and validate token refresh, permission and field contracts without any order submission. Dashboard uses `127.0.0.1:8765`; interactive OAuth callback is `127.0.0.1:8766`.
- Explicitly set paper-only risk ceilings in private `config/settings.json`: paper contract count, per-signal premium budget, total exposure and max simultaneous signals. Leave disabled without reviewed field evidence. The service requires both `paper_dispatch_enabled=true` and `paper_data_source=robinhood_mcp_readonly` plus successful independent auth checks to attempt PAPER simulation.
- Confirm scheduler single instance and at-logon behavior; host may remain on but service will not run before that user logs in after a cold restart. Check network availability and secure DPAPI account identity.
- Observe and reconcile first forward PAPER cycles: quote freshness, no real order writes, proper entry/exit/cancel, no historical rebuys, correct P&L evidence, pending exit and reconnect notifications; monitor CPU, RAM, I/O and backup growth.

## Exact post-certification Codex installation-only instruction

> Work ONLY on installation and private local configuration of the already **approved and certified** PAPER-only Swift AutoTrader GitHub release. First verify its full source SHA, independently published artifact SHA-256, all passing Windows/Linux release CI and native Windows installer test evidence. If any gate is missing, STOP without installing. Inventory existing Phoenix/Kalshi processes, services, directories, tasks and disk/RAM usage; do not modify, stop or read their secrets. Use the release's reviewed `setup.ps1 -DryRun` first and present its proposed changes for explicit approval. Then, only after approval, install the source-commit-bound verified ZIP using `-Apply -ExpectedCommit <approved full SHA> -PackageZip <approved ZIP path> -PackageSha256 <approved digest>`. Run initial PAPER-only smoke/health/backup/restart checks with Telegram and the PAPER dispatcher both disabled. Never enable LIVE_ENABLED or broker writes, never place actual orders. After further explicit approval, enroll Telegram and read-only Robinhood OAuth using user-local protected credentials and verify market-data evidence. Enable automatic PAPER simulation only after positive paper-risk ceilings, exact quote/contract/session evidence and forward tests pass. Validate stop/backup/restore/rollback and resource budgets, log only metadata, and deliver measured evidence. Do not add application code, use unofficial brokerage interfaces, or touch unrelated systems. If the verified release or official data fields are not supported, STOP and report that release engineering requires further GitHub work.

No installation is authorized by this document.
