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
(uses `DB_PATH`). The status line also shows the mode and the account ending.

- It sets `paused=1` in `paper_engine_control`, the same row the engine and `LiveBroker`
  read before **every** entry. It takes effect on the next entry; no restart is needed.
- Exits and cancels of orders you already own are **never** blocked by it.
- `entries-resume` refuses while an emergency stop is active; reset that manually.
- Unreadable control data counts as blocked.

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
`_call_write` refuses. A denial that mentions a position, realized P/L or order field is a
response-shape mismatch to fix before going live.

## Deploy this branch to the installed Swift copy (stays PAPER)

Default install root is `%LOCALAPPDATA%\SwiftAutoTraderPaper`. Run as your normal user.

1. Get the exact code in a source folder that is separate from the install:
   `git fetch origin; git checkout feat/live-broker-integration-20261117; git status --short`
   (must print nothing), then `$sha = git rev-parse HEAD` and confirm it is the PR head.
2. If you run tests or CLI commands by hand, remove any `PAPER_ONLY`, `PAPER_TRADING` and
   `PAPER_TRADING_ONLY` user environment variables that hold values the app rejects.
   (The service itself overrides them.)
3. Stop the service: `.\deployment\windows\manage.ps1 -Action Stop -Confirm:$false`
4. Back up: `.\deployment\windows\manage.ps1 -Action Backup -Confirm:$false`
5. Preview: `.\deployment\windows\setup.ps1 -DryRun` (no changes; stop if it names
   Phoenix or Kalshi paths).
6. Install the release over the existing install:
   `.\deployment\windows\setup.ps1 -Apply -ExpectedCommit $sha -NoSchedule -Confirm:$false`
   **Use `-NoSchedule` for an upgrade.** Without it the installer activates the new release
   and then fails because the `SwiftAutoTrader-Paper-Local` task already exists. The existing
   task keeps working: its scripts are refreshed and it launches the active release. The
   installer adds a new release, keeps `data`, `secrets`, `config` and `deployment.json`, and
   saves the old release for `-Action Rollback`. It needs network access for the locked install.
7. Confirm it is still paper: `.\deployment\windows\manage.ps1 -Action Status` shows
   `PAPER_ONLY`; `-Action EntriesStatus` works.
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
5. In Notepad, edit `<install>\config\settings.json`: add `"live_account_number"` (the full
   number of the MCP account, from the Robinhood app) and `"live_journal_path"`
   (for example `C:\\Users\\YOU\\AppData\\Local\\SwiftAutoTraderPaper\\data\\live\\journal.sqlite`).
   Leave `"mode": "PAPER_ONLY"` and `"live_enabled": false` for now.
6. Create the empty journal once: `.\deployment\windows\manage.ps1 -Action InitLiveJournal`
7. Rehearse as above and read `logs\live_rehearsal.jsonl` until every `WOULD_SEND` and every
   `DENIED` makes sense.

## Going live (only on your explicit go)

Do these yourself; no tool or agent should set them. First run `-Action PauseEntries` so the
service starts blocked.

1. In `config\settings.json` set `"mode": "LIVE"`, `"live_enabled": true`,
   `"broker_backend": "robinhood_live"`, with `live_account_number` and `live_journal_path`
   present.
2. In `deployment.json` set `"mode": "LIVE"`, `"live_enabled": true`,
   `"broker_writes_enabled": true`. The supervisor refuses to start if the two files
   disagree about live mode.
3. Restart: `.\deployment\windows\manage.ps1 -Action Restart -Confirm:$false`
4. Check the worker output for `MODE=LIVE account=...XXXX` and `self-check ok`. It refuses
   to start live if the journal is missing, the kill switch cannot be read, or the account is
   not the pinned one.
5. Only then: `-Action ResumeEntries`.

To stop new entries at any time: `-Action PauseEntries`. To go back to paper: set both files
back to `PAPER_ONLY` / `false` and restart.
