# Goal Contract and verified Plan Patch implementation

Status: Implemented baseline, including quiescent running-Task replacement

This increment establishes the first safe dynamic-replanning boundary for coordinated Tasks. It
does not let a planner freely rewrite a live graph. A Task receives an immutable Goal Contract,
and every proposed replacement is a durable, versioned Plan Patch with deterministic verifier
evidence.

## Runtime contracts

`GoalContract` binds a Task objective, bounded constraints, and bounded success criteria to a
canonical SHA-256 digest. The contract is created in the same transaction as a coordinated Task
and is not mutated by Plan Patches.

`PlanPatch` contains:

- the Goal Contract digest;
- the exact base plan version and digest;
- the next plan version, digest, and full bounded plan snapshot;
- the operator identity and reason;
- immutable verifier findings; and
- a `VERIFIED` or `APPLIED` lifecycle.

The verifier proves that the Goal binding is current, the base snapshot is current, the version
advances exactly once, the plan changes semantically, the proposed DAG passes all existing bounds,
and no execution history can be rewritten. For a running Task, the verifier also records the
number of terminal Runs, preserved completed Subtasks, remaining-node delta, concurrency bound,
and absence of external side effects.

## Safe application boundary

Plan Patches apply at either of two explicit boundaries:

- before execution while the coordinated Task is `CREATED`; or
- after execution has started, only while the Task is quiescent at a budget-induced
  `WAITING_APPROVAL` barrier.

The quiescent boundary requires every Run to be terminal, every historical Subtask to be completed,
and every Attempt to be terminal. It rejects Handoffs, A2A correlations, write-class MCP history,
and running or outcome-unknown Tool invocations. A candidate must preserve each completed
Subtask's exact specification and durable identity/output, cannot add remaining nodes, and cannot
increase maximum concurrency. Only unstarted Subtasks and their dependencies are replaced.

Apply locks and rechecks the Task, Goal Contract, Plan Patch, current graph, and history in one
transaction; advances the Task plan version; emits `agentmesh.task.plan-patch-applied`; and marks
the patch `APPLIED` atomically. Repeating an already successful apply is idempotent.

Active-Run replanning remains intentionally excluded. That wider boundary needs cancellation and
compensation semantics, explicit supersession state, and irreversible-side-effect convergence.

## API

- `GET /api/v1/tasks/{task_id}/planning` returns the immutable Goal and Plan Patch audit history.
- `POST /api/v1/tasks/{task_id}/plan-patches` verifies and persists a proposal.
- `POST /api/v1/tasks/{task_id}/plan-patches/{patch_id}/apply` atomically applies a verified patch.

The API and Console workflow are controlled by `dynamic_replanning`, which depends on
`coordinated_execution`. It is off in the minimal profile and can be enabled explicitly without
increasing the default demo surface. The Console exposes the current version, editable candidate
JSON, verifier evidence, and a separate apply action.

## Persistence and recovery

Migration `20260721_0030` adds one Goal Contract per Task and an append-only Plan Patch ledger.
The proposed plan snapshot is self-contained and digest-verified again when loaded for application.
If the transaction fails, neither the Task plan nor patch lifecycle advances.

## Verification

The test suite covers canonical Goal digests, contract bounds, semantic no-op rejection, stale base
rejection, unsafe history/side-effect rejection, completed-node identity and output preservation,
atomic remaining-graph replacement, idempotent apply, Feature Gate behavior, Console assets, API
projections, and real PostgreSQL persistence round trips for both safe boundaries.
