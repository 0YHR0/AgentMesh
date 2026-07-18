# AgentMesh

[![CI](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml)

AgentMesh is an open-source control plane for coordinating, observing, and governing teams of AI agents.

AgentMesh（协作式智能体平台）旨在让使用者只需要定义目标、约束和验收标准，平台负责规划、分派、流转、观察、介入与审计 Agent 的执行过程。

> Status: pre-alpha. The repository contains a formal L2 architecture baseline and a
> durable asynchronous direct and independently reviewed execution slices.

## Vision

AgentMesh 希望成为一个自主可控、框架中立的多 Agent 平台：

- 简单任务由单 Agent 直接完成，避免不必要的协作成本。
- 复杂任务可以拆解、并行、复核、返工和人工审批。
- Agent 可以拥有不同角色、模型、工具、知识、权限与资源配额。
- 本地 Agent 与远程 Agent 使用一致的任务和产物语义。
- 所有状态变化、调用、费用、质量评价和人工操作均可观察、可追溯。
- 平台优先采用开放协议，并支持私有化部署。

## Proposed stack

- Orchestration: LangGraph
- System of record: PostgreSQL
- Agent interoperability: A2A
- Tool and context interoperability: MCP
- LLM observability and evaluation: Langfuse
- Event delivery: Redis Streams initially, with an abstraction for NATS JetStream
- Artifact storage: S3-compatible object storage

技术选型是当前设计基线，不是不可变的产品边界。重要决策会通过 ADR 记录。

## Architecture documentation

- [Documentation map](docs/README.md)
- [Architecture levels](docs/architecture/README.md)
- [L0 system design](docs/architecture/L0-system-design.md)
- [L1 design plan](docs/architecture/L1-design-plan.md)
- [Formal L2 design baseline](docs/architecture/modules/formal/README.md)
- [Implementation status](docs/implementation-status.md)
- [Roadmap](docs/roadmap.md)
- [Glossary](docs/glossary.md)
- [Architecture decisions](docs/adr/README.md)
- [CI and pull request governance](docs/architecture/modules/ci-and-pr-governance.md)

## Runnable asynchronous slice

The current implementation proves this path:

```text
HTTP task command (202 Accepted)
  -> Task + Run + Transactional Outbox in PostgreSQL
  -> Event Relay -> Redis Streams consumer group
  -> Execution Worker + Attempt lease/fencing token
  -> LangGraph workflow + optional allowlisted read-only MCP Tool
  -> PostgreSQL checkpoint
  -> Inbox deduplication + persisted business result and usage ledger
  -> optional privacy-safe Langfuse Attempt Trace
```

The API, Event Relay, and Worker are separate processes. Redis is delivery infrastructure,
while PostgreSQL remains the business source of truth. The deterministic executor
intentionally requires no model API key.

The Relay also performs bounded Outbox/Inbox cleanup and pending-safe Redis Stream retention.
Compose exposes its Prometheus metrics at `http://localhost:9464/metrics`. The default Inbox
deduplication horizon is 30 days; retention is part of the reliable core and is not a feature
gate. See [Messaging retention and cleanup](docs/architecture/modules/messaging-retention-implementation.md)
for safety guarantees and tuning.

### Feature profiles

AgentMesh defaults to the `minimal` profile so a first-time user only needs the Task API and
the built-in deterministic Agent. Optional management APIs are enabled explicitly:

| Profile | Enabled optional capabilities |
|---|---|
| `minimal` | None; core task execution remains available |
| `standard` | Reviewed execution, Agent Registry management, and human Task resolution |
| `full` | Standard plus coordinated DAG/Handoffs, Deployments, inline-small Artifacts, read-only MCP, observability, and Task budgets; identity remains explicit opt-in |

Choose a profile in `.env` before starting Compose:

```dotenv
AGENTMESH_FEATURE_PROFILE=standard
```

Individual gates can override the profile:

```dotenv
AGENTMESH_FEATURE_GATES=reviewed_execution=true,coordinated_execution=true,handoffs=true,agent_registry_management=true,artifact_service=true,mcp_read_tools=true,observability=true,budget_admission=true,human_resolution=true
```

Configuration is validated at startup and changes require a restart. Dependencies are strict:
`agent_deployments` requires `agent_registry_management`. Query `GET /api/v1/features` to inspect
the effective state. Disabled server-side APIs return `403` with code `feature_disabled`.
See the [Feature Gate module design](docs/architecture/modules/feature-gates.md) for the extension
contract and boundaries.

With the `standard` profile, a Task can request independent review using structured acceptance
criteria. Executor and Reviewer work is persisted as separate Runs, failed reviews create bounded
revision Runs, and exhausted limits move the Task to `WAITING_APPROVAL` instead of accepting a
failed candidate. See the
[Reviewed execution implementation](docs/architecture/modules/reviewed-execution-implementation.md).

With the `full` profile, distinct Subtasks can run in parallel and flow their durable outputs into
dependent Subtasks before an independent Supervisor synthesizes the final result:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Research and summarize","execution_mode":"COORDINATED","max_concurrency":2,"subtasks":[{"key":"research-a","objective":"Research source A"},{"key":"research-b","objective":"Research source B"},{"key":"synthesize","objective":"Compare the research","depends_on":["research-a","research-b"]}]}'
```

Run the returned Task normally and inspect its `subtasks`, Runs, and Attempts through the Task API.
See [Coordinated Subtask DAG execution](docs/architecture/modules/coordinated-dag-implementation.md)
for durability, capability matching, propagation, and current-scope guarantees.

In the `full` profile, a completed source Subtask can also request a structured Handoff to an
unstarted downstream Subtask. The target Agent explicitly accepts or rejects it through the Task
Handoff endpoints. Accepted contracts bind the later target Run and enter its structured context;
rejected contracts remain audit history. See the
[Handoff lifecycle implementation](docs/architecture/modules/handoff-lifecycle-implementation.md).

The current Artifact increment accepts Base64-encoded UTF-8 `text/plain` and
`application/json` content up to 64 KiB by default. It persists immutable content hashes and
versions in PostgreSQL and supports verified download. This deliberately does not claim to be
the future large-file object-storage or malware-scanning path.

### Run with Docker Compose

```bash
docker compose up --build
```

Open the API documentation at `http://localhost:8000/docs`, or run:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Run the AgentMesh demo","input":{"source":"curl"}}'
```

Use the returned task ID to execute it:

```bash
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs \
  -H "Idempotency-Key: example-run-1"
```

The run command returns `202 Accepted`. Query `GET /api/v1/tasks/<task-id>` to observe
the Task, Run, and Attempt states until completion.

Pause queued or running work and later resume the same durable Run and LangGraph thread:

```bash
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/pause
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/resume
```

A queued Run pauses immediately. A running Run first reports `PAUSE_REQUESTED` and becomes
`PAUSED` at the next durable post-node boundary. Resume creates a new fenced Attempt without
re-executing a node whose output is already checkpointed.

Enable the `full` profile to invoke the bundled read-only MCP workspace Tool. In the Compose image,
the allowed root defaults to `/app`; configure `AGENTMESH_MCP_WORKSPACE_ROOT` and mount a volume to
expose a different directory.

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Read the project README","input":{"tool_call":{"tool":"workspace.read_text","arguments":{"path":"README.md"}}}}'
```

Run the returned Task normally, then inspect its digest-only invocation audit at
`GET /api/v1/tasks/<task-id>/tool-invocations`. The runtime verifies the MCP Server identity,
Tool allowlist, `readOnlyHint`, JSON Schema, path confinement, and result byte limit.

Enable the `observability` Gate to expose `GET /api/v1/tasks/<task-id>/usage`. Each Attempt includes
a stable Trace ID. Model executors can report Token buckets and integer-micro costs into the
PostgreSQL business ledger; the built-in deterministic Agent reports no fabricated usage.

To mirror content-free Attempt and generation metadata to Langfuse, set:

```dotenv
AGENTMESH_FEATURE_GATES=observability=true
AGENTMESH_LANGFUSE_ENABLED=true
AGENTMESH_LANGFUSE_PUBLIC_KEY=pk-lf-...
AGENTMESH_LANGFUSE_SECRET_KEY=sk-lf-...
AGENTMESH_LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Task objective, input/output, prompts, and Tool bodies are not exported by this adapter. Langfuse
failure does not affect execution or accounting. See the
[Observability and usage increment](docs/architecture/modules/observability-usage-implementation.md).

Enable `observability` and `budget_admission` to attach an immutable Task budget covering Run and
Attempt counts, Token/cost totals, and an overall UTC deadline. Token/cost limits include explicit
per-Attempt reservations, preventing parallel Workers from spending the same remaining capacity.
Inspect authoritative settled and reserved values at `GET /api/v1/tasks/<task-id>/budget`.

```json
{"objective":"Bounded work","budget":{"max_runs":3,"max_attempts":4,"max_tokens":20000,"token_reservation_per_attempt":4000,"max_cost_micros":5000000,"cost_reservation_micros_per_attempt":1000000,"currency":"USD"}}
```

Actual overruns and expired deadlines preserve accounting and move the Task to
`WAITING_APPROVAL`. An operator can inspect the durable candidate, reject it, or submit a monotonic
budget increase and resume from the recorded execution boundary:

```bash
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/resolutions/increase-budget-and-resume \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: increase-budget-1" \
  -d '{"actor":"operator","reason":"Approved extension","budget":{"max_runs":5}}'
```

See the [Task budget](docs/architecture/modules/task-budget-admission-implementation.md) and
[Human Task resolution](docs/architecture/modules/human-task-resolution-implementation.md)
implementation documents. With `identity_rbac` enabled, the authenticated Principal replaces the
client-supplied audit actor.

### Enable the Identity and RBAC boundary

Identity is disabled in every built-in profile, including `full`, because enabling it safely
requires an explicit credential. Generate a long random Bearer token outside the repository and
configure only its SHA-256 digest:

```bash
python -c "import hashlib; print(hashlib.sha256(b'replace-with-a-random-token-at-least-32-bytes').hexdigest())"
```

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true
AGENTMESH_IDENTITY_PRINCIPALS_JSON=[{"principal_id":"admin","tenant_id":"default","principal_type":"USER","status":"ACTIVE","roles":["TENANT_ADMIN"],"token_sha256":"<sha256-hex>"}]
```

After restarting, all `/api/v1` requests require the Bearer token. Health, readiness, OpenAPI, and
API documentation remain public.

```bash
curl http://localhost:8000/api/v1/identity/me \
  -H "Authorization: Bearer <raw-token>"
```

Available baseline roles are `TENANT_ADMIN`, `OPERATOR`, `AGENT_AUTHOR`, `AGENT_PUBLISHER`,
`APPROVER`, and `AUDITOR`. Agent authors cannot publish their own Versions. See the
[Identity/RBAC baseline](docs/architecture/modules/identity-rbac-baseline-implementation.md) for
the permission matrix, failure behavior, and current limitations.

For durable Principal and RoleBinding administration, enable `persistent_identity` as well. In
this mode configured bootstrap Principal IDs must be UUIDs. Initial roles are seeded only when a
Principal is first created, so a later database revocation is never undone by restart.

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,persistent_identity=true
AGENTMESH_IDENTITY_PRINCIPALS_JSON=[{"principal_id":"10000000-0000-0000-0000-000000000001","tenant_id":"default","principal_type":"USER","status":"ACTIVE","roles":["TENANT_ADMIN"],"token_sha256":"<sha256-hex>"}]
AGENTMESH_IDENTITY_OIDC_ISSUER=https://idp.example
AGENTMESH_IDENTITY_OIDC_AUDIENCE=agentmesh-api
```

OIDC tokens must pass signature, issuer, audience and time validation and match a registered
ExternalIdentity. AgentMesh ignores IdP role claims and resolves active PostgreSQL RoleBindings on
every request. Administration under `/api/v1/identity/principals` requires `TENANT_ADMIN`. This
Gate remains disabled in all built-in profiles. See the
[Persistent Identity/OIDC baseline](docs/architecture/modules/persistent-identity-oidc-implementation.md).

### Require Policy approval for high-risk actions

Enable Policy only together with Identity. The built-in secure rules require independent approval
for Agent Version publication and Task budget increases:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true
```

The requester creates an exact ActionIntent at `POST /api/v1/policy/actions`. An `APPROVER` reviews
the pending item through `/api/v1/approvals` and approves or rejects it. Approval returns a
short-lived `permit_id`; the original requester supplies it exactly once:

```bash
curl -X POST http://localhost:8000/api/v1/agent-versions/<version-id>/publish \
  -H "Authorization: Bearer <publisher-token>" \
  -H "Execution-Permit-Id: <permit-id>" \
  -H "Content-Type: application/json" \
  -d '{"verified_capabilities":["document.summarize"],"make_default":true}'
```

The Permit is bound to the requester, tenant, action, resource and canonical arguments. See the
[Policy/Approval baseline](docs/architecture/modules/policy-approval-baseline-implementation.md).

### Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres redis
alembic upgrade head
agentmesh-seed
uvicorn agentmesh.api.app:app --reload
```

Run the relay and worker in two additional terminals:

```bash
agentmesh-relay
agentmesh-worker
```

On PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1`.

The local defaults use `127.0.0.1` explicitly so PostgreSQL connections behave consistently across Windows, WSL, and Docker Desktop. Container-to-container connections continue to use the Compose service name `postgres`.

Run the fast test suite with:

```bash
ruff check .
pytest
```

With PostgreSQL and Redis running and migrations applied, include the real transport,
persistence, and checkpoint test with:

```bash
AGENTMESH_RUN_POSTGRES_TESTS=1 pytest -m postgres
```

On PowerShell, set the flag with `$env:AGENTMESH_RUN_POSTGRES_TESTS="1"`.

Install the optional Langfuse adapter with `pip install -e ".[dev,observability]"` before enabling `AGENTMESH_LANGFUSE_ENABLED`.

## Design principles

1. Single-agent by default; multi-agent by demonstrated need.
2. PostgreSQL is the business source of truth.
3. Agent conversation is not a substitute for a workflow state machine.
4. Every handoff carries a typed contract and explicit acceptance criteria.
5. High-risk actions require least privilege and policy-controlled approval.
6. Durable state and idempotency take precedence over clever prompting.
7. Observability is part of the execution contract, not an afterthought.
8. Protocols are boundaries: A2A for agent delegation, MCP for tools and context.

## Current scope

The implemented slice is asynchronous but deliberately single-agent. It includes reliable
Outbox/Inbox delivery, Redis Streams workers, execution leases, idempotent run requests,
PostgreSQL-backed LangGraph checkpoints, durable pause/resume, and the local Agent Registry core
with immutable Version bindings and capability discovery. Registry management is optional and
disabled by the default `minimal` profile. A gated inline-small Artifact Service supports
immutable text/JSON versions and verified download. It does not yet include real model providers,
planning and multi-agent scheduling, governed MCP Registry/Gateway or write tools, A2A Agent Card
import/peers, reviewers, approvals, large-file object storage and content scanning, full
evaluation/OTel operations, authentication, or a Web Console. A gated `workspace.read_text` MCP stdio Tool is
implemented as the first protocol vertical slice with durable invocation audit.

## Contributing

AgentMesh is at an early design stage. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing architecture changes.

## License

Licensed under the [Apache License 2.0](LICENSE).
