# Live operations: rehearsal, kill switch, deploy and OAuth enrollment

Live trading goes through `LiveBroker` and only when **every** live setting is present
(`PAPER_ONLY=false`, `LIVE_ENABLED=true`, `ALLOW_BROKER_WRITES=true`,
`BROKER_BACKEND=robinhood_live`, `LIVE_ACCOUNT_NUMBER`, `LIVE_JOURNAL_PATH`). Anything less
runs as paper and says what is missing. Nothing in this document turns live on by itself.
Never paste an account number, token or code into chat, Git or a log.

## Kill switch: pause entries and check it

One line each (Windows install, from the source checkout):

```powershell
.\deployment\windows\manage.ps1 -Action PauseEntries      # entries BLOCKED now
.\deployment\windows\manage.ps1 -Action EntriesStatus     # ALLOWED or BLOCKED, reason, time
.\deployment\windows\manage.ps1 -Action ResumeEntries     # asks for confirmation
```

Or directly: `python -m swift_autotrader entries-pause | entries-status | entries-resume`
(uses `DB_PATH`). These entry controls show `CONTROL_ONLY` and intentionally
avoid loading potentially broken live settings or OAuth credentials. Check
`manage.ps1 -Action Status` separately for the configured service mode.

- It sets `paused=1` in `paper_engine_control`, the same row the engine and `LiveBroker`
  read before **every** entry. It takes effect on the next entry; no restart is needed.
- Exits and cancels of orders you already own are **never** blocked by it.
- `entries-resume` refuses while an emergency stop is active; reset that manually.
- Unreadable control data counts as blocked.

### Mobile kill switch (optional; owner-enrolled)

Create a private Telegram bot yourself, then run `.\deployment\windows\secrets.ps1 -Action KillSwitchBotToken`
(secure prompt; stored with user DPAPI as `secrets\kill_switch_bot_token.dpapi`) and add
`"kill_switch_chat_id": <your private chat id>` to the LIVE settings. The LIVE launcher then enables
`kill-switch-listener` (`/status /pause /resume /cancel /stop /flatten`; `/flatten` needs a confirmation
token; only that chat is allowed). The chat may not be the Swift source channel. Without both pieces the
listener does not run and the local `PauseEntries` command remains the kill switch.

## Live rehearsal (real MCP, writes forced off)

Rehearsal connects with the already-enrolled token, and for each recorded ENTRY signal
runs the entry preflight: account check, buying power, daily P/L, quote and 10% ask check,
`review_option_order` and the write-tool schema check. It logs what would have been sent.
It never calls `place_option_order` or `cancel_option_order`, and it writes nothing to the
journal (not even a loss latch).

```powershell
.\deployment\windows\manage.ps1 -Action Rehearse
```

Each result is `WOULD_SEND` (with the exact `place_option_order` arguments, account masked
to its last four digits) or `DENIED` (with the reason). One JSON line per result goes to
`<install>\logs\live_rehearsal.jsonl`. Three independent guards stop a write: the client
wrapper only allows known read tools, the broker is built with `rehearsal=True`, and
`_call_write` refuses. Rehearsal first requires a real original ENTRY parser decision, an eligible Telegram
source revision and a source-sent timestamp no more than 120 seconds old. Stale
historical alerts are `DENIED: STALE_ENTRY_NO_REPLAY` and are never represented as
live-ready opportunities. Missing source lineage is also denied. To assess the
actual end-to-end path, run rehearsal against a genuinely fresh authorized alert;
this remains broker-write-disabled.

A denial that mentions a position, realized P/L or order field may signal a
response-shape mismatch to fix before going live. It may also be a legitimate
insufficient-buying-power, loss-limit, kill-switch or market-state denial.

## Deploy this branch to the installed Swift copy (stays PAPER)

The previous handoff identifies a possible existing install at
`E:\SwiftAutoTraderCertification\read-only-pilot\installed`; verify this on the
actual Windows host before changing anything. The installer default is
`%LOCALAPPDATA%\SwiftAutoTraderPaper` only if no explicit `-InstallRoot` is supplied.
Run as your normal user and use the **same verified root** for every management,
install, OAuth, backup, rehearsal and rollback action.

