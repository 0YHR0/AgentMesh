# AgentMesh

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml)

AgentMesh 是一个用于协调、观察和治理 AI Agent 团队的开源控制平面。

你只需要定义目标、约束和验收标准，AgentMesh 负责规划、分派、流转、执行、复核、
人工介入和审计。简单任务可以由单个 Agent 直接完成，复杂任务则可以拆解为具有依赖关系
的多个工作单元，由不同角色的 Agent 并行协作。

> 当前状态：Alpha（`v0.1.0-alpha.1`）。单团队 v1 范围已经实现并通过发布验收，适合
> 评估、本地开发和非关键单团队部署。多租户隔离和生产高可用认证属于后续范围。

## 核心能力

- Direct、Reviewed 和 Coordinated 三种任务执行模式。
- PostgreSQL 权威业务账本、Transactional Outbox/Inbox 和 Redis Streams。
- 带租约、fencing token 和 LangGraph PostgreSQL Checkpoint 的可恢复执行。
- 版本化 Agent Registry，以及按角色绑定的模型、能力、工具和运行策略。
- MCP Registry、只读工具、受 Permit 保护的幂等写入和未知结果收敛。
- A2A Peer/Agent Card Registry、远程委派、轮询、取消和状态收敛。
- Policy、分阶段/角色约束/Quorum 审批和一次性 Permit。
- Handoff、Goal Contract、Plan Patch、Artifact、预算、配额和使用量账本。
- 可回放的 Mission Map，展示 Agent 状态、依赖和 MCP/A2A/审批等交互流转。
- Langfuse 隐私安全观测、Prometheus 指标、备份恢复和免费 GitHub CI。

## 技术栈

- 编排：LangGraph
- 权威数据源：PostgreSQL
- 事件传输：Redis Streams
- Agent 互操作：A2A
- 工具和上下文互操作：MCP
- LLM 观测与评估：Langfuse
- Artifact：v1 使用内容寻址本地存储，并保留 S3 兼容适配边界

## 快速启动

需要 Docker Desktop 或兼容的 Docker Engine：

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

打开：

- AgentMesh Console：<http://localhost:8000>
- OpenAPI 文档：<http://localhost:8000/docs>
- Relay Prometheus 指标：<http://localhost:9464/metrics>

默认执行器是免费的确定性运行时，不需要模型 API Key。Console 默认显示英文，可在顶部
工具栏切换为简体中文，语言选择会保存在浏览器中。

打开 `http://localhost:8000/world`，或点击 Console 顶部的 **AgentMesh Office**，可以
进入空间化公司界面。中央场景由项目内自托管的 Phaser 3.90 渲染，任务列表和员工详情
仍使用可访问的 HTML。办公室是一张有边界的多屏大地图，支持 WASD/方向键与拖拽移动、
滚轮/HUD 缩放、聚焦选中员工和点击小地图导航。项目内置的语义地图还提供部门视图、
受限 A* 走廊寻路、员工列表、低动态 Handoff、显式开启的环境音、四向角色动画，以及
超过 50 名可见员工时的部门聚合。已发布 Agent Definition 会成为员工；仅存在于运行时的 Agent ID
会从真实 Task Run 投影出来。部门由角色、能力和标签推导，状态气泡、协作线路、流动数据
包和 Handoff 步行动画均来自权威的 Task、Run、Subtask 与 Handoff 状态，不维护另一套
游戏状态，也不提供虚构的经验等级。该页面与主 Console 共用会话级 Bearer Token 和
中英文偏好。

如需可选的高 DPI 正交策略视图，可显式配置
`AGENTMESH_FEATURE_GATES=office_3d=true`，然后访问 `http://localhost:8000/world-3d`。
该模式使用项目内自托管的 Babylon.js、3D 场景几何和清晰的 DOM 状态标签，同时保留
`/world` 作为轻量回退。研究、分析、工程和评审运营区分别拥有独立的建筑轮廓、功能设施、
双语部门标牌和克制的标志性动效，而不是仅靠换色区分。实验渲染器不会随任何内置 Profile
（包括 `full`）自动开启。

启用后，`/world-3d` 是日常使用的公司主界面，`/` 明确定义为**管理员后台**。用户可以
直接在 Office 中创建并选择立即执行真实的直接任务或多 Agent 协作任务。默认园区包含
八个独立风格空间，并使用权威坐标网格：拖动员工时只能落在未占用格子，工位持久化到
PostgreSQL；跨越房间边界时，部门由服务端根据格子位置自动更新。空闲员工会在本部门内
短距离活动并回到持久化工位，这些动效不会改变 Task 状态。园区规划器还可以新增最多八个
租户共享装饰空间，并自动扩展边界、道路、标牌、相机范围和导航；受限空间定义持久化到
PostgreSQL，并在不同浏览器会话之间同步，已有浏览器本地布局会通过一次性兼容路径导入。
Office 还会把经过脱敏的 MCP、A2A 和审批交互投影为 Agent 与对应受治理站点之间的短时
数据包动画。Task 与 Agent 的权威状态仍来自 Control API。

启用 `mcp_read_tools` 后，Console 会显示可搜索的 Tool Catalog，创建 Agent Version 时
可以直接勾选已经发布的只读 Tool。启用 governed MCP 的完整依赖链后，授权的 Tool
Provider 还可以从官方 MCP Registry 搜索候选 Server，执行受限匿名发现，选择明确标记
为只读的 Tool Schema 并发布不可变快照，无需手写 JSON。Registry 条目只作为候选来源，
需要 Bearer 或自定义认证的首次发现仍需手动配置。

