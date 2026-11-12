# Open blockers — do not misstate as completed

| ID | Blocker | Owner / resolution path | Severity |
| --- | --- | --- | --- |
| GH-001 | **RESOLVED:** Owner-created private repository verified; minimal README from `main` preserved on the untouched base. | Do not recreate; preserve main and publish a separate feature branch. | Resolved |
| GH-002 | **RESOLVED:** isolated branch, 70-file commit, PR #1 and 4/4 green Linux/Windows CI jobs are independently verified. | See `docs/PHASE_REPORTS/PHASE_0.md` and the GitHub Actions run https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43278754406. | Resolved |
| DEP-001 | **PARTIALLY RESOLVED in stacked PR #10:** universal `uv.lock`, dependency artifact SHA-256 hashes, pinned build backend and immutable Actions SHAs; clean locked installs and inventory audit passed on Windows/Linux Python 3.11/3.13. Full offline-hermetic builds, independent CVE scanning, signed SBOM/attestations and verified commit signatures still missing. | See `docs/SUPPLY_CHAIN.md` and issue #2. Do not claim signed release or completed outside audit. | Residual release gate |
| DEP-002 | **Phase 1 client choice resolved:** Telethon 1.45.0, MIT, published September 2026, installed from pinned lock. Live private-channel authorization/API behavior not yet verified. Official MCP SDK remains unverified. | Telethon local authorization/forward validation later; MCP SDK in Phase 4. | Future integration gate |
| OPS-001 | Telegram channel authorization/identifiers and paid content unavailable by design; no authentic ingestion tests. | Phase 1 local secure user approval and synthetic CI mocks. | Future local auth |
| OPS-002 | Robinhood official Agentic Trading MCP authentication, schema and permission set not validated; no real order permissions. | Phase 4 official docs/mock contract tests and later separate local auth. | Future integration gate |
| OPS-003 | Real market-data option chain validation, option tick/session constraints, buying-power and exact order review not implemented. | Phases 2–4. | Future execution gate |
| OPS-004 | Paper engine, fill reconciliation, cancel/fill races, durable submissions and kill switch not implemented. | Phase 3. | Future execution gate |
| OPS-005 | Windows runtime, protected secrets, scheduler, secure online backups and metrics not implemented. | Phases 5–6. | Future deployment gate |

None of these blockers authorizes skipping tests, accessing user devices, or enabling live trading.


### Phase 1 local gates

- AUTH-001: Authorized paid Swift Telegram membership, developer API credentials and a manually enrolled protected local Telethon user session not yet supplied/tested.
- CHANNEL-001: Actual signed channel peer ID and private membership/cache verification need local Windows authorization.
- FORWARD-001: Real new/edit/reconnect delivery, missed-update gap checks, local SQLite file ACL and rate-limit behavior are unverified. No automatic history backfill.
- SUPPLY-001: **Resolved:** transitive universal `uv.lock` with 37 hashed artifacts and Windows/Linux CI tests, immutable GitHub Actions pins, pinned setuptools, license inventory. **Still open:** independent advisory/CVE scans, signed SBOM, artifact attestations and hermetic offline build (issue #2).
