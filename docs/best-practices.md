# AgentMesh best practices

[简体中文](best-practices.zh-CN.md)

Status: supported single-team Alpha baseline  
Last updated: 2026-09-16

This guide is the operational companion to the [implementation status](implementation-status.md),
[roadmap](roadmap.md), and [v1 completion scope](v1-completion-scope.md). It describes the
recommended way to run the capabilities that are actually in this repository. It does not turn
the Alpha release into a production HA product: cross-tenant isolation, managed PostgreSQL HA/PITR,
managed-runtime durability, and chaos qualification remain future work.

## 1. Start with the smallest useful deployment

Use the deterministic provider first. It needs no model API key, makes no external request, and is
the fastest way to verify Task → Run → Attempt → Artifact/usage behavior.

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

Then check the public health endpoints and open the Console:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/api/v1/features
```

- Console: <http://localhost:8000>
- OpenAPI: <http://localhost:8000/docs>
- Relay metrics: <http://localhost:9464/metrics>

The checked-in Compose defaults use the `minimal` profile and, for the first-run Console demo,
override it with `coordinated_execution=true`. If a genuinely direct-only local environment is
desired, put the following in `.env` before starting Compose:

```dotenv
AGENTMESH_FEATURE_PROFILE=minimal
AGENTMESH_FEATURE_GATES=coordinated_execution=false
```

Keep this first pass free of real credentials. Once the deterministic path is healthy, enable one
capability at a time and confirm its effective state with `GET /api/v1/features`.

## 2. Select a feature profile deliberately

Profiles are convenience bundles, not security boundaries. Individual overrides are validated at
startup, dependencies are strict, and a configuration change requires a restart.

| Profile | Recommended use | Included optional capabilities |
| --- | --- | --- |
| `minimal` | First boot and smoke tests | Core Task execution only (Compose adds coordinated execution for its demo) |
| `standard` | Reviewed work and Agent administration | Reviewed execution, Agent Registry management, human resolution |
| `full` | Local evaluation of the broad single-team baseline | Standard plus coordinated DAG, Handoff, Deployment, Artifact, read-only MCP, observability, and budget |

Identity, persistent identity, Policy, governed MCP, A2A federation/delegation, Credential Broker,
Company modules, Office 2.5D, and all managed-runtime cutover gates are intentionally not enabled
by the built-in profiles. Add them explicitly and include every dependency. For example, a safe
local governance progression is:

```dotenv
AGENTMESH_FEATURE_PROFILE=standard
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true
```

The server rejects missing dependencies instead of silently running a partial policy. Treat a
`403` with `feature_disabled` as configuration feedback, not as a reason to bypass the API.

Do not enable any `managed_runtime_*_cutover` gate for an internet-facing or production service.
The A4.2d report at [qualification/a4-2-parity.json](qualification/a4-2-parity.json) is explicit
that coordinated managed failure, budget, cancellation, and unknown-outcome behavior has safety
differences from the legacy path. The managed cutover gates remain test/qualification-only until
the A4.3 durability and chaos work is accepted.

## 3. Model Agents as immutable, capability-qualified versions

An Agent Definition is the identity; an Agent Version is the immutable executable contract. Use a
new version for every instruction, model, tool-profile, or runtime-policy change. Publish it only
after review, and bind positions or tasks to the exact published version/digest. Do not edit a
published version in place or let a running Task resolve “latest”.

Recommended registration sequence when `agent_registry_management` is enabled:

1. Create the Agent Definition.
2. Create a version with role, capabilities, runtime adapter, model/limit policy, and explicit tool profile.
3. Submit and publish the version through the Agent Registry API.
4. Confirm capability matching and health/deployment state.
5. Bind the immutable version to a Task Subtask, Company Position, or Operation.

The Console can guide these steps. The API surface is also visible in OpenAPI and in
`src/agentmesh/api/agent_routes.py`. A published Agent Version is not the same thing as a worker
process: deployments and instances provide the operational health signal, while PostgreSQL
remains the authority for task execution.

The built-in deterministic Agent is a reference executor, not a quality benchmark. For a real
model, put the credential only in the Worker environment:

```dotenv
AGENTMESH_MODEL_PROVIDER=openai
AGENTMESH_MODEL_NAME=gpt-5.6-terra
AGENTMESH_MODEL_REASONING_EFFORT=low
OPENAI_API_KEY=replace-with-a-local-secret
```

Never commit `.env`, put a raw key in a Task, or expose the key to the Console/API. Compose passes
the model credential only to `worker` as `AGENTMESH_OPENAI_API_KEY`. To return to the no-key path,
remove the key and set `AGENTMESH_MODEL_PROVIDER=deterministic`.

For a non-LangGraph runtime, implement the public Runtime SDK/conformance boundary. The checked-in
`examples/reference-agent` is a minimal subprocess reference, and the
[AgentMesh Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter) demonstrates
the external extension boundary. Installed third-party extensions are trusted same-process code;
`extensions.lock` is an operator allowlist and audit check, not a sandbox.

## 4. Make task contracts explicit

State the objective, input shape, acceptance criteria, expected artifacts, deadline, and budget at
Task creation. Use Direct for work that is naturally one execution; use Reviewed when an
independent quality decision is needed; use Coordinated only when decomposition, parallelism, or
role separation has a measurable benefit.

Create and run a bounded Task with stable idempotency keys:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Summarize the project README","input":{"source":"README.md"}}'

curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs \
  -H "Idempotency-Key: readme-run-20260916-01"

curl http://localhost:8000/api/v1/tasks/<task-id>
```

