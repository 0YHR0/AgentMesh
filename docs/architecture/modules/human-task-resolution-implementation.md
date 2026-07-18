# Human Task resolution implementation

Status: Implemented baseline

Feature Gate: `human_resolution`

Budget resume additionally requires: `budget_admission`

## Responsibility

This increment gives an operator deterministic commands for a Task at the
`WAITING_APPROVAL` boundary. It closes the operational loop created by reviewed execution and
Task budget admission without claiming to implement the formal Policy and Approval module.

Supported resolutions are:

- accept the Task's persisted candidate output;
- reject the Task and move it to `FAILED`;
- replace an exhausted Task budget with a monotonic increase and resume from durable state.

Every successful command creates an immutable `TaskResolution` audit record in the same
transaction as the Task transition, any replacement Run, and its Outbox messages. Optional
`Idempotency-Key` handling makes command retries safe.

## State and recovery rules

Only a Task in `WAITING_APPROVAL` can be resolved. Accept requires a persisted candidate and never
re-executes it. Reject preserves the previous failure reason in the audit record.

A replacement budget keeps currency and reservation policy stable, cannot lower or newly constrain
any limit, and must increase at least one limit or deadline. `budget_revision` advances only when
the replacement is committed.

Resume is derived from durable execution history:

- direct execution accepts an already-produced over-budget candidate, otherwise queues a new Run;
- reviewed execution resumes the missing Executor/Reviewer/revision step, or evaluates a completed
  Reviewer result without repeating it;
- coordinated execution keeps completed Subtasks immutable, reopens only budget-canceled Subtasks,
  and lets the existing scheduler continue the DAG and Supervisor join.

Historical canceled Runs, Attempts, and completed Subtasks are never revived or rewritten.

## API

```text
GET  /api/v1/tasks/{task_id}/resolutions
POST /api/v1/tasks/{task_id}/resolutions/accept-candidate
POST /api/v1/tasks/{task_id}/resolutions/reject
POST /api/v1/tasks/{task_id}/resolutions/increase-budget-and-resume
```

Mutation bodies require a non-empty `actor` and `reason`. The budget command also carries the full
replacement budget contract. Until Identity is implemented, `actor` is operator-supplied audit
metadata and is not an authenticated Principal.

## Persistence and events

Migration `20260718_0015` adds `tasks.budget_revision` and the append-only
`task_resolutions` ledger. A successful command emits `agentmesh.task.resolved`; a resumed Run also
emits the normal `agentmesh.run.requested` message. Both use the existing transactional Outbox.

## Deliberate boundary

This baseline does not implement authentication/RBAC, Policy decisions, `ActionIntent`, quorum or
self-approval rules, approval expiry, delegated authority, or the Web Console approval queue. Those
remain owned by the formal Identity and Policy/Approval modules. High-risk Tool approval must not
reuse the operator-supplied `actor` field as an authorization mechanism.
