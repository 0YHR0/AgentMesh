# CI and pull request governance

Status: Implemented baseline.

## Purpose

This module makes changes to AgentMesh reproducibly reviewable before they reach `main`. The
baseline uses only services that are free for a public GitHub repository and commands that a
contributor can run locally. It deliberately does not depend on Copilot code review, GitHub Code
Quality, Codecov, or another paid SaaS product.

## Responsibilities

- Run independent quality, coverage, PostgreSQL integration, dependency, Compose E2E, and CodeQL
  checks for each pull request.
- Reject coverage below the accepted baseline.
- Prove database migrations can move forward, back one revision, and forward again.
- Prove the packaged services can complete a real asynchronous Task through Docker Compose.
- Scan newly introduced dependencies and Python source for high-impact security problems.
- Keep dependency and GitHub Action updates visible through bounded Dependabot pull requests.
- Give maintainers a consistent PR description and review checklist.
- Prevent direct, destructive, or unchecked changes to the default branch through a GitHub
  Ruleset.

## Non-responsibilities

- CI does not prove production capacity, availability, or model quality.
- CodeQL and dependency review do not replace threat modeling or human review.
- The initial ownership rule requests the primary maintainer but does not require a second human
  approval while the project has only one active maintainer.
- CI does not receive deployment credentials and does not deploy artifacts.

## Required checks

| Check | Contract | Local equivalent |
|---|---|---|
| `quality` | Ruff and Python bytecode compilation pass | `ruff check .` and `python -m compileall -q src tests` |
| `unit-coverage` | Fast tests pass and total package coverage is at least 80% | `pytest -m "not postgres" --cov=agentmesh --cov-fail-under=80` |
| `postgres-integration` | Migrations and real PostgreSQL/Redis integration pass | Start PostgreSQL/Redis, migrate, then run `AGENTMESH_RUN_POSTGRES_TESTS=1 pytest -m postgres` |
| `dependency-review` | A PR introduces no dependency rated high severity or worse | Review the dependency diff and advisories |
| `compose-e2e` | The built Compose stack accepts and completes a Task and exposes usage | `scripts/ci/compose-e2e.sh` |
| `codeql` | GitHub's Python CodeQL analysis succeeds | Review CodeQL results in GitHub Security |

The coverage XML report and failed Compose logs are retained for seven days. Superseded workflow
runs are canceled to reduce queue noise and feedback time.

## State and ownership

- Workflow definitions, scripts, PR templates, and dependency policy are versioned in this
  repository.
- GitHub owns workflow execution records, CodeQL results, dependency alerts, and Ruleset state.
- PostgreSQL and Redis started by CI are ephemeral and contain test-only data.
- `CODEOWNERS` currently assigns the repository to `@0YHR0`; ownership should be split by module
  when more maintainers are active.

## Failure behavior

- A failing required check blocks merging but does not mutate production state.
- Compose logs are captured only on failure, then all Compose services and volumes are removed.
- A missing coverage report is reported by the artifact step; the test command remains the source
  of the coverage decision.
- Python version-update PRs stay disabled until the project has a lock file; grouped security
  updates remain enabled. GitHub Actions allows one grouped minor/patch PR, while Docker allows one
  grouped patch PR. Runtime-minor and major changes remain explicit human decisions.

## Security boundaries

- Workflows use read-only repository contents permission by default.
- CodeQL alone receives `security-events: write` so it can publish analysis.
- CI uses GitHub-hosted runners; the project does not expose a maintainer-owned self-hosted runner
  to untrusted public pull requests.
- Workflows use `pull_request`, not privileged `pull_request_target`, when executing contributed
  code.
- No repository secrets are required by the test path.

## Evolution rules

- Raise the coverage floor gradually after coverage grows; never lower it merely to merge a PR.
- Require at least one approving review only after another active maintainer can satisfy it.
- Add a paid service only through an explicit ADR that demonstrates a gap in the free baseline.
- Keep required-check names stable. Renaming a check requires an atomic Ruleset update so pull
  requests are not blocked by a context that can no longer run.
