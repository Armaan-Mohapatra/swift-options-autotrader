# Swift AutoTrader — Windows installation guide (Phase 6)

**Intended reader:** Someone using Codex on their own Windows 10/11 computer, without advanced programming experience.

**Current status:** The Windows package has been developed in GitHub. **DO NOT install on the user's computer during Phase 6.** The procedures below are for a later separately authorized Codex session, after reviewing the Phase 6 PR and resolving required CI/release gates. There is **no cloud server** and no subscription infrastructure. The existing always-on Windows computer is the proposed host.

**Trading safety:** The installed software is PAPER ONLY. `PAPER_ONLY=true`, `LIVE_ENABLED=false`, and `ALLOW_BROKER_WRITES=false` are enforced. The auto-start service runs the **read-only dashboard** and, only after explicit local authorization, the **read-only Telegram listener**. It does **not** automatically place even paper orders from new Swift messages: exact option-chain verification, live quote evidence and preflight integration are not yet field-validated. The tested historical replay remains a separate opt-in research command. Never treat a dashboard appearing as proof of end-to-end trading readiness.

## Before installing — quick checklist

1. **Wait for review.** Draft PRs #1, #10–#14 are stacked, and Phase 6 is another draft PR. Check actual GitHub Actions Windows/Linux results and any open blocker before treating a ZIP as release-ready. Phase 5 has an unresolved pre-runner CI failure.
2. Use the existing, personally authorized Windows computer. No VPS, cloud VM, Docker, Phoenix/Kalshi directories, or shared trading database. The default local destination is `%LOCALAPPDATA%\SwiftAutoTraderPaper`.
3. Obtain Python **3.11 x64** and the `py.exe` Python launcher from the official Python installer, plus Git for Windows. Use a normal Windows user account; you do not need to provide an administrator password for the **per-user logon** task (Task Scheduler availability and permissions must be validated).
4. Have internet access for a **one-time pinned dependency installation**. The app itself is free to run locally. GitHub Actions may require repository usage/billing settings to be fixed before tests run.
5. For later Telegram listening only, prepare your legitimately authorized user account's Telegram API ID, API hash and an existing authorized Telethon `.session` file. **Never paste passwords, session text, tokens or paid Swift messages into GitHub, ChatGPT or CI.**
6. Use Codex to clone this private GitHub repository to a disposable, dedicated **source** directory. Checkout the exact **full, reviewed Phase 6 commit SHA**; do not use floating `main` or an unreviewed PR. Do not make local source edits before installation. Verify that the branch contains `uv.lock` and `deployment/windows/setup.ps1`.

## Safe walkthrough (once later authorized)

Open **Windows PowerShell** as your own normal user and use the cloned source checkout. These examples are illustrative; your Codex session must substitute the exact reviewed SHA, not the placeholder.

### Step 1 — preview everything without changes

```powershell
cd C:\Users\YOUR_USER\source\swift-options-autotrader
.\deployment\windows\setup.ps1 -DryRun
```

The preview lists the isolated folders, pinned dependencies, task schedule and guardrails. It **creates no directories, virtual environments, tasks, credentials or databases**. If the preview mentions Phoenix/Kalshi paths, stop.

### Step 2 — check environment and perform reviewed install

```powershell
py -3.11 --version
git status --short
git rev-parse HEAD
# Replace the placeholder only after Codex verifies it matches reviewed GitHub source:
.\deployment\windows\setup.ps1 -Apply -ExpectedCommit "REPLACE_WITH_VERIFIED_40_CHARACTER_COMMIT" -Confirm:$false
```

`-Apply` is an explicit file/system change; this step must **not** run during Phase 6 development. The script compares the full SHA to the clean checkout, rejects broad/overlapping paths, and copies only approved code files. It creates separate `releases`, `data`, `logs`, `backups`, `state`, `secrets`, `config`, and `tools` folders. The user and SYSTEM receive private NTFS ACLs.

It installs the **version-pinned `uv==0.10.0`** bootstrap into a dedicated local virtual environment, then runs `uv sync --locked --no-dev --extra telegram --no-editable --python 3.11` from the committed universal lock. The program's Python dependencies are locked; **the uv bootstrap wheel itself is version pinned but not yet cryptographically hash-pinned**, and the optional Robinhood SDK is not included. Source/signing and fully hermetic install validation remain release gates.

The installer sets safe, zero-dollar defaults, applies forward-only SQLite migrations, checks the app, and registers a limited-permission **per-user at-logon task** (`SwiftAutoTrader-Paper-Local`) with bounded restart-on-failure. It will run **after that user logs in**, not reliably before interactive login after a reboot. It does not require a cloud host and does not touch other trading tasks.

For an isolated installation rehearsal without Task Scheduler registration, supply `-NoSchedule` and a different, brand-new absolute `-InstallRoot` path. This is how GitHub runner tests are designed.

### Step 3 — inspect status without trading

```powershell
.\deployment\windows\manage.ps1 -Action Status
.\deployment\windows\manage.ps1 -Action Health
```

When the task starts, visit **http://127.0.0.1:8765/** on the **same computer**. The dashboard is read-only and refuses non-loopback binding. If the dashboard shows missing quotes or uncertain P&L, that is expected; the program does not invent market data. Never expose port 8765 through a router, reverse proxy or firewall exception.

### Step 4 — optional authorized Telegram receive-only enrollment

This must be a **separate user-authorized local step**, never GitHub CI. Before enabling, inspect the purchased Swift channel and legitimate user access; do not bypass paid access. Provide the authorized `.session` by a local path and the API hash through a secure Windows prompt:

```powershell
.\deployment\windows\secrets.ps1 -Action ImportTelegramSession -SessionFile "C:\SECURE\ALREADY_AUTHORIZED.session"
.\deployment\windows\secrets.ps1 -Action TelegramApiHash
```

