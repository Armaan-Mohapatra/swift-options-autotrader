# Phase 6 checkpoint — Windows local PAPER deployment source

**Status: CODE_COMPLETE / LOCAL_TESTED / CI_BLOCKED / PAPER_ONLY / NO_HOST_INSTALL.** This is NOT a live/trading-ready release. The Phase 5 base PR #14 remains **CI_BLOCKED**, with GitHub Actions jobs failing before runner assignment; the Phase 6 [Windows PR workflow](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43371067323) and [multi-OS PR workflow](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43371067349) also failed **prior to runner allocation** (zero executed steps), so no Windows CI pass can be claimed.

## Provenance

- Private repository: https://github.com/Armaan-Mohapatra/swift-options-autotrader
- Base: `codex/phase5-analytics-dashboard` at `ce3d65130b7d9ea976de87515a779c5438dd90c0`, draft PR #14, unmerged.
- Branch: `codex/phase6-windows-deployment`; [draft PR #16](https://github.com/Armaan-Mohapatra/swift-options-autotrader/pull/16) is stacked on Phase 5 and unmerged. First implementation commit: `d8475105efd956f071a6cc4339b45f615124b9f3`; later hardening/report commits may advance HEAD.
- `main`, earlier draft PRs and Phoenix/Kalshi/other repositories remain unmodified. No user's physical Windows computer was accessed.

## Implemented source

- `deployment/windows/{common,setup,manage,service,secrets,healthcheck}.ps1`: preview-first Windows PowerShell scripts, safe absolute root, separate application/release/data/secrets/logs/backups directories, verified clean source SHA, pinned `uv==0.10.0` bootstrap + `uv sync --locked`, SQLite WAL migrations, user-and-SYSTEM NTFS ACLs, DPAPI secret entry, user-scoped Task Scheduler logon task, stop/start/restart/health/status/backup/restore/rollback/uninstall. `-NoSchedule` confines an installation smoke to the GitHub runner temporary area.
- `src/swift_autotrader/windows_runtime.py`: bounded restart supervisor, Windows/POSIX OS single-instance lock, startup/daily consistent SQLite backup, hash-checked offline restore, read-only localhost dashboard, optional authorized receive-only Telegram client, local notification polling, heartbeat, fail closed on missing configuration, no automatic parser-to-order submission.
- `src/swift_autotrader/windows_status_notifications.py`: optional, default-disabled Telegram **status bot** to a separate operator-controlled chat; only symbolic error codes, DPAPI-protected bot token injected locally, no paid Swift text. Delivery is at-least-once and is not a transaction guarantee.
- `scripts/build_windows_package.py`: SHA-bound, deterministic allowlisted package with per-file SHA-256 manifest, rejects credentials, databases, `.env`, `.session`, git metadata and unlisted archive entries; a package is not a release tag.
- `.github/workflows/windows-deployment.yml`: Windows PowerShell 5.1 syntax parse, no-side-effect preview, synthetic unit checks, isolated real install `-NoSchedule`, backup/uninstall preservation, checksum package artifact. All operations use temporary disposable GitHub runner locations and fake settings. It does not enroll credentials, register a Windows task, or call a broker.
- `docs/{WINDOWS_RUNBOOK,OPERATIONS,ROLLBACK}.md` and `deployment/windows/README.md`: practical instructions, operator checks and recovery procedures.

## Local verification

Portable Phase 6 focused test command:

```sh
PYTHONPATH=src python -m pytest -q \
  tests/unit/test_windows_runtime.py \
  tests/unit/test_windows_status_notifications.py \
  tests/unit/test_windows_package.py \
  tests/integration/test_windows_deployment_contract.py
```

**24 passed, 0 failed, 0 skipped** on the isolated development Python runtime. Python source compiled and heuristic secret scan passed. The local Phase 5 working tree was reconstructed from earlier source bundles rather than a private Git clone; a broad run yielded **248 passed, 14 failed, 37 setup errors**, predominantly because stale local Phase 0–3 fixture copies expected fewer migrations and an older `Settings` interface. This is **NOT** a successful complete regression and must **NOT** be used as the release gate. The authoritative GitHub branch must run CI using actual remote source files. PowerShell syntax/execution and Windows Task Scheduler registration were **NOT locally run** (Linux development host).

## Known limits — must be verified before installation

1. **GitHub Actions:** Phase 5 base jobs had no allocated runner and no executed test steps, so a full Linux/Windows code suite has not been CI-validated. Phase 6 Windows synthetic smoke has not run until actual GitHub runners start. Owner must inspect account Actions availability, quotas and billing plus runner status.
2. **Pinned bootstrap:** `uv==0.10.0` and Python 3.11 minor are selected; Python patch and the `uv` bootstrap artifact are not SHA-locked. The Python runtime lock includes approved packages, but optional Robinhood MCP SDK resolution, signed release build, independent CVE/license/security scan and hermetic offline rebuild still require audit.
3. **Logon-only startup:** Current-user DPAPI requires same user identity. Task starts after that user logs in; it does not guarantee pre-login reboot recovery. Long-running restart policy needs field validation.
4. **Real Telegram:** No legitimate `.session`, paid channel authorization, Swift private text, or actual user account was connected. Windows enrollment must be manual and reviewed. The Telegram client is receive-only. Local status bot delivery is opt-in to a separate chat.
5. **No automatic order execution:** The deployed supervisor does not run `paper-cycle` or `parse-replay`. Verified option instrument/year, broker market-session/tick rules and fresh quote+buying-power preflight are not wired/field-verified. Running the parser without a catalog risks irreversibly putting authentic alerts into review, so this is intentionally disabled. Phase 3/5 synthetic paper engine remains available via isolated test/replay commands, not as an unattended trading service.
6. **Data integrity:** Backups are consistent via SQLite backup API; restore refuses unexpected WAL sidecars, manifest/hash errors and active single-instance lock. Backups include sensitive local message content and must be stored privately. Code rollback does not reverse SQLite migrations. Power-loss and long-running production recovery remain unverified.
7. **Resources:** Five-second supervisor polling, minute-scale notification checks, 2MiB × 4 supervisor logs, seven database backup retention and no automatic market scanning are low-overhead design choices. Actual Windows CPU/RAM/disk usage is **not measured**, and historical data size determines real storage.

## Remaining user-host actions (future, separately approved)

Read [beginner runbook](../WINDOWS_RUNBOOK.md). Have Codex check the host's Python 3.11 launcher/Git, NTFS/BitLocker, network, Task Scheduler, identity/logon, available disk/memory and existing trading apps; verify reviewed commit SHA; run dry-run; request permission to install; use the isolated `-Apply` mode after CI approval; perform local Telegram session/DPAPI enrollment; start task; validate dashboard/reconnect/reboots/backups/restore/rollback and resource consumption. Live brokerage writes remain disabled; do not run any production broker orders.

**Phase 6 stop gate:** No merge, release tag, live auth or Windows installation performed; Phase 7 integration/security audit needs separate instruction. CI must pass before "release-ready" can truthfully be claimed.

## Final hardening and CI evidence

The follow-up implementation hardening commit `19204a5766d1f2d1dc7f9962ccbeef1f31adaba6` added idempotent same-release install handling, validation of scheduled-task target identity before status/control actions, BOM-free Windows settings manifests, `uv` system-Python selection, preservation of failed child status and a secret-filtered status bot environment. Its local focused suite **24 passed** and compilation/heuristic secret scan passed. The final-code [Windows workflow run 43371545210](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43371545210) and [multi-OS run 43371545243](https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43371545243) each reported **failure before runners were assigned** (zero steps). A follow-up documentation-only commit may supersede this code commit; consult PR #16 for HEAD and its own CI. Do not claim CI passed.
