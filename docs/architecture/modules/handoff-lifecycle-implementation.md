# Durable coordinated Handoff lifecycle

Status: Implemented baseline.

This increment adds an explicit, durable responsibility-transfer contract between existing
Subtasks in an immutable coordinated DAG. It replaces informal chat-only transfer with a queryable
business record and deterministic scheduling behavior.

## Contract and lifecycle

A Handoff records its source Subtask, successful source Run, Trace and Agent, an independent
causation ID, downstream target Subtask and proposed target Agent, objective, reason,
completed-work summary, unresolved questions, constraints, acceptance criteria, actors,
timestamps, and optimistic version.

```text
REQUESTED ── accept ──> ACCEPTED
    └────── reject ───> REJECTED
```

Both terminal decisions are idempotent only when the actor and decision payload match. Conflicting
replays fail with a stable conflict. Command-level `Idempotency-Key` records are persisted in the
same transaction as state, Outbox audit event, and result reference.

## Safety boundaries

- Handoffs are valid only for a running `COORDINATED` Task.
- The source Subtask and its current Run must already have completed successfully.
- The target must be a downstream DAG Subtask in `BLOCKED` or unassigned `READY` state.
- The source and target Subtasks and Agents must be distinct.
- The target Agent must have an active published default Version, asynchronous execution mode, and
  every capability required by the target Subtask.
- A target can have at most one accepted Handoff; a Task can contain at most eight Handoffs in this
  bounded slice.
- Accepting a Handoff does not modify the immutable PlanVersion or grant source Agent permissions.

The request actor must match the immutable source Run Agent and the decision actor must match the
target Agent. These checks establish the domain invariant, but they are not authentication; trusted
Principal resolution and authorization remain part of the Identity/Policy modules.

## Scheduling and context

Only an accepted Handoff affects scheduling. When the target becomes READY, its Run is bound to the
accepted target Agent and immutable Version. The target receives a schema-safe
`accepted_handoffs` list containing the structured contract, alongside normal Subtask input and
dependency outputs. Rejected Handoffs remain audit history and do not influence routing or prompts.

Request, accept, and reject emit versioned `agentmesh.handoff.*` Outbox events. Handoff records are
included in Task detail/list projections without per-Task query growth.

## Feature gate and current limits

The `handoffs` feature depends on `coordinated_execution`. It is enabled by the `full` profile and
disabled by `minimal` and `standard`, so existing first-run behavior stays unchanged.

This slice intentionally does not support dynamic Subtask creation, PlanVersion replacement,
clarification, ArtifactRefs, budget/deadline transfer, Handoff of already-running target work,
identity-backed authorization, approval, or remote A2A delegation. Those require their owning L2
contracts instead of overloading this lifecycle.

## Verification

Tests cover request/decision idempotency, invalid actors and graph relationships, rejection
isolation, accepted Agent binding, structured context injection, disabled gates, API projection,
bounded list queries, Alembic upgrade/downgrade, and the real PostgreSQL + Redis + LangGraph path.
