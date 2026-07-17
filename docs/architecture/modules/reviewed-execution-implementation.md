# Reviewed execution implementation

Status: Implemented baseline  
Last updated: 2026-07-17

This document records the first production vertical slice of the formal Reviewed Execution
design. It is intentionally narrower than the complete evaluation, policy, and approval modules.

## Supported contract

A Task chooses one immutable execution mode at creation:

- `DIRECT`: one Executor Run completes the Task, preserving the minimal baseline.
- `REVIEWED`: every candidate is evaluated by a separate Reviewer Run before completion.

Reviewed Tasks persist a bounded contract containing one to twenty acceptance criteria, a maximum
revision count, and an optional deadline. Criteria currently support deterministic JSON output
checks:

- `OUTPUT_PATH_EXISTS`
- `OUTPUT_PATH_EQUALS`

Each criterion records a stable key, description, JSON object path, required flag, and optional
expected value. The reviewer returns one result for every criterion. AgentMesh validates the keys,
recomputes acceptance from required results, and derives an integer quality score from `0` to
`10000` basis points instead of trusting an Agent-provided aggregate decision.

## Durable flow

```text
CREATED -> READY -> Executor Run -> RUNNING
  -> Reviewer Run queued -> REVIEWING
     -> accepted -> COMPLETED
     -> rejected and within bounds -> new Executor Run -> READY
     -> rejected at revision/deadline bound -> WAITING_APPROVAL
```

Every Executor and Reviewer invocation is a separate `TaskRun` and LangGraph thread. A Run stores
its `role` and `revision_number`; its immutable Agent version binding, fenced Attempts, checkpoint,
usage records, and Inbox/Outbox semantics are unchanged. PostgreSQL remains the source of truth,
while Redis Streams only wakes the next durable Run.

The Task retains the latest candidate and normalized review decision for monitoring. Successful
completion promotes the candidate to final output. Exhaustion never silently accepts a failed
candidate: it records `review_revision_limit_reached` or `review_deadline_exceeded` and moves to
`WAITING_APPROVAL`.

## Built-in roles

Registry seeding creates distinct built-in definitions:

- `AGENTMESH_AGENT_ID` for Executor Runs;
- `AGENTMESH_REVIEWER_AGENT_ID` for Reviewer Runs.

The credential-free baseline uses a deterministic executor and deterministic acceptance reviewer.
Future model-backed implementations can replace either executor behind the same `AgentExecutor`
port without changing Task or Run semantics.

## API example

Reviewed execution is enabled by the `reviewed_execution` feature. It is off in `minimal`, on in
`standard` and `full`, and can be explicitly overridden.

```json
{
  "objective": "Produce a reviewed summary",
  "execution_mode": "REVIEWED",
  "acceptance_criteria": [
    {
      "key": "summary",
      "description": "The result contains a summary",
      "kind": "OUTPUT_PATH_EXISTS",
      "path": ["summary"],
      "required": true
    }
  ],
  "max_revisions": 1
}
```

`AGENTMESH_REVIEW_MAX_REVISIONS` is the platform hard limit for a Task request. The default is
three. A client may choose a smaller value, including zero for review-without-automatic-rework.

## Consistency and recovery invariants

- Candidate persistence, completed Executor Run, next Reviewer Run, and next Outbox message share
  one transaction.
- Review persistence, revision decision, optional next Executor Run, and next Outbox message share
  one transaction.
- A duplicate wakeup is suppressed by the existing tenant-scoped Inbox record.
- An expired Attempt is fenced before another worker can finalize the same Run.
- Reviewer output with missing, duplicate, or unknown criterion keys fails closed.
- Direct Tasks cannot carry review policy, and Reviewed Tasks cannot omit acceptance criteria.

## Deferred scope

This increment does not implement human approval resolution APIs, model/judge ensembles, semantic
rubrics, budget accounting across providers, or asynchronous evaluation. `WAITING_APPROVAL` is the
durable escalation boundary for the future Policy and Approval module. Coordinated Subtask DAGs,
Handoffs, and rollout groups remain the next orchestration increment.
