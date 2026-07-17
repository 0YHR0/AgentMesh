# Attempt lease renewal implementation

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Durable asynchronous execution](durable-async-execution.md),
[Formal Orchestrator and scheduler](formal/orchestrator-and-scheduler.md)

## 1. Scope

This increment lets an Execution Worker keep ownership of a long-running Attempt while the
workflow node is still running. It closes the previous limitation that one Agent node had to finish
inside a single `AGENTMESH_RUN_LEASE_SECONDS` window.

The implementation remains intentionally narrow: it renews the active Attempt lease for the
current single-Agent workflow. It does not add DAG scheduling, admission control, deadline policy,
or external runtime cancellation.

## 2. Renewal contract

When a Worker acquires a Run, it creates a `TaskAttempt` with:

- a stable `worker_id`;
- an opaque `lease_token`;
- a monotonically increasing `fencing_token`;
- `lease_expires_at` and `heartbeat_at` timestamps.

While `WorkflowRunner.run()` is active, `RunExecutionService` starts a background renewer. The
renewer periodically reloads the Attempt under the Unit of Work, verifies that it is still the
latest Attempt for the Run, verifies that it is still `RUNNING`, and extends
`lease_expires_at` by the configured lease duration.

Only the Worker that owns the original `worker_id` and `lease_token` can renew the Attempt.

## 3. Configuration

- `AGENTMESH_RUN_LEASE_SECONDS` controls the lease duration.
- `AGENTMESH_RUN_LEASE_RENEWAL_SECONDS` optionally controls the renewal cadence.
- When the renewal cadence is not set, the Worker renews approximately every third of the lease
  duration.

The minimal profile does not need extra configuration.

## 4. Failure behavior

| Situation | Behavior |
|---|---|
| Renewal succeeds | `heartbeat_at` and `lease_expires_at` are updated; the Attempt keeps ownership |
| Attempt is no longer latest | renewal stops; finalization is later rejected by fencing |
| Attempt is terminal | renewal stops; finalization follows existing terminal-state checks |
| Lease already expired | renewal stops; another Worker may reclaim with a higher fencing token |
| Temporary renewal error | Worker logs the error and retries on the next interval |
| Worker process dies | renewal stops naturally; the lease expires and Redis pending delivery can be reclaimed |

Finalization still checks that the Attempt is the latest, still `RUNNING`, and not expired. Renewal
extends the valid window; it does not weaken the fencing guard.

## 5. Verified acceptance criteria

- a running workflow can renew an Attempt lease while the node is still executing;
- renewal updates the persisted heartbeat/lease without external services in fast tests;
- stale, terminal, or expired Attempts cannot be renewed;
- existing pause/resume, duplicate delivery, idempotency, and finalization behavior remains
  compatible.
