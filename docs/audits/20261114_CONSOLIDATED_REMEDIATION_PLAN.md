# Consolidated integration and attended deployment plan
Status: NOT LIVE CERTIFIED. Existing production runtime unchanged. No automated brokerage writes added.

## Baseline and evidence
PR #24 at 162c59218c865edf7db6885cb9a7a420a12b80ae is the remote base; owner-reported installed release 0c090e034ba7a761a1eb1a0cd08e06c91ddf8098 requires exact local verification. External unattended candidate: 127 synthetic tests reproduced against reconstructed dependencies, not a Windows or live result. Do not merge competing authorization paths. Official Robinhood documentation (https://robinhood.com/us/en/support/articles/trading-with-your-agent/) describes eligible external agent order execution and broker-required approvals; account authorization and actual response schemas are unverified.

## Targeted reviewed-candidate repair artifact
Swift-Reviewed-Integration-Repairs.zip SHA256 2f88290832ecd5ebed9a5abcd477b55a369f9e79d780cb05f3442d1bb1dc0918; owner-held artifact, not uploaded executable code. 15 manifest entries verified. 90 tests passed in 1.46s with Python 3.13.5, network blocked, reconstructed archived runtime and explicitly loaded test-only shim. Early emergency stop, stable review notification keys and single-use retry permission tested. Monitor stop-loop and unknown daily P&L formatting changed but not dynamically tested. No native PowerShell or real broker testing. Per-order approval retained.

## One staged deployment workflow (attended real orders only)
1. Freeze exact source and manifest; keep PIN, DPAPI, Telegram session and production journal untouched. Verify installed release and dependencies without shims. Stop with explicit operator approval before any changes to installed files.
2. Consolidate operational repairs into reviewed source and test native Windows PowerShell 5.1 / existing Python, full regression suite, exact imports and lock behavior. Reject missing or divergent base instead of patching blindly.
3. Exercise read-only monitoring / isolated paper simulation: disconnect/reconnect, crash recovery, stale or revised alerts, same-contract lots, unknown outcomes, cancellation/fill races, notification failures, process health and shutdown. Incomplete recovery must not fabricate authorization or ownership. Validate that read-only mode cannot invoke broker writes.
4. Read-only field verification of account eligibility, exact quote/instrument, fees, buying-power semantics, pagination, order/position snapshots and provider review behavior. No real test orders or settings changes. Redact receipts and retain local private state.
5. Build validated release in staging with fixed hashes; approved code-only promotion and rollback. Never roll back transaction state; stop and verify actual process exit, not just stop-file presence. Background startup/restart permitted for monitoring and paper only; real requests require individual review. No paid CI, no Phoenix/Kalshi changes.
6. Optional supervised brokerage verification after explicit individual order authorization; report actual fill/cancellation evidence without treating simulations as live results. Unverified or unknown outcomes block further conflicting requests.

## Remaining NO-GO gates
Exact installed runtime, full native Windows tests, reviewed release integration, missed-followup edit/retraction handling, broker reconnect, true P&L/fee evidence, notifications, service and rollback, live order lifecycle. No unattended live certification. No scheduled live service or automatic approval. No deployment or real trades performed.
