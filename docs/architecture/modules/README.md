# L2 module designs

Status: Active.

本目录用于存放单个 L1 容器的内部组件设计。不要在这里重复 L0 目标或跨系统决策。

The currently implemented vertical slice is described here:

- [Durable asynchronous execution](durable-async-execution.md)
- [Agent Registry implementation](agent-registry-implementation.md)
- [Feature Gates](feature-gates.md)

Bootstrap MVP documents remain as historical context for the first synchronous slice and
are superseded where they conflict with the durable asynchronous execution document:

- [Task domain and execution model](task-execution-model.md)
- [Persistence and consistency](persistence-and-consistency.md)
- [Orchestration and Agent Runtime](orchestration-runtime.md)
- [Control API](control-api.md)

The complete target design is maintained separately:

- [Formal L2 design baseline](formal/README.md)
- [Cross-module contracts](formal/cross-module-contracts.md)

Formal module documents:

- [Task and execution domain](formal/task-and-execution-domain.md)
- [Persistence and consistency](formal/persistence-and-consistency.md)
- [Orchestrator and scheduler](formal/orchestrator-and-scheduler.md)
- [Local Agent Runtime](formal/local-agent-runtime.md)
- [Agent Registry](formal/agent-registry.md)
- [MCP integration](formal/mcp-integration.md)
- [A2A integration](formal/a2a-integration.md)
- [Artifact Service](formal/artifact-service.md)
- [Policy and approval](formal/policy-and-approval.md)
- [Event Relay](formal/event-relay.md)
- [Observability and evaluation](formal/observability-and-evaluation.md)
- [Identity, tenancy and secrets](formal/identity-tenancy-and-secrets.md)
- [Control API](formal/control-api.md)
- [Web Console](formal/web-console.md)
- [Deployment and operations](formal/deployment-and-operations.md)

每份文档应从 [module design template](../../templates/module-design-template.md) 创建。
