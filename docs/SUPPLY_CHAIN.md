# Dependency and license inventory — Phase 0 hardening / Phase 1

**Purpose:** Exact, inspectable dependency selection for safe read-only Telegram development. The locked CI and Python 3.11/3.13 Windows/Linux installs are evidence of reproducible resolution, not independent proof of malware absence or cryptographic build provenance.

- Universal `uv.lock` generated using `uv==0.10.0` in a GitHub Actions Ubuntu job, committed with exact versions, artifact HTTPS URLs, SHA-256 digests, and optional `dev` and `telegram` extras.
- A locked install (`uv sync --locked --extra dev --extra telegram`) and audit script run on both Windows and Linux under Python 3.11 and 3.13. The audit rejects unknown dependencies, untrusted indices, non-PyPI host URLs, missing SHA-256 digests, and changed versions.
- Build backend is `setuptools==84.2.1`, published 2026-10-20 under MIT; pinned directly in pyproject. `uv==0.10.0` is an exact CI resolver version, licensed MIT OR Apache-2.0.
- `actions/checkout` and `actions/setup-python` GitHub Actions are SHA-pinned to Git tag commits verified through the upstream GitHub API. CI has read-only contents permission and does not persist checkout credentials.

## Locked Python package licenses

| Package | Version | License | Purpose / source |
| --- | --- | --- | --- |
| colorama | 0.4.6 | BSD-3-Clause | pytest console support; https://pypi.org/project/colorama/ |
| iniconfig | 2.3.1 | MIT | pytest support; https://pypi.org/project/iniconfig/ |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Python packaging helper; https://pypi.org/project/packaging/ |
| pluggy | 1.7.0 | MIT | pytest hooks; https://pypi.org/project/pluggy/ |
| pyaes | 1.6.1 | MIT | Telethon encryption; https://pypi.org/project/pyaes/ |
| pyasn1 | 0.6.5 | BSD-2-Clause | RSA dependency; https://pypi.org/project/pyasn1/ |
| pygments | 2.21.0 | BSD-2-Clause | pytest rich tracebacks; https://pypi.org/project/Pygments/ |
| pytest | 9.2.0 | MIT | test runner; https://pypi.org/project/pytest/ |
| rsa | 4.9.1 | Apache-2.0 | Telethon encryption; https://pypi.org/project/rsa/ |
| ruff | 0.16.8 | MIT | lint / style; https://pypi.org/project/ruff/ |
| telethon | 1.45.0 | MIT | optional MTProto user client; https://pypi.org/project/Telethon/ |
| swift-options-autotrader | 0.1.0 | Private/proprietary | local project only; no published package |

Dependencies are limited to optional Telegram + development extras; no Robinhood library/API credentials are installed. Upstream licenses are identified from the package metadata, but distribution obligations for a future release and vulnerability advisories require a refreshed review at release time.

## Remaining caveats

- Hashes authenticate chosen package archive bytes against the lock record; do not prove that upstream project code is harmless.
- The build backend and `uv` executable are exact-version pinned but not part of the universal application lock; a fully hermetic offline build and signed software-bill-of-materials have not been produced.
- This audit is fail-closed and inventory-focused, not a substitute for SCA/CVE advisory scanning, attestation verification, source audit, or secure software release process.
- The CI source archive runs only synthetic test data, without paid Swift Telegram content or credentials.
