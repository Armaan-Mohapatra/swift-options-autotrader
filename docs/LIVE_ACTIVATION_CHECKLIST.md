# Live activation is NOT AUTHORIZED

> Update: the owner has authorized live trading through `LiveBroker` (see `AGENTS.md`). Live still starts only when every live setting is present, and only the owner's local configuration turns it on. Operating steps: [LIVE_OPERATIONS.md](LIVE_OPERATIONS.md). The checklist below stays a review aid.

All boxes remain unchecked. This is a future review aid, not an activation mechanism.

- [ ] Complete phases 0–7 with actual green Windows/Linux CI and reviewed changes.
- [ ] Validate real authorized Swift Telegram parsing via user-approved local session; no scraping or paid-access bypass.
- [ ] Confirm official Robinhood MCP documented tools, approval prompts, correct dedicated account permissions, secure OAuth lifecycle and read-only reconciliation.
- [ ] Verify every option instrument, expiry, ticker, tick-size, hours, order review and account/risk limit.
- [ ] Complete forward **paper-only** testing with monitored restart and network failure exercises, partial fills, 3600-second cancellations, stale alerts, ambiguous exits and idempotency.
- [ ] Review realistic execution, missing-data cases and risk; do not assume profitability.
- [ ] Create and rehearse manual kill switch, secure credentials, backups and rollback.
- [ ] Obtain **separate explicit user approval** and required platform approval before permitting any brokerage writes.
- [ ] Verify first deliberately limited live action and actual broker state manually; do not auto-enable for future session.

No current sprint phase, green PR, git commit, local machine availability or ChatGPT output fulfills the last approval gate.
