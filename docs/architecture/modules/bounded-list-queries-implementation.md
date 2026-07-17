# Bounded list query implementation

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Durable asynchronous execution](durable-async-execution.md),
[Artifact Service implementation](artifact-service-implementation.md)

## 1. Scope

This increment removes N+1 child collection loading from the current Task and Artifact list
endpoints. It keeps the existing response shapes and pagination model, but bounds the number of
child queries per list page.

## 2. Implemented contract

Task list pages now execute:

- one query for the Task page;
- one query for all Runs owned by Tasks in that page;
- one query for all Attempts owned by Runs in that page.

Artifact list pages now execute:

- one query for the Artifact page;
- one query for all Artifact Versions owned by Artifacts in that page.

The application service groups child records in memory and then attaches them back to the parent
aggregates. Parent ordering remains controlled by the parent page query. Child ordering remains the
same as the detail endpoints: Runs by `queued_at`, Attempts by `started_at`, and Artifact Versions
by `version_number`.

## 3. Boundaries

This does not change offset pagination, response schemas, tenant scoping, or authorization. It also
does not introduce projection-specific read models yet; those remain future Web Console and
operations API work.

## 4. Verified acceptance criteria

- empty pages avoid child collection queries;
- parents without children return empty child lists;
- parents with multiple child records preserve child ordering;
- Task list child loading is bounded to two child queries per page;
- Artifact list child loading is bounded to one child query per page;
- PostgreSQL integration tests assert fixed `SELECT` counts for the optimized list paths.
