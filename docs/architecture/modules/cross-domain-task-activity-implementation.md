# Cross-domain Task activity baseline

Status: Implemented baseline

## Outcome

Operators can inspect one chronological Task history across AgentMesh module boundaries. The Control
API owns normalization and authorization; the browser does not join module APIs or infer lifecycle
events from prose.

## Projection sources

`GET /api/v1/tasks/{task_id}/activity?limit=100` reads the authoritative tenant-scoped Task and its
durable related ledgers in one Unit of Work:

- Task, Run, Attempt and Subtask lifecycle timestamps;
- verified/applied Plan Patches and requested/decided Handoffs;
- governed MCP Tool invocation start/completion;
- Artifact Versions bound through `producer_run_id`;
- immutable human Task Resolutions; and
- durable outbound A2A RemoteTaskCorrelation state.

Each item has a stable `{category}:{entity_id}:{action}` identifier, category, action, status,
timestamp, entity reference, optional actor/trace ID, and a small allowlisted metadata map. Items are
sorted newest first with a deterministic ID tie-break and the response is capped at 200 entries.

## Security boundary

- The endpoint requires `activity_timeline` and `task:read`.
- The Task tenant is checked before any related ledger is projected; a foreign Task is indistinguishable
  from a missing Task.
- The projection excludes Task objective/input/output/error, Subtask input/output/error, Run output/error,
  Tool arguments/results/errors, Artifact content, Resolution reason/details, A2A endpoint/result/error,
  approval rationale, and all credential material.
- Artifact lookup is restricted to Versions whose producer Run belongs to the Task.
- The existing realtime event channel only invalidates the view; this authorized API remains the data plane.

## Consistency and failure behavior

The baseline is computed from authoritative ledgers and introduces no audit dual write, consumer, or
new database table. A missing optional ledger produces no entry. A source query failure fails the
whole projection rather than returning a misleading partial history. Redis availability does not
affect the API; broker loss only delays realtime invalidation and the Console safety poll recovers it.

## Verification

Unit tests cover newest-first normalization, Artifact-to-Run correlation, payload redaction, tenant
isolation, response limits, and feature gating. PostgreSQL integration verifies Task, Run, Attempt,
and Artifact evidence through the real API. Console asset tests and a full-profile browser smoke test
cover rendering and realtime refresh.

## Deferred

- cursor pagination and query-level bounds for very large single-Task ledgers;
- a persisted denormalized index for tenant-wide search, export, archival, and aggregate filters;
- exact Policy/Approval correlation for Task-bound governed actions;
- configurable category filters and deep links to every entity type.
