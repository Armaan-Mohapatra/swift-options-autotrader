# Optional local AI token-efficiency setup (Windows)

This document **does not install anything**. These commands must run on the Windows host that actually runs Codex/Claude Code, not merely in a remote GitHub cloud container. Do not run installers within a trading runtime or change any trading services.

## 1. Measure before changing anything

With Node.js/npm available, run an on-demand local report (not a daemon):

```powershell
node --version
npm --version
npx --yes ccusage@latest codex daily
npx --yes ccusage@latest claude daily
npx --yes ccusage@latest session
```

`ccusage` reads locally accessible agent-session data. These token totals/API-price estimates are **not** guaranteed to equal the subscription allowance or account-side billing. Do not publish raw session logs, prompts, file paths, or secrets.

## 2. Optional RTK command-output reduction

Only after verifying official release/source and licensing, and after backing up user-level agent settings:

```powershell
winget install --id rtk-ai.rtk --exact
```

Open a new terminal and inspect first:

```powershell
rtk --version
rtk init --global --dry-run
rtk init --global --codex --dry-run
```

If review confirms the expected **user-level** hook changes and your installed Codex/Claude clients support them, activate one integration at a time:

```powershell
rtk init --global
rtk init --global --codex
rtk gain
```

Verify hooks in a disposable non-trading directory with a harmless command. Do not blindly patch existing settings or overwrite another hook; preserve backups and rollback instructions. For safety or release failures inspect uncompressed original output.

## 3. What NOT to enable by default
- No Caveman cache keepalive, transparent third-party proxies or cloud auto-routing.
- No automatic agent teams, continuous Goals, recursive prompts, or background inference monitors.
- No simultaneous compaction tools before establishing a one-tool baseline.
- No paid CI, project runtime dependency changes, or live/paper worker restarts.

## 4. Measure success
Compare two equivalent **fully verified** tasks using the same model and similar scope. Measure total input+output tokens, retries, time, any missing diagnostics, and test pass rate. Remove an optimizer if correctness or reliability degrades.

Official projects: https://github.com/rtk-ai/rtk and https://github.com/ccusage/ccusage .
