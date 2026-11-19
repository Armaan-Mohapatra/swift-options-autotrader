# Dependencies / license review (2026-11-12)

Phase 0 **runtime:** Python 3.11+ **standard library only** (`sqlite3`, `argparse`, `dataclasses`, `decimal`, etc.). No network/client package is installed by this repository at runtime.

| Dependency | Proposed version | License / verification | Role |
| --- | --- | --- | --- |
| `pytest` | `9.2.0` (Oct 27, 2026) | MIT, PyPI metadata: https://pypi.org/project/pytest/ | test extra only |
| `ruff` | `0.16.8` (Sep 16, 2026) | MIT, PyPI metadata: https://pypi.org/project/ruff/ | test/lint extra only |
| `setuptools` | `84.2.1` (pinned in Phase 1) | MIT, https://pypi.org/project/setuptools/84.2.1/ | package build only |
| `Hydrogram` | **NOT INSTALLED** | PyPI last 2024-06-30, LGPLv3: https://pypi.org/project/Hydrogram/ | Rejected in Phase 1 in favor of maintained MIT Telethon 1.45.0 |
| Official `mcp` SDK | **NOT INSTALLED** | Official source https://github.com/modelcontextprotocol/python-sdk | Candidate Phase 4; verify current version, license, API and official Robinhood connectivity |

**Phase 0 historical gap (remediated in stacked Phase 1 branch):** the original Phase 0 PR #1 had direct-only dependency pins, no `uv.lock` and floating GitHub Actions tags. Stacked PR #10 now commits a universal hashed lock, pins the build backend, uses immutable verified Actions SHAs, and validates a package/version/license inventory on four CI platforms. See `docs/SUPPLY_CHAIN.md`, `scripts/audit_lock.py`, and https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43285018249. This still does NOT establish fully hermetic builds, independent CVE audits, cryptographically signed artifacts or software bill-of-materials attestations; residual issue #2 remains open.


## Phase 1 selection

`Telethon==1.45.0` (MIT; PyPI release 2026-09-10, https://pypi.org/project/Telethon/) is an optional `telegram` extra. Documented `TelegramClient` and `events.NewMessage`/`MessageEdited` APIs were reviewed against Telethon 1.45 docs (https://docs.telethon.dev/en/stable/). It replaces the previously planned Hydrogram 0.2.0 (LGPLv3; last PyPI 2024-06-30). CI installs the pinned direct version from resolved universal `uv.lock` with package archive SHA-256 digests on Windows and Linux; official real user session behavior remains unverified. Full independent signed SBOM / advisory scanning remains issue #2.

GitHub Actions checkout v4 and setup-python v5 are pinned to immutable GitHub public commit SHAs, verified from their tag refs.
