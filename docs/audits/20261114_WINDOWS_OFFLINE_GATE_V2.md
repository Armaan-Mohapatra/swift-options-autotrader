# Windows offline gate V2 — owner-reported execution evidence
Date: 2026-11-14 (Pacific).
Source: owner-pasted PowerShell transcript. Not a fresh independent Windows run by the reviewer.

## Results
- First runner failed during test collection: replacement of subprocess.Popen with a function prevented Windows asyncio.windows_utils.Popen subclass definition, with cascading asyncio import errors.
- V2 static gate: STATIC_COMPATIBILITY_PASS; installed release marker matches; 15 manifest entries verified; 11 Python files parsed; missing_static_imports empty.
- V2 synthetic suite: 90 passed in 5.65s, exit code 0.
- Final receipt: EXACT_LOCAL_SOURCE_SYNTHETIC_PASS; compatibility_shims_added=false; live_certified=false; production_modified=false.
- Static receipt: broker_calls=false; release_bytes_certified=false.
- Runtime source SHA256 values were reported for broker/mcp_local_auth.py, broker/mcp_paper_data.py, broker/mcp_transport.py, broker/robinhood_mcp.py, config.py, launch_preview.py, parser/rules.py, signals/identity.py, windows_runtime.py. Marker matching and static import availability do not establish complete Git release identity.

## Disposition
Reviewed portfolio candidate's local Windows synthetic compatibility gate passed. This does not apply to the external unattended candidate's separate 127-test suite. No real order tests, real authorization checks, production promotion or unattended real-money certification. Next gates: fresh read-only brokerage field verification, monitoring/service lifecycle validation, and a non-destructive reviewed release deployment plan with individually authorized orders. Preserve existing journals, account pin, credentials, and unrelated runtimes.
