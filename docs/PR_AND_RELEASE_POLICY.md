# PR, commit, reproducibility and reporting policy

- A phase's work is on its own `codex/phase-N-*` branch off reviewed base; never commit directly to main or modify unrelated repositories.
- Each PR must include scope/phase, reviewed diff, no secrets/paid data, actual full branch SHA, test counts (pass/fail/skip), Windows/Linux job URLs with real conclusions, dependency provenance/licensing, reproducer commands, migration impact and remaining limitations.
- No merge until authorized reviewer approves and all mandatory checks pass. Do not use a PR to authorize any order or enrollment.
- Verify Git commit SHA by refetching branch/commit via GitHub after push; compare local source file checksums/manifest where appropriate. No force pushes except reviewed recovery with lease.
- Use only synthetic fixtures and mock brokers in CI; never configure real account/Telegram credentials or session caches.
- Reproducibility: target pinned direct dependencies now; a full resolved, cross-platform lock, immutable workflow action SHAs, dependency license attestations and clean venv validation are required before any **hermetic/release-ready** claim; passing the Phase 0 matrix establishes only **foundation CI_PASSED**, not reproducibility or release approval.
- Each `docs/PHASE_REPORTS/PHASE_N.md` must state `IMPLEMENTED`, `TESTED`, `MOCK_ONLY`, `BLOCKED`, `REQUIRES_LOCAL_AUTH` distinctions, real links, exact test counts, security/license review and honest remaining work.
- Phase 7 may tag only an independently reviewed, mandatory-green **PAPER-ONLY** release. Installation, live authentication and trading still require separate steps and approval.