Next edit `%LOCALAPPDATA%\SwiftAutoTraderPaper\config\settings.json` in Notepad. Fill `telegram_channel_id` with the precise signed source-channel ID, set `telegram_api_id` and set `telegram_enabled` to `true`. Never insert the hash or token in this JSON file. The hash is protected by **user-scoped Windows DPAPI** and the session file has restricted NTFS ACLs. The same Windows identity must run Task Scheduler, or DPAPI decryption can fail. Validate the session, message formats, reconnects and channel permission on the real machine **before** turning on unattended listening. The listener never posts back to Swift.

### Step 5 — optional metadata-only status bot

This is **off by default** and is separate from the Swift source account. With a legitimately created Telegram bot and a private destination chat, run `secrets.ps1 -Action StatusBotToken`, then set `status_notifications_enabled=true` and `status_chat_id` in private `config\settings.json`. The status chat **must not** be the Swift source channel. Sent messages contain only symbolic error codes such as `BACKUP_FAILED`; no paid Swift text or brokerage details. Delivery is at-least-once, and a failure can cause duplicates. Do not enable without reviewing the target.

### Step 6 — safe daily operations

```powershell
.\deployment\windows\manage.ps1 -Action Start -Confirm:$false
.\deployment\windows\manage.ps1 -Action Status
.\deployment\windows\manage.ps1 -Action Logs
.\deployment\windows\manage.ps1 -Action Backup -Confirm:$false
.\deployment\windows\manage.ps1 -Action Stop -Confirm:$false
.\deployment\windows\manage.ps1 -Action Restart -Confirm:$false
```

The supervisor also takes a consistent SQLite backup at startup and about every 24 hours while running, retaining seven backups. Startup and status scans use a conservative five-second loop and one-minute notification scan; no continuous quote-fetching job is enabled. Logs rotate at 2 MiB × four files for the supervisor, excluding other application/library logs. **Windows CPU/RAM and total disk consumption have not been measured on the intended computer and must be benchmarked there**; no numerical memory/CPU savings are guaranteed.

### Step 7 — recovery, rollback, uninstall

Refer to [OPERATIONS.md](OPERATIONS.md) for error states and [ROLLBACK.md](ROLLBACK.md) before restoring data. `-Action Uninstall` first unregisters only the Swift task and **preserves** data, credentials, backups and logs. Data deletion is disabled unless both `-PurgeData` and `-IUnderstandDataLoss` are explicitly supplied after manual review and confirmation.

## What's still pending after GitHub development

- GitHub owner must resolve Actions runner failures, rerun full previous-phase regressions and Windows install smoke, and approve all stacked PRs. **Do not tag Phase 6 release-ready until those checks pass.**
- Verify Windows PowerShell 5.1 script syntax and lifecycle on an isolated Windows runner (the Linux-only static checks are not sufficient).
- Have Codex inspect the intended host's Python version, PowerShell, free disk, available memory, account identity, scheduled-task permissions, firewall status, local-only dashboard and other active trading software **without changing unrelated processes**.
- Clone and verify a full GitHub SHA, run dry-run, obtain approval to install, then complete local-only authorization for Telegram (and later **read-only** MCP OAuth if separately approved).
- Validate restart after logoff/reboot, database/WAL recovery, optional status notifications, disk/CPU/RAM, dashboard and service errors. No actual broker write is allowed.
- Supply genuinely verified option chains, time-aligned quotes, and a separately reviewed paper-forward adapter before enabling unattended paper decisions. Phase 7 audit and a separate owner approval are still required; **live trading is not enabled by this package**.


## Phase 7 hold — do not install before acceptance gates

Phase 7 did **not** produce a CI-certified GitHub paper-only release tag. The source branch and draft PR are available only for review. Before asking Codex to install, the repository owner must resolve GitHub Actions runner-allocation issue #15, obtain all mandatory passing Windows/Linux tests and Windows isolated PowerShell rehearsal on the exact reviewed SHA, verify dependency artifact provenance and sign off on the package. No cloud hosting is needed.

After separate approval, Codex should: (1) inventory the real Windows host, existing Phoenix/Kalshi processes/tasks/paths and local resources without stopping anything; (2) choose a new isolated install root with data/backups on a supported local drive; (3) clone the reviewed SHA; (4) run `powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\deployment\windows\setup.ps1 -DryRun` and review the no-change preview; (5) only with explicit owner permission run `setup.ps1 -Apply -ExpectedCommit <the-full-reviewed-40-character-SHA>`; (6) confirm PAPER mode, localhost dashboard, service at-logon, backups, restore, rollback, single instance, stop/restart and resource use; (7) separately enroll an authorized read-only Telegram session and optional private status bot if approved. The installer is not an unattended paper trading signal executor. See `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/ROLLBACK.md` and `docs/PHASE_REPORTS/PHASE_7.md`.


## Phase 8 addendum — optional PAPER dispatcher is NOT installation-authorized

The new source includes `paper-dispatch-service` and Windows settings `paper_dispatch_enabled=false`, `paper_data_source=none` by default, with paper risk budgets zero. Enabling requires a legitimately authorized Telegram session, **separately** manually approved Robinhood read-only OAuth tokens stored under the same Windows user's DPAPI-managed `secrets/oauth.dpapi`, explicit strictly positive paper-only risk budgets, and independent actual option quote/tick/depth/session validation. The interactive OAuth callback uses `127.0.0.1:8766`; dashboard stays on `127.0.0.1:8765`. No personal account was accessed in GitHub development. Current official response-field compatibility and transitive SDK lock are unknown, therefore **do not enable the worker or install before release certification**. See `docs/PAPER_DISPATCH_PHASE8.md`.