Use a unique key per logical command and reuse that exact key on a network retry. Do not generate a
new key just because the first HTTP response was lost; the durable idempotency record is what
prevents duplicate Runs.

For a small parallel DAG, declare dependencies rather than asking Agents to coordinate through
unstructured chat:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Research and summarize","execution_mode":"COORDINATED","max_concurrency":2,"subtasks":[{"key":"research-a","objective":"Research source A"},{"key":"research-b","objective":"Research source B"},{"key":"synthesize","objective":"Compare the research","depends_on":["research-a","research-b"]}]}'
```

Inspect the returned `subtasks`, Runs, Attempts, Handoffs, and output lineage. A dependent
Subtask should consume a durable output or Artifact reference from its predecessors, not an
untracked message copied into a prompt.

## 5. Treat control commands as durable workflows

Pause, resume, and cancel through the API or Console, and retain the command's idempotency key in
your caller:

```bash
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/pause
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/resume
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/cancel \
  -H "Idempotency-Key: cancel-<task-id>-01"
```

A queued run can pause immediately; a running run pauses at a durable post-node boundary. Resume
creates a fenced Attempt and uses the checkpoint rather than replaying a completed node. A
Coordinated cancellation locks the aggregate, respects drain precedence, and may wait for
reconciliation evidence when dispatch has crossed the provider boundary. A provider that cannot
prove cancellation is not represented as successfully canceled merely because the local request
returned.

`OUTCOME_UNKNOWN`, `LOST`, `RECONCILIATION_REQUIRED`, and an A2A/MCP unknown result are safety
states. Never turn one into `FAILED` or blindly retry it. First inspect the persisted evidence and
use the appropriate privileged reconciliation command with same-tenant authorization,
`outcome:reconcile`, a reason/evidence digest, and an idempotency key. Late provider results are
retained as evidence and cannot overwrite a local canceled or already-converged business state.

## 6. Bound cost, time, and approvals before enabling side effects

Enable `observability` before `budget_admission`. Set hard Run/Attempt/token/cost limits and an
overall UTC deadline. Per-Attempt reservations are important when multiple Workers run in
parallel; they prevent each Worker from spending the same remaining budget.

```json
{
  "objective": "Bounded work",
  "budget": {
    "max_runs": 3,
    "max_attempts": 4,
    "max_tokens": 20000,
    "token_reservation_per_attempt": 4000,
    "max_cost_micros": 5000000,
    "cost_reservation_micros_per_attempt": 1000000,
    "currency": "USD"
  }
}
```

Inspect `GET /api/v1/tasks/<task-id>/budget`. Overruns and deadline expiry preserve the candidate
and move the Task to `WAITING_APPROVAL`; resolve it with a monotonic budget increase or a human
decision. Do not mutate budget rows directly.

For publication, spending, external writes, or other high-risk actions, enable Identity and Policy
together. Use independent approval roles, structured reasons, and one-time action-bound Permits.
An Agent author must not publish its own version. Keep external commercial writes disabled until
the full intent/permit/receipt/reconciliation path and an explicit business approval are ready.

## 7. Govern MCP and A2A as security boundaries

### MCP

Start with read-only tools. The governed registry requires `identity_rbac`, `policy_approval`,
`mcp_read_tools`, and `governed_mcp`; the model tool loop is a further explicit gate. Publish an
immutable Server/Tool snapshot, declare side-effect class and schema, allow the logical tool in the
Agent Version, and verify Catalog resolution before execution.

For writes, use only `IDEMPOTENT_WRITE` tools with a required string `idempotency_key`, an exact
approved ActionIntent, and a one-time Permit. `NON_IDEMPOTENT_WRITE` and `IRREVERSIBLE` actions
remain disabled in this baseline. If the response is lost, query/reconcile using the same operation
key; do not repeat an unknown external side effect.

Use the Credential Broker for authenticated providers. Store only a metadata-only SecretReference
in PostgreSQL and keep the actual value in the API process environment. The Broker issues a
short-lived workload lease; do not pass user bearer tokens or raw secrets through Agent state.

### A2A

Register a tenant-scoped Peer, discover its pinned HTTPS A2A Agent Card, inspect the candidate, and
activate the immutable snapshot explicitly. Federation, delegation, and background reconciliation
are separate gates. Use the `a2a-reconciler` Compose profile when automatic polling is enabled:

```bash
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true,a2a_reconciliation=true docker compose --profile a2a up -d
```

Use stable correlation/idempotency identities, bounded timeouts, and workload-bound credentials.
Remote cancel is best effort; remote late completion is kept as evidence and does not silently
rewrite a local terminal state. A discovered Agent Card is a candidate, not proof that every
declared Skill is trustworthy.

## 8. Use the right source of truth and observe it

PostgreSQL is the business source of truth for Task, Run, Attempt, Subtask, Agent binding, approval,
budget, Artifact metadata, Outbox/Inbox, MCP/A2A correlation, and reconciliation evidence. Redis
Streams is delivery infrastructure. Redis loss should trigger relay recovery and replay from
durable Outbox—not manual business-state edits.

The API, Relay, and Worker are separate processes. Keep at least one Relay and one Worker healthy,
watch the readiness endpoint, and monitor:

- API readiness and accepted-command latency;
- Outbox pending/quarantined rows and Relay publication lag;
- Redis consumer pending depth and dead-letter growth;
- Attempt lease expiry, worker capacity, and queue age;
- budget/quota rejection, MCP circuit state, and A2A unknown correlations;
- Artifact digest failures and reconciliation age.

Relay Prometheus metrics are exposed at `http://localhost:9464/metrics`. Enable `observability`
for usage and trace query APIs. Langfuse is optional; install `.[observability]` and configure:

