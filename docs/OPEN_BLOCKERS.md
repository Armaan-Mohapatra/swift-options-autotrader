# Open blockers — do not misstate as completed

| ID | Blocker | Owner / resolution path | Severity |
| --- | --- | --- | --- |
| GH-001 | **RESOLVED:** Owner-created private repository verified; minimal README from `main` preserved on the untouched base. | Do not recreate; preserve main and publish a separate feature branch. | Resolved |
| GH-002 | **RESOLVED:** isolated branch, 70-file commit, PR #1 and 4/4 green Linux/Windows CI jobs are independently verified. | See `docs/PHASE_REPORTS/PHASE_0.md` and the GitHub Actions run https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43278754406. | Resolved |
| DEP-001 | **PARTIALLY RESOLVED in stacked PR #10:** universal `uv.lock`, dependency artifact SHA-256 hashes, pinned build backend and immutable Actions SHAs; clean locked installs and inventory audit passed on Windows/Linux Python 3.11/3.13. Full offline-hermetic builds, independent CVE scanning, signed SBOM/attestations and verified commit signatures still missing. | See `docs/SUPPLY_CHAIN.md` and issue #2. Do not claim signed release or completed outside audit. | Residual release gate |
| DEP-002 | **Phase 1 client choice resolved:** Telethon 1.45.0, MIT, published September 2026, installed from pinned lock. Live private-channel authorization/API behavior not yet verified. Official MCP SDK remains unverified. | Telethon local authorization/forward validation later; MCP SDK in Phase 4. | Future integration gate |
| OPS-001 | Telegram channel authorization/identifiers and paid content unavailable by design; no authentic ingestion tests. | Phase 1 local secure user approval and synthetic CI mocks. | Future local auth |
| OPS-002 | Robinhood official Agentic Trading MCP authentication, schema and permission set not validated; no real order permissions. | Phase 4 official docs/mock contract tests and later separate local auth. | Future integration gate |
| OPS-003 | Real market-data option chain validation, option tick/session constraints, buying-power and exact order review not implemented. Phase 2 test catalog is synthetic only. | Phase 4 official connector and Phase 3 risk/order integration; no automated entry eligibility before real catalog validation. | Future execution gate |
| OPS-004 | **RESOLVED FOR SYNTHETIC PAPER ONLY:** Phase 3 implements durable paper fills, cancel/fill races, risk gates, idempotent intents, reconciliation and stop. No official broker/market forward-validation. | [Phase 3 report](PHASE_REPORTS/PHASE_3.md); Phase 4/6 require new independent approvals. | Synthetic scope complete; live validation open |
| OPS-005 | Windows runtime, protected secrets, scheduler, secure online backups and metrics not implemented. | Phases 5–6. | Future deployment gate |

None of these blockers authorizes skipping tests, accessing user devices, or enabling live trading.


### Phase 1 local gates

- AUTH-001: Authorized paid Swift Telegram membership, developer API credentials and a manually enrolled protected local Telethon user session not yet supplied/tested.
- CHANNEL-001: Actual signed channel peer ID and private membership/cache verification need local Windows authorization.
- FORWARD-001: Real new/edit/reconnect delivery, missed-update gap checks, local SQLite file ACL and rate-limit behavior are unverified. No automatic history backfill.
- SUPPLY-001: **Resolved:** transitive universal `uv.lock` with 37 hashed artifacts and Windows/Linux CI tests, immutable GitHub Actions pins, pinned setuptools, license inventory. **Still open:** independent advisory/CVE scans, signed SBOM, artifact attestations and hermetic offline build (issue #2).

### Phase 2 synthetic parser gates

- PARSER-001: Real authorized Swift paid-channel alert formats (including actual spacing, edits, replies and duplicates) were **not** accessed. 81/81 in-sample synthetic agreement is not measured real-world accuracy; no forward validation exists.
- CATALOG-001: The deterministic parser requires a real verified option listing, including expiration year. Until the official read-only broker integration is available in a later phase, `parse-replay` holds otherwise syntactically valid entries at `REVIEW_REQUIRED / INSTRUMENT_CATALOG_REQUIRED`.
- REVIEW-001: Ambiguous updates and missing reply ancestry persist as review records; no reviewed operator UI or authority to resolve into executable trades exists yet.
- EXEC-001: **Synthetic-only resolved in Phase 3:** CLOSE_100_PERCENT triggers cancellation confirmation and then a paper sell-to-close limit covering the confirmed matching fills. Real broker confirmation/forward execution still requires Phase 4 and later approval.
- GITHUB-001: Draft PR #11 stacks on #10 and #1; review/merge order is explicit and no branch was automatically merged.

### Phase 3 paper lifecycle gates

- PAPER-001: The paper broker, paper fills, quote crossing and buying-power model are deterministic synthetic test fixtures; they do not represent a validated Robinhood account, real option chain, option bid/ask, fees, slippage or fills.
- PAPER-002: The standalone `paper-cycle` command is intentionally not a trading-ready live Telegram consumer: without independent catalog/session/permissions checks it fails closed and does not mutate signal eligibility. Phase 4 must supply a reviewed verified instrument and account source, still paper-only.
- PAPER-003: Exit orders rely on an explicit fresh matching test BID quote; if missing or unfilled, the position remains exposed and a durable alert requires operator action. Do not infer a successful close from intent submission.
- PAPER-004: No continuous Windows supervisor, production CPU/RAM profiling, data backups or field-auth validation. Phase 6 is a separate local deployment authorization; Phase 7 still requires full replay/security audit.
- GITHUB-002: Phase 3 PR #12 is stacked on PR #11, both draft/unmerged. No branch was merged or deployed.

## Phase 4 unresolved local validation gates

- Official Robinhood MCP OAuth/PKCE dynamic registration, redirect and token
  refresh are untested with real accounts. Local Windows enrollment is required.
- Exact live `tools/list` schemas, account agentic eligibility, options level,
  buying power, tick sizes, supported hours, approvals, quote timestamps,
  option-order response shapes and idempotency visibility need read-only testing.
- `mcp==2.3.0` remains an optional *local* SDK dependency, not part of the
  existing universal `uv.lock`. A platform-resolved lock and SBOM are needed.
- Live brokerage write/cancel are intentionally disabled, not a future phase
  being activated automatically.