1. Get the exact code in a source folder that is separate from the install:
   `git fetch origin; git checkout feat/live-broker-integration-20261117; git status --short`
   (must print nothing), then `$sha = git rev-parse HEAD` and confirm it is the PR head.
2. If you run tests or CLI commands by hand, remove any `PAPER_ONLY`, `PAPER_TRADING` and
   `PAPER_TRADING_ONLY` user environment variables that hold values the app rejects.
   (The service itself overrides them.)
3. Stop the service: `.\deployment\windows\manage.ps1 -Action Stop -Confirm:$false`
4. Back up: `.\deployment\windows\manage.ps1 -Action Backup -Confirm:$false`.
   The current backup command snapshots both the main SQLite control database
   and, if configured, the separate live order journal. Verify both filenames and
   SHA-256 metadata under `<install>\backups`; no backup leaves this machine.
5. Preview: `.\deployment\windows\setup.ps1 -DryRun` (no changes; stop if it names
   Phoenix or Kalshi paths).
6. Install the release over the existing install:
   `.\deployment\windows\setup.ps1 -Apply -ExpectedCommit $sha -NoSchedule -Confirm:$false`
   **Use `-NoSchedule` for an upgrade.** Without it the installer activates the new release
   and then fails because the `SwiftAutoTrader-Paper-Local` task already exists. The existing
   task keeps working: its scripts are refreshed and it launches the active release. The
   installer adds a new release, keeps `data`, `secrets`, `config` and `deployment.json`, and
   saves the old release for `-Action Rollback`. It needs network access for the locked install.
7. Confirm it is still paper: `.\deployment\windows\manage.ps1 -Action Status`
   reports `configured_mode: PAPER_ONLY`; `-Action EntriesStatus` works.
8. Start it again: `.\deployment\windows\manage.ps1 -Action Start -Confirm:$false`
   To undo: `.\deployment\windows\manage.ps1 -Action Rollback`.

## Enroll the Robinhood OAuth for the MCP account

You do the browser sign-in and approval yourself; nothing here handles a password.

1. In Robinhood, confirm the agent (MCP) account exists, is option-approved (level 2 or
   higher) and allows agent access. Only that account should be authorized.
2. Set up the session:
   ```powershell
   $root = "$env:LOCALAPPDATA\SwiftAutoTraderPaper"
   . .\deployment\windows\common.ps1
   $py = Get-SwiftPython $root
   $env:SWIFT_INSTALL_ROOT = $root
   $env:ROBINHOOD_MCP_INTERACTIVE = 'true'
   ```
3. Run `& $py -m swift_autotrader mcp-diagnostics --connect`. It opens your browser at
   Robinhood and listens on `http://127.0.0.1:8766/callback`. Sign in and approve there.
   It prints counts only. Expect `READ_ONLY_SCHEMA_OK` and one option-approved agentic
   account. The token is stored encrypted for your Windows user in
   `<install>\secrets\oauth.dpapi`, so the scheduled task must run as the same user.
4. `Remove-Item Env:ROBINHOOD_MCP_INTERACTIVE`
5. In Notepad, edit `<install>\config\settings.json`: add `"live_journal_path"`
   (for example `C:\\Users\\YOU\\AppData\\Local\\SwiftAutoTraderPaper\\data\\live\\journal.sqlite`).
   The LIVE loader reads the account from the key `"robinhood_mcp_account"` (the full MCP
   account number; it must equal the DPAPI-pinned account or the service refuses to start).
   Keep that key out of the paper `settings.json`; stage it in a separate LIVE candidate file
   (see "Going live"). Leave `"mode": "PAPER_ONLY"` and `"live_enabled": false` for now.
