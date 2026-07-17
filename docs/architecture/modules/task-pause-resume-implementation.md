# Durable Task pause and resume implementation

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Durable asynchronous execution](durable-async-execution.md),
[Formal Task and execution domain](formal/task-and-execution-domain.md)

## 1. Scope

This increment adds persistent pause and resume semantics to the existing single-Agent Run. Pause
is a business command, not a process-local flag: Task and Run status, timestamps, Outbox events,
Inbox consumption, Attempt outcome, and the LangGraph thread all participate in recovery.

The same Run and `thread_id` are retained across resume. A resumed execution creates a new Attempt
with a higher fencing token. This is an operational continuation, not a business retry or a new
Run.

## 2. State model

```text
Task: READY   -> PAUSED -> READY
Task: RUNNING -> PAUSE_REQUESTED -> PAUSED -> READY -> RUNNING

Run:  QUEUED  -> PAUSED -> QUEUED
Run:  RUNNING -> PAUSE_REQUESTED -> PAUSED -> QUEUED -> RUNNING

Attempt: RUNNING -> PAUSED
Attempt: RUNNING -> LEASE_EXPIRED (crash recovery while pause is requested)
```

`TaskRun` records `pause_requested_at`, `paused_at`, `resumed_at`, and
`paused_from_status`. Resume always re-enters the schedulable `READY/QUEUED` boundary so current
Agent availability, policy, lease, and cancellation state can be checked again.

Repeated pause while `PAUSE_REQUESTED/PAUSED` and repeated resume after the same pause cycle are
state-idempotent and do not create duplicate wakeups or lifecycle events.

## 3. Queue and execution behavior

### Queued Run

Pausing `READY/QUEUED` changes both entities directly to `PAUSED`. An already published or pending
RunRequested message is safe: the Worker records it in Inbox and acknowledges it without leasing
an Attempt. Resume writes a fresh RunRequested message in the same transaction as the
`READY/QUEUED` transition.

### Running Run

Pausing `RUNNING` first persists `PAUSE_REQUESTED`. The current node is cooperative and is not
terminated in the middle of model, tool, or arbitrary Python execution. When the workflow reaches
the post-node checkpoint/finalization boundary, the Worker:

1. keeps the candidate output in the durable LangGraph checkpoint;
2. changes Task/Run to `PAUSED`;
3. changes the current Attempt to `PAUSED`;
4. records Inbox and emits `agentmesh.task.paused` atomically.

On resume, LangGraph reads the completed checkpoint output, so the finished Agent node is not
executed again. A new Attempt then publishes the checkpointed result into the PostgreSQL business
ledger.

## 4. Crash, lease, and race behavior

| Situation | Convergence |
|---|---|
| Worker crashes after pause request | stale Redis delivery is reclaimed after lease expiry, old Attempt becomes `LEASE_EXPIRED`, Task/Run become `PAUSED` |
| old Worker returns after expiry | finalization is rejected by Attempt status/lease fencing and cannot overwrite `PAUSED` |
| cancel races with pause | row locks serialize commands; cancel is terminal and late execution output is ignored |
| pause races with successful finalization | whichever locks first wins; accepted pause stops at the checkpoint boundary, otherwise terminal completion rejects the later pause |
| resume response is lost | repeating resume after the same pause cycle returns current state without another RunRequested message |
| old queued wakeup arrives after pause | Inbox records and acknowledges it without starting work |

Workflow failure after an accepted running pause request remains a real terminal failure; pause
does not hide execution errors.

## 5. HTTP and events

- `POST /api/v1/tasks/{task_id}/pause` returns `202 Accepted` with current Task state.
- `POST /api/v1/tasks/{task_id}/resume` returns `202 Accepted` and queues durable work.
- both responses include `Location: /api/v1/tasks/{task_id}`.
- Task queries expose the pause/resume timestamps and `paused_from_status` on the Run.

Lifecycle events are `agentmesh.task.pause-requested`, `agentmesh.task.paused`, and
`agentmesh.task.resumed`. Resume also writes a normal `agentmesh.run.requested` command routed to
the execution stream.

## 6. Current boundary

- Pause occurs before execution or at the current workflow node's durable completion boundary; it
  does not preempt an in-flight model/tool call.
- The current Direct graph has one Agent node. Future multi-node templates must expose safe
  boundaries and cooperative cancellation handles per Runtime contract.
- Attempt lease renewal keeps an in-flight node owned while the Worker remains alive. Results
  returned after an actually expired lease are still rejected by fencing.
- Approval/input interrupts, deadlines, explicit resume guards, operator reasons, and dynamic
  workflow migration remain later Orchestrator/Policy increments.
- Pause/resume is part of the reliable core and is not Feature-Gated.

## 7. Verified acceptance criteria

- queued pause consumes old wakeups without creating an Attempt;
- running pause keeps output in checkpoint and resumes without re-executing the Agent;
- resume retains Run/thread identity and creates a higher fencing-token Attempt;
- expired lease recovery converges to `PAUSED` and rejects late Worker output;
- PostgreSQL/Redis integration persists timestamps, Inbox records, and completes after resume;
- cancel, terminal transitions, tenant isolation, and existing execution remain compatible.
