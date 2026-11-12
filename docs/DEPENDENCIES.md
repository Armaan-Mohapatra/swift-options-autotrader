# Dependencies / license review (2026-11-12)

Phase 0 **runtime:** Python 3.11+ **standard library only** (`sqlite3`, `argparse`, `dataclasses`, `decimal`, etc.). No network/client package is installed by this repository at runtime.

| Dependency | Proposed version | License / verification | Role |
| --- | --- | --- | --- |
| `pytest` | `9.2.0` (Oct 27, 2026) | MIT, PyPI metadata: https://pypi.org/project/pytest/ | test extra only |
| `ruff` | `0.16.8` (Sep 16, 2026) | MIT, PyPI metadata: https://pypi.org/project/ruff/ | test/lint extra only |
| `setuptools` | `>=75,<85` | build backend version range; exact resolution still needs lock | package build only |
| `Hydrogram` | **NOT INSTALLED** | PyPI last 2024-06-30, LGPLv3: https://pypi.org/project/Hydrogram/ | Proposed Phase 1 read-only MTProto; review current maintenance and LGPL compliance first |
| Official `mcp` SDK | **NOT INSTALLED** | Official source https://github.com/modelcontextprotocol/python-sdk | Candidate Phase 4; verify current version, license, API and official Robinhood connectivity |

**Known gap:** direct development dependencies are pinned, but transitives and build-backend dependencies are not fully hash-pinned; no validated Windows/Linux lock exists yet. Do not represent this as hermetic reproducibility. The actual private repository ran four successful cross-platform GitHub CI jobs on 2026-11-12 (https://github.com/Armaan-Mohapatra/swift-options-autotrader/actions/runs/43278754406). A full transitive lock, immutable Actions SHAs and complete license/SBOM audit remain open as issue #2. Updating a dependency requires a separate reviewed change and redone tests.
