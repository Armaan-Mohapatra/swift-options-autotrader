# External portfolio candidate: independent intake audit
Disposition: NO-GO for live deployment. Documentation/evidence only; no executable candidate imported.
Base PR #24: 162c59218c865edf7db6885cb9a7a420a12b80ae. The user's installed release 0c090e034ba7a761a1eb1a0cd08e06c91ddf8098 was reported previously and was not inspected.
Candidate inner ZIP SHA-256: b595b0750be44059315c01350b5bfbff0c10c65e76cd02ec4b2322f7926d79c2.
22 declared file hashes matched. 16 Python files (including test-only shim) parsed with Python 3.11 grammar.
Original report: 127 tests passed. Independent Linux Python 3.13.5 run: 127 passed in 1.73s. First run failed collection (6 errors): archived runtime lacks load_pinned_agentic_account; its supplied shim was shadowed by environment startup hooks. Passing run explicitly loaded supplied test-only shim after blocking network sockets, and used archived cffd494 runtime plus source-only historical finalization patch and one-character repair to archived windows_runtime.py. This is not exact installed-source validation. No real account, credentials, orders, live notifications, or Windows machine accessed.
## Findings
- New execution mode in source does not establish actual broker authorization. Its provider eligibility contract and review response assumptions are unverified.
- The health record hardcodes supervised mode even when startup output differs. The startup report is not evidence of an end-to-end order lifecycle.
- Receiver restart counter never resets; no complete broker reconnect/shutdown integration tests. The stop file prevents writes but does not necessarily terminate a healthy receiver; rollback guidance must not presume STOPPED.
- Missed-followup recovery needs adversarial tests for edited/retracted pending messages and durable idempotence. The earlier review's read-only monitor, operator unknown outcome resolution and single-use retry improvements are not all present in this archive.
- Windows launcher and validator have not run natively. No service installed; real fee units, buying-power reservations, quote freshness, live order lifecycle and account pin remain unverified.
- Manifest covers 22 declared files; separate test-only evidence directory is not covered by that internal list. Entire inner archive hash matches.
- Candidate includes the older review report unchanged; its 87-test result applies to earlier source.
Do not import unverified execution authorization changes or overwrite existing companion/state. Retain original archive as owner-held evidence. No release, merge, CI execution or activation requested by this audit.
