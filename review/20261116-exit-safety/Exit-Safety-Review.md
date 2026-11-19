# Exit incident safety candidate

Base: 86828420f2544f942b64d723910fa194af58f8d9. This is a targeted safety review, not an activation release.

- Refusals are typed and distinguished from uncertain placements. A read-only review interruption cannot be mistaken for a sent order.
- Definite exit refusal records an immediate UNFILLED_EXIT incident, preserves exposure, and does not terminate the normal close-processing cycle.
- New entries are held whenever an existing closure request or sell intent has residual exposure or unresolved order state. The hold is recalculated from SQLite and checked before durable entry intent creation. It is independent of signal labels and process lifetime.
- Existing reconciliation and close processing remain available. Health marks unresolved exits degraded; notification scan elevates UNFILLED_EXIT to critical.
- No replacement real-money order is generated after rejection, unknown execution, or provider-required approval. Broker permission is not bypassed. Manual or external closure requires verified attribution; reconciliation must not invent fills.
- Holds apply to normal pending exits too. This is intentionally conservative. Unknown outcomes, broker outages, and approval-required exits may still require operator intervention. A rejected exit is not magically closed.
- Tests: hermetic SQLite matrix and reopen regression added. Full candidate regression and native Windows verification are required. No claim of live readiness. Prior 808 passed/2 skipped is for base only. No authorization, real broker call or deployment was performed.
- Remaining separate work: native power qualification, legacy audit and lint remediation, separately authorized brokerage acceptance. 
