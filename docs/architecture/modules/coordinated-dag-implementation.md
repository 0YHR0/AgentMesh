# Coordinated Subtask DAG execution

Status: Implemented baseline.

This increment adds a durable, bounded multi-Agent execution mode without changing the default
single-Agent experience. It implements the first local coordination slice from the formal Task,
Orchestrator, Registry, persistence, and Control API designs.

## Runtime contract

A client creates a Task with `execution_mode=COORDINATED`, an immutable plan of 2 to 20 Subtasks,
and `max_concurrency`. Each Subtask declares an objective, structured input, namespaced required
capabilities, optional preferred Agent, and predecessor keys. Creation rejects duplicate or missing
keys, self-dependencies, cycles, more than 100 edges, and concurrency outside platform limits.

The canonical plan is stored with version `1` and a SHA-256 digest. Materialized Subtasks and
dependency edges are independent durable records. The initial slice deliberately does not mutate
or dynamically replan that graph.

```text
Task + immutable plan
        |
        v
 READY Subtasks --capability match--> immutable Agent Version-bound Runs
        |                                  |
        +---- bounded parallel dispatch ---+
                                           v
 BLOCKED successors <-- committed predecessor outputs
        |
        v
 all Subtasks COMPLETED --> independent Supervisor Run --> Task result
```

## Scheduling and data flow

- Starting a coordinated Task preflights every Subtask against active definitions and published
  default Agent Versions before any Run is emitted.
- A preferred Agent is selected when supplied. Otherwise matching is deterministic by normalized
  Agent name and Version ID.
- A Version must support asynchronous execution and contain every required verified capability.
- Scheduling sorts READY Subtasks by key and never exceeds the Task's persisted concurrency bound.
- A successor becomes READY only after every predecessor is durably `COMPLETED`.
- The successor receives its own `subtask_input` plus structured `dependency_outputs` keyed by
  predecessor key; Agent-to-Agent state is therefore durable data flow, not an in-memory chat.
- Once every Subtask completes, exactly one independently persisted `SUPERVISOR` Run receives all
  outputs and synthesizes the final Task output.

Task locking serializes scheduling decisions in this bounded implementation. Run dispatch still
uses the existing transactional Outbox, Redis Streams relay, Inbox deduplication, fenced Attempts,
lease renewal, and LangGraph checkpoints.

## Failure and cancellation

A Subtask execution or finalization failure marks that Subtask and the parent Task failed, cancels
all non-terminal sibling Subtasks and Runs, and cancels active Attempts. A late worker result is
consumed safely and cannot resurrect terminal work. User cancellation applies the same propagation
rule. Capability loss after the initial preflight fails closed if a later Subtask is scheduled.

The Task, Subtasks, and dependency edges are inserted in one PostgreSQL transaction with explicit
foreign-key ordering. Run creation and its Outbox message are also atomic.

## Feature gate and limits

The `coordinated_execution` gate is off in `minimal` and `standard`, and on in `full`. Operators may
enable it independently. `AGENTMESH_COORDINATED_MAX_CONCURRENCY` defaults to `4` and caps the
per-Task request (the API and domain also impose a hard maximum of `10`). A built-in
`demo-supervisor` with `general.supervise` is seeded for the local runnable path.

Pause/resume, dynamic replanning, aggregate budgets, formal Handoff lifecycle, human intervention,
remote A2A peers, and rollout groups are intentionally outside this increment. A rollout group
runs multiple candidates for one work item; this DAG runs distinct dependent work items.

## Verification

The test suite covers graph validation, deterministic fork/join order, concurrency bounds,
capability mismatch rollback, failure/cancellation propagation, the disabled gate, API behavior,
and the real PostgreSQL + Redis + LangGraph checkpoint path. The Compose E2E smoke test executes
direct, independently reviewed, and coordinated Tasks.