```dotenv
AGENTMESH_FEATURE_GATES=observability=true
AGENTMESH_LANGFUSE_ENABLED=true
AGENTMESH_LANGFUSE_PUBLIC_KEY=pk-lf-...
AGENTMESH_LANGFUSE_SECRET_KEY=sk-lf-...
AGENTMESH_LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

The adapter mirrors privacy-safe Attempt/generation metadata, not Task objectives, prompts,
inputs/outputs, or Tool bodies. Langfuse failure must not block execution or accounting. Use the
durable Attempt Trace ID to correlate API, worker, and external observability records.

## 9. Operate PostgreSQL and Redis safely

For local or single-node evaluation, the bundled Compose file creates a PostgreSQL volume and binds
PostgreSQL/Redis to loopback. Do not expose the unauthenticated development API directly to the
internet. For a small remote host, use `compose.test.yaml`, a firewall, and an SSH tunnel:

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --build
ssh -L 8000:127.0.0.1:8000 user@test-host
```

For an actual production deployment, provide your own TLS/authentication edge, secret manager,
backups, resource limits, image-digest pinning, log rotation, and PostgreSQL HA/PITR design. These
are deployment responsibilities beyond this Alpha's bundled Compose certification.

Back up database metadata and content-addressed Artifacts together:

```bash
python scripts/operations/backup.py
python scripts/operations/restore.py backups/agentmesh-YYYYMMDDTHHMMSSZ --yes
python scripts/ci/compose_e2e.py
```

