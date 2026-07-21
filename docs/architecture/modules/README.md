# L2 module designs

Status: Active.

本目录用于存放单个 L1 容器的内部组件设计。不要在这里重复 L0 目标或跨系统决策。

The currently implemented vertical slice is described here:

- [Repository implementation status](../../implementation-status.md)

- [Durable asynchronous execution](durable-async-execution.md)
- [Attempt lease renewal](attempt-lease-renewal-implementation.md)
- [Durable Task pause and resume](task-pause-resume-implementation.md)
- [Agent Registry implementation](agent-registry-implementation.md)
- [Role-bound model runtime implementation](role-bound-model-runtime-implementation.md)
- [Feature Gates](feature-gates.md)
- [Artifact Service implementation](artifact-service-implementation.md)
- [Bounded list queries](bounded-list-queries-implementation.md)
- [Read-only MCP Tool implementation](read-only-mcp-tool-implementation.md)
- [Governed MCP Registry and Catalog baseline](governed-mcp-registry-implementation.md)
- [Governed MCP Streamable HTTP runtime](mcp-streamable-http-implementation.md)
- [Controlled MCP capability snapshot refresh](mcp-capability-refresh-implementation.md)
- [Trusted A2A Peer and Agent Card Registry baseline](a2a-peer-registry-implementation.md)
- [Controlled A2A Agent Card discovery](a2a-agent-card-discovery-implementation.md)
- [Governed outbound A2A delegation baseline](a2a-outbound-delegation-implementation.md)
- [Durable automatic A2A reconciliation](a2a-reconciliation-implementation.md)
- [Controlled A2A remote cancellation](a2a-remote-cancellation-implementation.md)
- [Audited operator outcome reconciliation](operator-outcome-reconciliation-implementation.md)
- [Workload Credential Broker baseline](workload-credential-broker-implementation.md)
- [Observability and usage implementation](observability-usage-implementation.md)
- [Task budget and admission control implementation](task-budget-admission-implementation.md)
- [Human Task resolution implementation](human-task-resolution-implementation.md)
- [Identity, Principal, and RBAC baseline](identity-rbac-baseline-implementation.md)
- [Persistent Identity and OIDC baseline](persistent-identity-oidc-implementation.md)
- [Policy Decision and Approval baseline](policy-approval-baseline-implementation.md)
- [Event Relay poison-row quarantine](event-relay-quarantine-implementation.md)
- [Messaging retention and cleanup](messaging-retention-implementation.md)
- [Reviewed execution](reviewed-execution-implementation.md)
- [Coordinated Subtask DAG execution](coordinated-dag-implementation.md)
- [Goal Contract and verified Plan Patch](goal-contract-plan-patch-implementation.md)
- [Durable coordinated Handoff lifecycle](handoff-lifecycle-implementation.md)
- [CI and pull request governance](ci-and-pr-governance.md)

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
- [Hierarchical quota admission implementation](hierarchical-quota-admission-implementation.md)
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
