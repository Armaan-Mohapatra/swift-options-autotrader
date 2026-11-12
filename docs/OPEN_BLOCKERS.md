# Open blockers — do not misstate as completed

| ID | Blocker | Owner / resolution path | Severity |
| --- | --- | --- | --- |
| GH-001 | **RESOLVED 2026-11-12:** User created private `Armaan-Mohapatra/swift-options-autotrader`; initial main has a one-line README. | Do not recreate; preserve main and publish a separate feature branch. | Resolved |
| GH-002 | Feature branch/PR/Actions evidence pending publication and review. | Publish foundation, inspect GitHub commit/PR, and verify actual Windows/Linux CI. | Pending verification |
| DEP-001 | The repository pins direct pytest/Ruff versions, but does **not** carry fully resolved cross-platform transitive hashes/`uv.lock`; sandbox cannot confirm a clean Windows install. | Generate/verify full lock in authorized development environment, pin Actions by immutable SHA and verify third-party license inventory before merge. | Reproducibility gap |
| DEP-002 | Hydrogram last PyPI release dated 2024 with LGPLv3; long-term maintenance/API fitness needs evaluation. MCP SDK schema/version must be verified in future respective phases. | Phase 1/4 evaluate official sources, LGPL compliance and any alternative before including runtime libraries. | Future integration gate |
| OPS-001 | Telegram channel authorization/identifiers and paid content unavailable by design; no authentic ingestion tests. | Phase 1 local secure user approval and synthetic CI mocks. | Future local auth |
| OPS-002 | Robinhood official Agentic Trading MCP authentication, schema and permission set not validated; no real order permissions. | Phase 4 official docs/mock contract tests and later separate local auth. | Future integration gate |
| OPS-003 | Real market-data option chain validation, option tick/session constraints, buying-power and exact order review not implemented. | Phases 2–4. | Future execution gate |
| OPS-004 | Paper engine, fill reconciliation, cancel/fill races, durable submissions and kill switch not implemented. | Phase 3. | Future execution gate |
| OPS-005 | Windows runtime, protected secrets, scheduler, secure online backups and metrics not implemented. | Phases 5–6. | Future deployment gate |

None of these blockers authorizes skipping tests, accessing user devices, or enabling live trading.
