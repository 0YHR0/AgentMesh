# AgentMesh

AgentMesh is an open-source control plane for coordinating, observing, and governing teams of AI agents.

AgentMesh（协作式智能体平台）旨在让使用者只需要定义目标、约束和验收标准，平台负责规划、分派、流转、观察、介入与审计 Agent 的执行过程。

> Status: pre-alpha. The repository contains an architecture baseline and a minimal runnable single-agent vertical slice.

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

## Minimal runnable slice

The current MVP proves this path:

```text
HTTP task command
  -> framework-independent Task domain
  -> PostgreSQL business ledger
  -> LangGraph workflow with a durable thread
  -> deterministic local Agent executor
  -> persisted result and observable Run
```

The deterministic executor intentionally requires no model API key. It validates the platform boundary before real model, MCP, A2A, queue, reviewer, and multi-agent behavior are added.

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
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs
```

### Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
uvicorn agentmesh.api.app:app --reload
```

On PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1`.

The local defaults use `127.0.0.1` explicitly so PostgreSQL connections behave consistently across Windows, WSL, and Docker Desktop. Container-to-container connections continue to use the Compose service name `postgres`.

Run the fast test suite with:

```bash
ruff check .
pytest
```

With PostgreSQL running and migrations applied, include the real persistence and checkpoint test with:

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

The implemented slice is deliberately synchronous and single-agent. It does not yet include a queue, asynchronous workers, real model providers, MCP tools, A2A peers, reviewers, approvals, an artifact store, or a Web Console. These capabilities will be added through the documented ports and module boundaries.

## Contributing

AgentMesh is at an early design stage. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing architecture changes.

## License

Licensed under the [Apache License 2.0](LICENSE).
