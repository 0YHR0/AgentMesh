# Task Budget and Admission Control Implementation

Status: implemented baseline  
Feature Gate: `budget_admission` (depends on `observability`)  
Tracks: [Issue #37](https://github.com/0YHR0/AgentMesh/issues/37)

## Scope

This increment makes a Task budget a business-authoritative execution contract. It supports hard
limits for Run count, Attempt count, canonical Token usage, fixed-point cost in one declared
currency, and an overall UTC deadline. Token and cost limits require an explicit conservative
per-Attempt reservation so concurrent Workers cannot each assume the same remaining capacity.

The policy is immutable after Task creation. Task and Attempt records retain settled and reserved
amounts, settlement source, and a machine-readable exhaustion reason. Money uses integer micros;
floating-point values never enter the admission ledger.

## Lifecycle

```text
Task policy
  -> Run admission (deadline, Run count, remaining reservation capacity)
  -> Task row lock
  -> Attempt admission (deadline, Attempt count, settled + reserved + request)
  -> reserve on Task + snapshot on Attempt
  -> execute
  -> actual settlement, conservative settlement, or release
  -> continue | WAITING_APPROVAL
```

Actual provider usage uses the canonical `total` bucket. If a successful execution reports no
usage, AgentMesh settles the full reservation as `CONSERVATIVE_ESTIMATE`; it never assumes an
unobserved model call was free. Failure, cancellation, and lease expiry release the reservation.
A late actual result can restate a released Attempt and remains attributable to its original Trace.

An actual call can exceed its conservative reservation because external providers cannot be
atomically stopped at an exact Token boundary. AgentMesh records that actual usage and moves the
Task to `WAITING_APPROVAL`, preserving the candidate output. It stops creating further Runs and
Attempts and cancels coordinated siblings at their durable boundary.

## API

`POST /api/v1/tasks` accepts an optional `budget` object:

```json
{
  "max_runs": 4,
  "max_attempts": 6,
  "max_tokens": 20000,
  "token_reservation_per_attempt": 4000,
  "max_cost_micros": 5000000,
  "cost_reservation_micros_per_attempt": 1000000,
  "currency": "USD",
  "deadline": "2026-07-19T12:00:00Z"
}
```

`GET /api/v1/tasks/{task_id}/budget` returns policy, Run/Attempt counts, settled/reserved Token and
cost values, and the exhaustion reason. Normal Task responses also expose the policy and current
counters so operators do not need a telemetry backend to understand business state.

## Feature profile

`minimal` and `standard` remain unchanged. `full` enables `budget_admission` and its
`observability` dependency. A custom profile must enable both explicitly:

```text
AGENTMESH_FEATURE_GATES=observability=true,budget_admission=true
```

## Deferred

- platform/tenant/project hierarchical quota ledgers and weighted fair scheduling;
- provider price catalogs, model-specific estimation, tool-cost budgets and confidence metadata;
- operator commands to increase a budget and resume `WAITING_APPROVAL` work;
- remote A2A reservation coordination and distributed quota services;
- wall-clock interruption inside a provider call (the current deadline is checked at durable
  admission and settlement boundaries).

## Verification

Unit/API tests cover policy validation, actual overrun, conservative settlement, concurrent
reservation exclusion, deadline rejection, Feature Gate behavior and status projection. The
PostgreSQL suite verifies model round trips and migration constraints, while compose E2E protects
the default unbudgeted path.
