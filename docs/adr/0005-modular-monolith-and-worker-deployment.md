# ADR 0005: Start with a modular control plane and independent workers

Status: Proposed
Date: 2026-07-16

## Context

候选能力可以拆成十多个服务，但早期缺少独立扩缩容、故障域和团队边界的证据。全部放在一个进程又会让长 Agent 执行、事件投递和公网集成拖垮 Control API。

## Decision

初始正式部署使用四类自有进程：

1. Web Console。
2. Control API，内部包含 Task、Registry、Policy/Approval、Artifact metadata 和 Identity/Tenancy 模块。
3. Execution Worker，包含 Orchestrator、Scheduler 和 Local Agent Runtime；MCP/A2A client adapter 可先内置。
4. Event Relay。

模块通过显式 Port 和数据所有权隔离。MCP Gateway 在凭证/egress 边界需要时首先拆出；A2A Gateway 在公网 ingress 或独立扩缩容需要时拆出。

## Consequences

- 降低首版部署和本地开发成本。
- API、Worker 和 Relay 已具备必要故障隔离和独立扩缩容。
- 模块化单体内可能出现跨模块直接调用诱惑，需要架构测试和 owner schema 约束。
- 后续拆服务需要将 Port 替换为网络/消息 adapter，但不应改变领域契约。

## Alternatives considered

- 每个候选模块独立微服务：拒绝，当前运维、事务和开发成本高于收益。
- 单一 API 进程执行所有 Agent：拒绝，长任务、资源隔离和重启恢复不可接受。
- 完全 serverless functions：不适合长连接、Worker lease、stdio MCP 和持久 Workflow 的初始基线。