6. Create the empty journal once: `.\deployment\windows\manage.ps1 -Action InitLiveJournal`
7. Before running an unattended service, confirm that the Telegram source is
   enrolled and `telegram_enabled=true`, `paper_dispatch_enabled=true`, and
   `paper_data_source=robinhood_mcp_readonly` are configured. Explicitly set the
   premium ceiling to a positive value no higher than $999; the underlying
   `MAX_PREMIUM_PER_SIGNAL_USD` default is $0 and intentionally blocks every buy.
   The Windows runtime fails to start LIVE when the dispatcher is disabled.
   Do not copy credentials or full account numbers into logs or Git.
8. Rehearse as above and read `logs\live_rehearsal.jsonl` until every `WOULD_SEND` and every
   `DENIED` makes sense.

## Acceptance-timed cancellation and continuous risk polling

The live broker now tolerates Robinhood orders that omit `executions` when
`processed_quantity=0`; a positive fill quantity without executions still
fails closed. Its timeout helper checks the reconciled broker acceptance time,
not only the earlier local reservation time. The live dispatcher polls realized
and unrealized P/L on each cycle, even with no new signals, and blocks new
entries whenever the risk snapshot cannot be trusted. Exits and cancellations
remain available. All these behaviors require local integration validation.

## Live dashboard evidence boundary

The local dashboard never claims to show authoritative Robinhood P/L. When
`LIVE` is configured, it labels the mode as `LIVE CONFIGURED · BROKER P/L
UNVERIFIED` and suppresses simulated P/L cards. The local journal and source
metadata can help troubleshooting, but they do not replace read-only broker
positions, confirmed executions, buying power, or the daily-loss guard.

## Backup and recovery boundary

`manage.ps1 -Action Backup` and the supervised daily backup use SQLite's
online backup API for both the control database and a configured live journal.
They create separate timestamped, hashed files named `swift-*.sqlite3` and
`swift-live-*.sqlite3`, respectively, under the managed local backup folder.
The journal can contain sensitive account/order details: do not upload it
to GitHub, chat, cloud storage, or share it with agents.

Restoring the control database **never** restores the live order journal.
A live-journal snapshot cannot be passed to the control restore command.
A restore of the live journal requires an offline coordinated recovery and
broker reconciliation, not a blind file swap: later broker writes or fills
might have happened after the snapshot. If the journal is lost, keep live
entries disabled and stop for manual reconciliation.

## Going live (only on your explicit go)

Do these yourself; no tool or agent should set them. First run `-Action PauseEntries` so the
service starts blocked.

1. In `config\settings.json` set `"mode": "LIVE"`, `"live_enabled": true`,
   `"broker_writes_enabled": true`, with `robinhood_mcp_account`, `live_journal_path`,
   `max_premium_per_signal_usd` (<= 999), `max_daily_loss_usd` (<= 750) and
   `max_contracts_per_signal` (1) present. The LIVE loader then forces
   `DB_PATH=<install>\data\live.sqlite3`, a control database separate from the paper one.
2. In `deployment.json` set `"mode": "LIVE"`, `"live_enabled": true`,
   `"broker_writes_enabled": true`. The supervisor refuses to start if the two files
   disagree about live mode.
3. Write the owner's expiring unrestricted-execution receipt
   `<install>\secrets\unattended-live-approval.json` (version 2: `account_sha256` of the pinned
   account, `issued_at`, `expires_at` at most 7 days later, and `max_contracts_per_signal`,
   `max_premium_per_signal_usd`, `max_daily_loss_usd` caps that the configured values may not
   exceed). Without a valid receipt `live-dispatch` refuses to start; it stops when it expires.
4. Stop `SwiftAutoTrader-Paper-Local`, enable and start `SwiftAutoTrader-Live-Local`
   (`manage.ps1 -Action RegisterLiveTask` if it is not registered). The paper and live tasks
   refuse to run together.
5. Check the worker output for `MODE=LIVE account=...XXXX` and `self-check ok`. It refuses
   to start live if the journal is missing, the kill switch cannot be read, or the account is
   not the pinned one.
6. Only then: `-Action ResumeEntries`.

To stop new entries at any time: `-Action PauseEntries`. To go back to paper: set both files
back to `PAPER_ONLY` / `false` and restart.
