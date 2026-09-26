# Next release: guided tasks, model connections, and employee memory

Status: accepted for implementation. Scope agreed on 2026-09-27.

## Product outcome

A new user opens the original Console, understands the distinction between a free demonstration
and real model execution, configures a model connection, creates employees, and runs a task with
visible responsibilities, dependencies, results, and optional memory. English remains the default;
Chinese is available throughout the flow.

The game-style Office surfaces are retired. The task dependency view remains useful execution
evidence. Company, employee, position, and memory business records remain available without a map.

## Acceptance packages

| Package | Completion evidence | Initial status |
|---|---|---|
| A: focused Console | Office assets removed, old URLs redirect, guided task form works with real API, bilingual and responsive browser checks | In progress |
| B: model connections | Authenticated write-only credentials, encrypted persistence, provider test, OpenAI/DeepSeek worker execution, Agent Version binding, failure tests | In progress |
| C: employee memory | Explicit built-in memory setup, scoped task context, candidate review, second-task recall, revoked memory excluded | In progress |
| D: integrated qualification | Real API/browser flow, deterministic provider fixtures, migration/integration tests, CI green; actual external calls identified separately | Pending |
| E: delivery | README/user guides reflect actual UI, progress tracked, small commits pushed, reviewed PR merged and deployment health verified | Pending |

These packages describe acceptance, not percentage estimates. A saved form or a unit test alone
does not establish end-to-end readiness.

## Guided task contract

1. Describe the objective, source material, expected output, and success conditions in user language.
2. Choose one employee, independent review, or a team only when the enabled execution mode supports it.
3. For a team, select published employees, describe each deliverable, and choose preceding steps by
   name. Detect missing employees, invalid dependencies, and cycles before submission.
4. Optional controls expose concurrency, bounded execution budgets, deadlines, and memory scope.
5. Review the plan and explicitly choose create or create-and-run. Failures preserve entered data.

The form sends supported Task contract fields. A setting shown as effective must reach the actual
Worker execution. Deployment-default employee selection is labeled as such until explicit direct
selection is supported. Advanced features explain their purpose and setup state without claiming
that a feature flag proves a working external integration.

## Model and credential contract

Connections belong to the configured tenant and have an explicit protocol, model, endpoint, and
credential. Existing deterministic and environment-based OpenAI execution keep working. New
connections support OpenAI Responses and DeepSeek Chat Completions through provider adapters.

Key values are write-only. Stored values use authenticated encryption with a server-side master
key outside the database. Responses and errors contain safe metadata, never credentials or raw
provider bodies. Authenticated administrative access is required to manage or test connections;
an anonymous development principal cannot authorize credential storage. Tests use explicit calls,
bounded timeouts, and safe endpoint validation. Existing Runs retain their selected execution
configuration when an administrator changes a connection.

The UI offers named connections, supported model options, status, and explicit connection testing.
A successful save is distinct from successful provider authentication and inference. A real paid
provider smoke test requires an operator-supplied credential and must be reported separately from
fixture qualification.

## Memory contract

The first usable path is the existing PostgreSQL memory backend, with no separate service or key.
Users explicitly set up memory for a company, select a policy, and opt a task into that scope.
Approved memory may be recalled within authorized namespaces. New learning is a candidate until
accepted under policy, and revocation immediately excludes it from future recall. Reviewers keep
their independence as defined by the existing runtime contract.

The UI explains what was recalled and supports candidate review and manual knowledge entry. Mem0,
MemOS, vector retrieval, and their credentials remain optional later adapters tracked in the
[external memory proposal](external-memory-adapters.md).

## Verification and rollout

- Unit tests cover protocol conversion, secret redaction, invalid connections, and memory scope.
- Persistence tests cover migrations, encrypted storage, revisions, and restart persistence.
- API tests cover auth, feature-disabled responses, setup, task creation, and version binding.
- Browser checks cover English/Chinese, task mode changes, team dependencies, advanced fields,
  connection errors, memory setup, and real API submission on desktop and narrow screens.
- Deploy only after integration review, retaining a rollback path and existing database records.
- Production HA/chaos work stays in issue #160; authentication and encrypted credential handling
  are prerequisites of this release's API-key workflow.
