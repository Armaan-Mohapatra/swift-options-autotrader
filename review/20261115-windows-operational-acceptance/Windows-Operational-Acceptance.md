# Windows Operational Acceptance — 2026-11-15

## Release and installation

- Repository: `Armaan-Mohapatra/swift-options-autotrader`
- Reviewed branch: `audit/mvp-final-offline-20261115`
- Installed source commit: `837aec03e42e8d424fb0435f4b32166e1940e111`
- Versioned source: `E:\SwiftAutoTrader\releases\837aec03e42e8d424fb0435f4b32166e1940e111`
- Installation root: `E:\SwiftAutoTrader\install-837aec03`
- Manifest and configuration remain `PAPER_ONLY`; live and broker writes are disabled.

## Native Windows validation

- Python 3.11 x64; Windows test run on the installed candidate: **628 passed, 1 failed, 1 skipped**.
- The failure is `tests/unit/test_telegram_enrollment.py::test_interactive_enrollment_happy_path`: it expects POSIX permission bits after `chmod`, while Windows reports different mode bits. This leaves the native suite non-clean.
- Test log is local at `E:\SwiftAutoTrader\validation-837aec03\windows-pytest-e-drive.log` and is not included here.
- The installed SQLite database passed `PRAGMA quick_check`.

## Authentication and historical replay

- Standalone local Robinhood OAuth verification completed read-only: schema and tool checks passed; three accessible accounts and one options-approved Agentic account were observed. No account identifiers are included. No brokerage writes or permission changes were made.
- Existing Telegram session/channel access was verified read-only. A bounded sample of 100 recent messages was read in memory; 94 text messages were sent through the historical parser in an isolated simulated database.
- Replay outcomes: 78 `REVIEW_REQUIRED`, 16 `IGNORED`; zero verified instrument catalog rows, quotes, simulated orders, or fills. Evidence quality was unverified and catalog input was not broker-verified, so no executable trade outcome is claimed.
- Historical replay did not call the broker or enable any live execution path.

## Windows service and operational status

- Candidate task `SwiftAutoTrader-Paper-Local` is **Disabled** after failed health checks. It is not running continuously and no candidate process was running at final inspection.
- The service runner starts with the installation root as its working directory. With the installed environment loaded, application configuration fails with `Session path cannot be inside the checkout`; Telegram monitoring therefore cannot start. The dashboard was unavailable after the service failure.
- Supervisor/health logs remain local under `E:\SwiftAutoTrader\install-837aec03\logs\` and state under `E:\SwiftAutoTrader\install-837aec03\state\`; they are not included because they may contain local operational details.
- Older Swift-related scheduled tasks were observed and left unchanged.
- Display sleep is configured Never on AC and battery per `powercfg`; lid-close behavior was not verified due Windows permission restrictions during this run.

## Strategy and production decision

- Reviewed configuration preserves one contract per entry, LOTTO eligibility, 10% entry slippage tolerance, full exits on partial-sale alerts for matching Swift-owned positions, no averaging down, $750 net realized daily loss default, and optional mobile kill-switch disabled.
- $10,000 aggregate exposure is a design ceiling subject to buying power, not an activated limit.
- The candidate reports `live_dispatch_registered=false` and `live_startup_ready=false`. Standalone production execution integration and supervised standalone brokerage acceptance remain outstanding. Existing ChatGPT/MCP brokerage evidence does not certify this application for live use.
- **Decision: NO-GO for continuous monitoring and live execution on this release.** Keep paper/read-only and broker writes disabled until the Windows session-path issue, clean native acceptance, production dispatcher integration, and supervised standalone brokerage acceptance are resolved.

## Final status

- Installed: **Yes**
- Running continuously: **No**
- Current mode: **PAPER_ONLY**, currently stopped
- Authorized for real-money execution: **No**
- Production live entry point: **Missing/unregistered**
