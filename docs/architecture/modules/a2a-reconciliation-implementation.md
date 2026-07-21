# Durable automatic A2A reconciliation

Status: Implemented baseline
Last updated: 2026-07-21

## Scope

This increment automatically polls known outbound A2A 1.0 Tasks without turning Redis delivery or
an in-memory timer into business truth. Every schedule, attempt, failure and worker claim is stored
on the tenant-scoped `RemoteTaskCorrelation` in PostgreSQL.

It intentionally does not recover an initial `OUTCOME_UNKNOWN` send that has no remote Task ID.
AgentMesh cannot safely invent that identity or repeat a send whose delivery is ambiguous.

## Durable scheduling and claiming

An active remote response stores `next_poll_at`. The dedicated reconciler selects only
`WAITING_REMOTE` correlations with a known remote Task ID whose schedule is due. It orders by due
time and stable ID, then claims a bounded batch with `FOR UPDATE SKIP LOCKED`.

Each claim stores `poll_lease_owner` and `poll_lease_expires_at`. Another worker skips a live claim;
if a process crashes, the row becomes eligible after the short lease expires. A successful active
response clears the lease and schedules the next poll. A terminal or intervention response clears
the lease and removes the schedule.

Explicit operator reconciliation uses the same lease field but ignores `next_poll_at`, preventing a
manual request from racing an active background claim.

## Failure convergence

Credential acquisition and transport failures are persisted rather than thrown away. Consecutive
failures use exponential backoff from `AGENTMESH_A2A_POLL_FAILURE_BASE_SECONDS`, capped by
`AGENTMESH_A2A_POLL_FAILURE_MAX_SECONDS`. Success resets the consecutive failure counter. At
`AGENTMESH_A2A_POLL_MAX_FAILURES`, the correlation enters `INTERVENTION_REQUIRED`, stops automatic
egress and records an auditable error on the correlation, Task and Run.

Malformed remote responses already use the existing intervention path and are not retried
automatically. This avoids converting a stable protocol/schema incompatibility into a polling storm.

## Deployment

The `a2a_reconciliation` Feature Gate depends on `a2a_delegation` and is off in every built-in
profile. Run `agentmesh-a2a-reconciler` after enabling the Gate. Docker Compose additionally places
the process behind the `a2a` profile:

```bash
docker compose --profile a2a up
```

The process needs PostgreSQL and controlled A2A egress but not Redis or LangGraph checkpoints. It
reuses the workload-bound Credential Broker when that Gate is enabled. Cancellation states use the
same durable scheduler and lease. Streaming, push callbacks, Peer circuit breaking and
reconciliation metrics remain later increments.
