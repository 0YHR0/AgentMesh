# Goal Contract and verified Plan Patch implementation

Status: Implemented baseline

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
and no execution history can be rewritten.

## Safe application boundary

The first slice applies Plan Patches only while a coordinated Task is still `CREATED`. The apply
transaction locks the Task, Goal Contract, and Plan Patch; rechecks all base digests; rejects any
Run or Handoff history; replaces Subtasks and dependencies; advances the Task plan version; and
marks the patch `APPLIED` atomically. Repeating an already successful apply is idempotent.

Running-task replanning is intentionally not included. A later increment must introduce explicit
Subtask supersession, cancellation convergence, budget redistribution, and irreversible-side-effect
guards before that boundary can be widened.

## API

- `GET /api/v1/tasks/{task_id}/planning` returns the immutable Goal and Plan Patch audit history.
- `POST /api/v1/tasks/{task_id}/plan-patches` verifies and persists a proposal.
- `POST /api/v1/tasks/{task_id}/plan-patches/{patch_id}/apply` atomically applies a verified patch.

The API is controlled by `dynamic_replanning`, which depends on `coordinated_execution`. It is off
in the minimal profile and can be enabled explicitly without increasing the default demo surface.

## Persistence and recovery

Migration `20260721_0030` adds one Goal Contract per Task and an append-only Plan Patch ledger.
The proposed plan snapshot is self-contained and digest-verified again when loaded for application.
If the transaction fails, neither the Task plan nor patch lifecycle advances.

## Verification

The test suite covers canonical Goal digests, contract bounds, semantic no-op rejection, stale base
rejection, execution-history rejection, atomic replacement, idempotent apply, Feature Gate behavior,
API projections, and a real PostgreSQL migration/persistence round trip.
