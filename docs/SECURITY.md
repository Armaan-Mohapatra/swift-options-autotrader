# Security policy

## Protected assets and hard boundaries

- Only this private repository is in scope; **no Phoenix, Kalshi, historical unrelated trading data, cloud production or existing Windows service** may be read or modified.
- Never commit `.env`, paid/private Telegram content or identifiable channel info, MTProto `.session` files, cached auth tokens, brokerage account IDs, balances, statements, OAuth access/refresh tokens, private keys, or any brokerage write credentials. Public synthetic test strings are permitted.
- Use strict local file permissions and OS credential storage in the later Windows installation. The Phase 0 CLI has no auth integration.
- A legitimate authorized Telegram membership and appropriate Robinhood external-agent permissions are prerequisites to future local connectivity. Never circumvent access restrictions or user confirmations.
- Never infer that GitHub CI mocks provide approval for live orders. Neither docs nor repository actions can turn on live trading.

## Defense in depth

1. **Fail-closed config:** `PAPER_ONLY=true`, `LIVE_ENABLED=false`, `ALLOW_BROKER_WRITES=false`, `BROKER_BACKEND=paper`. Changing any flag to permit live action is rejected by the Phase 0 config schema.
2. **Fail-closed broker:** `RobinhoodMCPBroker` has no HTTP/OAuth client; write methods always raise `BrokerWritesDisabledError`. The paper adapter also raises `NotImplementedError` and cannot pretend to execute.
3. **No side-effectful diagnostics:** `smoke`, `config-check`, `dry-run` neither start sessions nor initialize state. Only explicit `migrate` creates local SQLite files.
4. **Data integrity:** WAL, uniqueness keys, immutable migration fingerprints. Phase 3 must persist intended submissions before broker calls and implement reconciliation before retrying ambiguous results.
5. **CI:** only synthetic tests; least-privilege `contents: read` workflow token. No secrets configured. Python script scans obvious forbidden paths and secret assignments; it is **heuristic** and not a comprehensive secrets audit.
6. **Supply chain:** Pin current direct development tool versions; later introduce full lock and SBOM/license scanners. Never assume declared dependencies are safe without checking transitive licenses.
7. **Incident procedure:** Stop order submission, revoke compromised token locally, verify all open/filled broker orders independently, preserve audit records, rotate credentials. No automatic broker kill command exists in Phase 0.

## Security reports

Because the project is private, report security issues privately to the repository owner through GitHub's private communications rather than a public issue. Do not include sensitive secrets in a bug report. No response SLA or email address is asserted.

## Security debt — explicitly not implemented

Access-controlled paid-content encryption at rest, secure Windows account storage, dedicated account preflight, live connector schema verification, complete secrets scanner, full dependency audit, signed release, database online backups, disaster recovery, idempotency/reconciliation implementation, market-hours gating, and supervised kill switch are later-phase requirements and must remain blockers until verified.
