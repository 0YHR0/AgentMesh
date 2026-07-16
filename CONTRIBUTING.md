# Contributing to AgentMesh

AgentMesh uses a documentation-led architecture process. Runtime contributions should improve the accepted vertical slice while keeping target module boundaries explicit.

## Before proposing a change

1. Read the [architecture map](docs/architecture/README.md).
2. Confirm which architecture level the change belongs to.
3. For a cross-cutting or hard-to-reverse decision, add an ADR.
4. Keep unresolved questions explicit instead of hiding assumptions in prose.

## Design contribution rules

- L0 documents describe system intent and boundaries, not classes or endpoints.
- L1 documents define deployable containers, ownership, data flow and trust boundaries.
- L2 documents define the internal components and contracts of one L1 container.
- L3 documents cover schemas, APIs, algorithms and implementation details.
- Every module design must define responsibilities, non-responsibilities, dependencies, state ownership, failure modes and observability.
- New protocols or infrastructure components require a concrete problem statement.

Use the [module design template](docs/templates/module-design-template.md) for detailed module proposals.

## Commits and pull requests

- Keep commits scoped to one coherent design or implementation change.
- Explain why a change is needed and which decision it affects.
- Link the relevant architecture document or ADR.
- Do not commit credentials, production data or generated runtime artifacts.
- Complete the pull request template and call out migrations, feature gates, and rollback risks.
- All required GitHub checks must pass before merge; do not bypass a failed check by weakening its
  threshold in the same change.

Run the same free quality and coverage gates locally before opening a pull request:

```bash
ruff check .
python -m compileall -q src tests
pytest -m "not postgres" --cov=agentmesh --cov-fail-under=80
```

The real PostgreSQL/Redis integration and Compose E2E commands are documented in
[CI and pull request governance](docs/architecture/modules/ci-and-pr-governance.md). GitHub-hosted
CI is authoritative because these checks depend on clean infrastructure.

## Implementation policy

Runtime changes must reference the relevant L2 design or explicitly state that they are a bounded bootstrap experiment. New cross-module contracts should be proposed and reviewed before implementation.
