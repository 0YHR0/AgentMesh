# Contributing to AgentMesh

AgentMesh is currently documentation-first. Contributions should improve shared understanding before adding implementation.

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

## Implementation policy

Runtime code should not be added until the relevant L1 boundary and initial contracts have been accepted.