Redis is intentionally not in the business backup because it is transport state. Stop API/Worker/
Relay/Reconciler or use an isolated Compose project during restore. Verify `/ready`, Task→Run→
Attempt lineage, an Artifact digest/download, approvals, Outbox backlog, replay bookmarks, and a
new end-to-end Task after restore. See [slo-and-restore.md](operations/slo-and-restore.md).

## 10. Upgrade and rollback with compatibility evidence

Before a release, take a backup, run migrations in a disposable environment, and check for pending
model drift:

```bash
alembic upgrade head
alembic check
```

For a normal application release, roll the API/Relay/Worker together with the migration floor
documented by the release. Do not hand-edit migration tables or downgrade past a schema that has
stored newer evidence. For managed direct-runtime experiments, follow
[runtime-direct-cutover-rollback.md](operations/runtime-direct-cutover-rollback.md): stop new
admission, disable only the admission gate, preserve existing Run authority, and reconcile
crossed/unknown executions without falling back to the legacy worker or redispatching blindly.

## 11. Test the exact profile you intend to run

Run fast checks on every change:

```bash
ruff check .
pytest
```

With PostgreSQL and Redis available, run the real transport/persistence/checkpoint suite:

```bash
AGENTMESH_RUN_POSTGRES_TESTS=1 pytest -m postgres
```

On PowerShell:

```powershell
$env:AGENTMESH_RUN_POSTGRES_TESTS="1"
pytest -m postgres
```

For the Compose qualification path, run the checked-in script after the stack is ready:

```bash
python scripts/ci/compose_e2e.py
```

For changes to Runtime authority, review the machine-readable parity and activation fixtures,
targeted PostgreSQL atomicity tests, migration upgrade/downgrade matrix, and the GitHub CI
PostgreSQL/Compose/coverage/CodeQL checks. A green unit suite alone does not prove crash-window,
concurrency, or rollback safety.

## 12. Troubleshooting checklist

| Symptom | First checks | Safe response |
| --- | --- | --- |
| `503` or `/ready` is not ready | `docker compose ps`, `docker compose logs migrate api relay worker`, PostgreSQL/Redis health | Fix dependency/migration/configuration; do not write business rows manually |
| `403 feature_disabled` | `GET /api/v1/features`, profile and exact gate dependencies | Enable the required dependency chain, restart, and re-check effective state |
| Task remains queued | Relay metrics, Outbox pending rows, Worker health, Agent Version/deployment health | Repair delivery/worker capacity; preserve the Task and idempotency key |
| Run is `PAUSED`/`WAITING_APPROVAL` | Task detail, acceptance/budget/deadline evidence | Use the matching resume, resolution, or approval command; do not force status |
| Runtime/A2A/MCP outcome is unknown | Evidence, correlation, operation key, reconciliation age | Query/reconcile; never blind-retry an external side effect |
| Artifact cannot download | Stored SHA-256, scan state, artifact volume mount | Isolate possible corruption and restore/verify from backup |
| Langfuse is unavailable | `observability` gate and adapter logs | Continue with PostgreSQL trace/usage; fix Langfuse separately |

When in doubt, prefer an explicit durable `WAITING_APPROVAL` or reconciliation state over an
optimistic success/failure guess. That is the central reliability contract of AgentMesh.

## 13. Recommended adoption sequence

1. Run deterministic Direct Tasks with Compose and verify backup/restore.
2. Add `standard` and register immutable Agent Versions; introduce Reviewed execution.
3. Add `full` and a small Coordinated DAG with explicit acceptance criteria and bounded budgets.
4. Enable read-only MCP with Identity/Policy and a narrow tool allowlist.
5. Add A2A Peer discovery and background reconciliation only for a trusted test peer.
6. Enable Company/Memory/Finance/Pack gates only for a defined single-team workflow; keep recurring
   Operations in `DRAFT` until staffing, approval, and preflight checks pass.
7. Use the Office/Mission Map for operator visibility, but treat Control API and PostgreSQL as
   authoritative—not the visual simulation.
8. Propose and qualify production-runtime/chaos work before any managed cutover or external write.

This sequence keeps the first user experience small while preserving the durable contracts needed
for a future multi-agent company platform.
