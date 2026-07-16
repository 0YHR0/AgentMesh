# AgentMesh

AgentMesh is an open-source control plane for coordinating, observing, and governing teams of AI agents.

AgentMesh（协作式智能体平台）旨在让使用者只需要定义目标、约束和验收标准，平台负责规划、分派、流转、观察、介入与审计 Agent 的执行过程。

> Status: pre-alpha. The repository contains a formal L2 architecture baseline and a
> durable asynchronous single-agent execution slice.

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
- [Roadmap](docs/roadmap.md)
- [Glossary](docs/glossary.md)
- [Architecture decisions](docs/adr/README.md)

## Runnable asynchronous slice

The current implementation proves this path:

```text
HTTP task command (202 Accepted)
  -> Task + Run + Transactional Outbox in PostgreSQL
  -> Event Relay -> Redis Streams consumer group
  -> Execution Worker + Attempt lease/fencing token
  -> LangGraph workflow + PostgreSQL checkpoint
  -> Inbox deduplication + persisted business result
```

The API, Event Relay, and Worker are separate processes. Redis is delivery infrastructure,
while PostgreSQL remains the business source of truth. The deterministic executor
intentionally requires no model API key.

### Feature profiles

AgentMesh defaults to the `minimal` profile so a first-time user only needs the Task API and
the built-in deterministic Agent. Optional management APIs are enabled explicitly:

| Profile | Enabled optional capabilities |
|---|---|
| `minimal` | None; core task execution remains available |
| `standard` | Agent Registry management |
| `full` | Agent Registry and Deployment management |

Choose a profile in `.env` before starting Compose:

```dotenv
AGENTMESH_FEATURE_PROFILE=standard
```

Individual gates can override the profile:

```dotenv
AGENTMESH_FEATURE_GATES=agent_registry_management=true,agent_deployments=false
```

Configuration is validated at startup and changes require a restart. Dependencies are strict:
`agent_deployments` requires `agent_registry_management`. Query `GET /api/v1/features` to inspect
the effective state. Disabled server-side APIs return `403` with code `feature_disabled`.
See the [Feature Gate module design](docs/architecture/modules/feature-gates.md) for the extension
contract and boundaries.

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
PostgreSQL-backed LangGraph checkpoints, and the local Agent Registry core with immutable
Version bindings and capability discovery. Registry management is optional and disabled by the
default `minimal` profile. It does not yet include real model providers,
planning and multi-agent scheduling, MCP tools, A2A Agent Card import/peers, reviewers,
approvals, an artifact store, full observability, authentication, or a Web Console.

## Contributing

AgentMesh is at an early design stage. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing architecture changes.

## License

Licensed under the [Apache License 2.0](LICENSE).