## 运行完整多 Agent Showcase

Showcase 不访问外部网络，也不需要付费 API：

```bash
AGENTMESH_FEATURE_PROFILE=full docker compose up -d
docker compose --profile showcase run --rm showcase
```

PowerShell：

```powershell
$env:AGENTMESH_FEATURE_PROFILE = "full"
docker compose up -d
docker compose --profile showcase run --rm showcase
```

在 2 核 4 GiB 的远程测试服务器上，使用带资源限制的 Compose Overlay：

```bash
AGENTMESH_FEATURE_PROFILE=full \
docker compose -f compose.yaml -f compose.test.yaml up -d --build
```

该 Overlay 将 API 和 Relay 指标限制为服务器回环地址，并设置保守的容器内存上限。
请通过 `ssh -L 8000:127.0.0.1:8000 user@test-host` 访问 Console，不要把未启用身份认证的
开发配置直接暴露到公网。

在 Console 中选择标题以 `[Showcase]` 开头的任务。Mission Map 会展示 Subtask DAG、
重试、Handoff、MCP、A2A、审批和 Plan Patch 证据，并支持过滤、缩放、聚焦、时间回放、
共享书签和脱敏导出。

## Feature Profile

AgentMesh 默认使用最小能力集，高级功能按需开启：

| Profile | 能力 |
|---|---|
| `minimal` | 核心 Task 执行；首次 Compose 启动额外开启 coordinated execution |
| `standard` | Reviewed execution、Agent Registry 管理和人工任务处理 |
| `full` | Coordinated DAG、Handoff、Deployment、Artifact、只读 MCP、观测和预算 |

在 `.env` 中设置：

```dotenv
AGENTMESH_FEATURE_PROFILE=full
```

也可以逐项覆盖：

```dotenv
AGENTMESH_FEATURE_GATES=reviewed_execution=true,coordinated_execution=true,agent_registry_management=true,artifact_service=true,mcp_read_tools=true,observability=true,budget_admission=true
```

Identity/RBAC 在所有内置 Profile 中都保持关闭，必须由部署者显式配置凭据后开启。

第一个 Virtual Company 模块需要显式开启：

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true
```

它提供 `/api/v1/companies` 下的 Company、Organization Unit、Position、Appointment 和组织关系图
接口。只有已经发布并满足 Position 所需能力的 Agent Version 才能被任命。开启后 Office 会优先
展示持久化的任命、职位和匹配的组织空间；关闭后现有 Agent Team 运行方式不受影响。

继续加入 `company_goals=true` 可启用 Operating Cycle、Objective、区分已验证值与估算值的
Key Result、Initiative，以及由 Initiative 发起的 Task 追踪。Initiative 必须经过批准和激活才能
通过原有 Task 应用服务创建任务，完成 Initiative 前至少要有一条持久化 Task 证据。Office 会在
匹配的组织空间上显示活跃 Objective 与 Initiative 数量。

## 使用真实模型

复制 `.env.example` 为 `.env`，配置 Worker 使用 OpenAI Responses API：

```dotenv
AGENTMESH_MODEL_PROVIDER=openai
AGENTMESH_MODEL_NAME=gpt-5.6-terra
AGENTMESH_MODEL_REASONING_EFFORT=low
OPENAI_API_KEY=replace-with-your-local-key
```

不要提交 `.env`。模型密钥只进入 Worker 环境，不写入 PostgreSQL，也不会暴露给 Console。
删除这些配置或恢复 `AGENTMESH_MODEL_PROVIDER=deterministic` 即可回到免费本地模式。

## API 示例

创建任务：

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Run the AgentMesh demo","input":{"source":"curl"}}'
```

运行返回的 Task：

```bash
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs \
  -H "Idempotency-Key: example-run-1"
```

查询执行状态：

```bash
curl http://localhost:8000/api/v1/tasks/<task-id>
```

暂停和恢复：

```bash
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/pause
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/resume
```

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres redis
alembic upgrade head
agentmesh-seed
uvicorn agentmesh.api.app:app --reload
```

另开两个终端运行：

```bash
agentmesh-relay
agentmesh-worker
```

PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活虚拟环境。

运行测试：

```bash
ruff check .
pytest
```

## 设计原则

1. 默认使用单 Agent，只有存在可证明收益时才使用多 Agent。
2. PostgreSQL 是业务状态的唯一权威来源。
3. Agent 对话不能代替工作流状态机。
4. 每次 Handoff 都携带类型化契约和明确验收标准。
5. 高风险动作遵循最小权限，并受 Policy 和独立审批控制。
6. 持久化状态和幂等性优先于复杂 Prompt 技巧。
7. 可观测性属于执行契约，而不是事后补充。
8. A2A 负责 Agent 委派，MCP 负责工具和上下文，两者都是安全边界。

## 当前边界

Alpha 已实现约定的单团队 v1 范围。以下能力明确属于后续扩展：

- 跨租户 RLS 和加权公平调度。
- 托管 PostgreSQL HA/PITR 和 Kubernetes 容量认证。
- 云 Secret Manager、OAuth Exchange、mTLS 和云对象存储。
- A2A Streaming/Push 和远程 Artifact 传输。
- 超过 20 个 Agent 的 Mission Map 语义聚类。

准确边界请参考：

- [v1 完成范围](docs/v1-completion-scope.md)
- [实现状态](docs/implementation-status.md)
- [路线图](docs/roadmap.md)
- [架构文档索引](docs/README.md)
- [版本变更记录](CHANGELOG.md)

## 贡献

提交架构或代码变更前，请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

使用 [Apache License 2.0](LICENSE)。
