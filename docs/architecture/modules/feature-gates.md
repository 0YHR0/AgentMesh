# Feature Gates

Status: Implemented baseline.

## 1. Responsibility

Feature Gate 模块负责把同一个 AgentMesh 发行版组合成不同复杂度的运行形态。它控制可选能力的入口，
不控制核心 Task 执行链路，也不承担认证、授权、计费套餐或数据库 migration 职责。

首版采用启动时不可变配置。这样 API、Worker 和未来的 Web Console 可以在一个进程生命周期内看到一致状态，
并避免正在执行的任务因动态开关发生语义变化。

## 2. Profiles and gates

| Profile | Key optional capabilities | Intended use |
|---|---|---|
| `minimal` (default) | None | 第一次运行、核心 Task API、单内置 Agent |
| `standard` | Registry management, reviewed execution | 自定义 Agent、版本、能力和受控独立评审 |
| `full` | All current Gates, including coordinated execution, Handoffs, observability and Task budgets | 完整的当前正式功能集合 |

当前 Gate：

| Gate | Dependency | Server-side boundary |
|---|---|---|
| `agent_registry_management` | None | Agent Definition、Version、Capability 和候选搜索 API |
| `agent_deployments` | `agent_registry_management` | Deployment、Instance 和 heartbeat API |
| `artifact_service` | None | Artifact 创建、版本、元数据和下载 API |
| `mcp_read_tools` | None | 显式 `tool_call` Task、MCP Runtime 和调用审计查询 API |
| `observability` | None | Task usage/cost 查询和 Langfuse export 前置条件 |
| `reviewed_execution` | None | 独立 Reviewer Run 和有界 Revision |
| `coordinated_execution` | None | Subtask DAG、能力路由和 Supervisor join |
| `handoffs` | `coordinated_execution` | Coordinated Subtask 间的结构化 Handoff lifecycle |
| `budget_admission` | `observability` | Task budget、Run/Attempt admission 和 budget status API |

Registry 的内部读取和内置 Agent seed 不受管理 Gate 限制，因为最小 Task 链路仍需要绑定不可变 Agent Version。
MCP Gate 关闭时，Task API 会在创建阶段拒绝包含 `tool_call` 的请求，Worker 也保留第二道拒绝边界；
已存在的调用审计不会删除，重新启用后仍可查询。

The `observability` Gate has no Feature dependency. It protects the tenant-scoped Task usage API
and is required before Langfuse export may be enabled. It is Off in `minimal`/`standard` and On in
`full`. Turning it Off retains Attempt Trace IDs and existing Usage records.

The `budget_admission` Gate requires `observability` because actual Token/cost settlement uses the
durable Usage ledger. Turning it Off does not remove existing budget policies or counters, but new
budgeted Tasks and the budget status API are unavailable. Workers continue honoring already
persisted budget contracts so a configuration change cannot silently weaken an in-flight Task.

## 3. Configuration contract

```dotenv
AGENTMESH_FEATURE_PROFILE=minimal
AGENTMESH_FEATURE_GATES=agent_registry_management=true,artifact_service=true,mcp_read_tools=true
```

解析顺序为 profile 基线，再应用逐项覆盖，最后验证依赖。Profile 会去除首尾空白并转为小写；Gate 名称区分
大小写，布尔值只接受 `true` 和 `false`。
重复 Gate、未知 Gate、未知 profile、非法值或缺失依赖都会在容器构建时抛出
`InvalidFeatureConfiguration`，阻止进程以歧义状态启动。

修改配置需要重启相关进程。`GET /api/v1/features` 始终可用，返回当前 profile、各 Gate 状态、依赖和
`restart_required` 标记。

## 4. Enforcement flow

```text
HTTP request
  -> FastAPI route dependency
  -> FeatureGateSet.require(feature)
     -> enabled: continue to application service
     -> disabled: 403 {"code":"feature_disabled", ...}
```

Gate 在应用服务产生副作用之前执行。OpenAPI 仍列出关闭的端点，让用户可以发现能力及其契约；调用时会得到
稳定且可机器识别的错误。未来 Web Console 应读取 `/api/v1/features` 调整导航，但 UI 隐藏只能作为体验优化，
不能代替服务端校验。

## 5. Data and deployment behavior

- 所有 profile 使用相同的代码包和数据库 schema。
- Migration 不受 Gate 控制，始终升级到当前 head。
- 关闭 Gate 不删除已有数据；再次启用后数据仍可使用。
- `minimal` 仍运行 API、Event Relay、Execution Worker、PostgreSQL 与 Redis，因为它们属于可靠执行核心。
- 首版 Gate 是部署级配置，不支持按用户、租户、请求或百分比灰度。

## 6. Extension contract

新增可选模块时必须同时完成：

1. 在 `Feature` 和 `FEATURE_SPECS` 注册稳定名称、说明与依赖。
2. 明确它属于哪些 profile；默认优先保持 `minimal` 关闭。
3. 在所有服务端入口执行 Gate，而不是只控制路由展示或前端菜单。
4. 保证关闭状态不会破坏核心链路，并提供 `minimal`、启用和依赖非法三类测试。
5. 在本文件记录数据保留、重启要求与运行中任务的行为。

如果未来确实需要动态或按租户发布，应新增独立 ADR，设计版本化快照、缓存失效、任务运行绑定与审计，
而不是改变当前 `FeatureGateSet` 的进程内不可变语义。
