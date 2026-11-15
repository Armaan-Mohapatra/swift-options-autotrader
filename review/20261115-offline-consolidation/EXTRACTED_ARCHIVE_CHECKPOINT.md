# Consolidated offline review checkpoint — November 15, 2026

REVIEW ONLY — not an installer, production build or live certification.

Archive: Swift-Consolidated-Offline-Review.zip
Bytes: 387608
Archive SHA-256: f2c4d086e10f0f766554d39671bafe9ae1094f4d9c4b2b6f372c344a84620e55
Bundle manifest SHA-256: 5f743ea535c9f34b018e4be58e758ca674a8e638bea5c6fa5aa503218f6af085
18 outer members, 17 manifest entries. Three inner archives have 84, 67, 9 members and 83, 66, 8 individually verified source entries.

Actual rerun from fresh extracted ZIP on Linux Python 3.13.5, pytest 9.0.2:
- Review baseline: 188 passed, one warning, 2.18s.
- Hardened isolated paper variant: 33 passed, one warning, 1.65s. Ten retained + 23 new.
- Operational rehearsal: seven passed, 0.007s, archival staging and mock rollback checks passed.
- Network, external execution and SQLite guard self-tests passed for both pytest suites.
- Status OFFLINE_REVIEW_PASSED_NOT_LIVE_CERTIFIED; 228 case executions, production_modified=false, broker_contacted=false, live_certified=false.

Prior owner-transcribed Windows results apply to prior artifacts, not this paper overlay. The overlay is explicitly blocked from startup; real-money activation remains blocked. The recovery script is review text, not natively tested. No production changes, brokerage calls, or local laptop work.

All 15 text files from the bundle (including manifest) were uploaded and their Git blob identities verified before writing this checkpoint. The three nested baseline binary ZIPs and complete outer ZIP remain in the owner-held archive; they are not published by this checkpoint. GitHub is not independently runnable without those archives. The defect register is the controlling readiness record. Keep PR #26 draft and unmerged.
