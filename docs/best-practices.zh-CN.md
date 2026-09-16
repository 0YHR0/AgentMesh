# AgentMesh 最佳实践

[English](best-practices.md)

状态：单团队 Alpha 基线已支持  
最后更新：2026-09-16

本文是[实现状态](implementation-status.md)、[路线图](roadmap.md)和
[v1 完成范围](v1-completion-scope.md)的运维配套指南，描述当前仓库实际支持的运行方式。
它不会把 Alpha 版本包装成生产级高可用产品：跨租户隔离、托管 PostgreSQL HA/PITR、托管
Runtime 耐久性和 Chaos 资格验证仍属于后续工作。

## 按使用场景阅读本文

| 场景 | 从哪里开始 | 推荐做法 |
| --- | --- | --- |
| 评估产品或演示界面 | 第 1–2 节 | 使用确定性 Provider 和 Compose，不配置 API Key；先创建一个 Direct 任务。 |
| 用真实模型完成一个边界清晰的工作 | 第 3–4 节 | 只在 Worker 环境配置模型 Key，发布不可变 Agent Version，并使用 Direct 执行。 |
| 让多个专业角色协作交付一个成果 | 第 4、6 节 | 仅在拆分或独立复核确有收益时使用 Coordinated，并明确依赖、验收标准、deadline 和预算。 |
| 为 Agent 接入工具或远程 Agent | 第 7 节 | 从只读开始，先启用 Identity/Policy，发布不可变 MCP/A2A snapshot，外部写入继续要求审批。 |
| 在远程服务器部署非关键单团队实例 | 第 8–11 节 | 使用 test-host Compose override、Firewall/SSH Tunnel，监控 PostgreSQL/Relay/Worker，并验证备份恢复。 |
| 处理未知结果或中断的工作流 | 第 5、12 节 | 保留持久化 unknown/paused 状态，检查证据，并使用原始幂等或 correlation key 做 reconciliation。 |
| 规划生产部署 | [生产加固 #160](https://github.com/0YHR0/AgentMesh/issues/160) | 把当前 Alpha 作为基线；完成持久 Runtime、Chaos、身份、隔离、HA、安全和负载验证后才能宣称生产就绪。 |

## 1. 从最小可用部署开始

先使用确定性 Provider。它不需要模型 API Key、不访问外部网络，是验证
Task → Run → Attempt → Artifact/usage 链路最快的方式。

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

检查公开健康接口并打开 Console：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/api/v1/features
```

- Console：<http://localhost:8000>
- OpenAPI：<http://localhost:8000/docs>
- Relay 指标：<http://localhost:9464/metrics>

仓库内 Compose 默认使用 `minimal` profile，同时为了首次打开 Console 的演示覆盖开启
`coordinated_execution=true`。如果确实只需要 Direct，本地启动前在 `.env` 写入：

```dotenv
AGENTMESH_FEATURE_PROFILE=minimal
AGENTMESH_FEATURE_GATES=coordinated_execution=false
```

第一次运行不要配置真实凭据。确定性流程健康后，再一次只启用一个能力，并用
`GET /api/v1/features` 确认实际生效状态。

## 2. 有意识地选择 Feature Profile

Profile 只是便利组合，不是安全边界。逐项覆盖会在启动时校验，依赖必须完整满足，配置
变化必须重启。

| Profile | 推荐用途 | 默认包含的可选能力 |
| --- | --- | --- |
| `minimal` | 首次启动和冒烟测试 | 仅核心 Task 执行（Compose 为演示额外开启 coordinated） |
| `standard` | 复核工作和 Agent 管理 | Reviewed 执行、Agent Registry 管理、人工处理 |
| `full` | 本地评估完整单团队基线 | 在 standard 之上增加 Coordinated DAG、Handoff、Deployment、Artifact、只读 MCP、观测和预算 |

Identity、Persistent Identity、Policy、受治理 MCP、A2A Federation/Delegation、Credential
Broker、Company 模块、Office 2.5D 以及所有 managed-runtime cutover gate 都不会被内置
Profile 自动开启，必须显式配置并包含全部依赖。例如，先启用本地治理边界：

```dotenv
AGENTMESH_FEATURE_PROFILE=standard
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true
```

服务器会拒绝缺依赖的配置，不会悄悄运行半套策略。遇到带有 `feature_disabled` 的 `403`，
应当把它当作配置反馈，不要绕过 API。

不要为公网或生产服务开启任何 `managed_runtime_*_cutover`。查看
[qualification/a4-2-parity.json](qualification/a4-2-parity.json)：它明确记录 coordinated
managed 在失败、预算、取消和 unknown outcome 上与 legacy 路径存在安全差异。A4.3 耐久性
和 Chaos 工作正式接受前，managed cutover gate 只能用于测试和资格验证。

## 3. 使用不可变、能力匹配的 Agent Version

Agent Definition 是身份，Agent Version 是不可变的可执行契约。每次修改指令、模型、工具
profile 或 Runtime policy 都创建新 Version；经过复核后再发布，并让 Position 或 Task 绑定
精确的已发布 Version/digest。不要原地修改已发布 Version，也不要让运行中的 Task 解析
“latest”。

启用 `agent_registry_management` 后，建议按以下顺序注册：

1. 创建 Agent Definition。
2. 创建 Version，填写角色、能力、Runtime adapter、模型/限制策略和明确的工具 profile。
3. 通过 Agent Registry API 提交复核并发布。
4. 确认能力匹配，以及 deployment/instance 健康状态。
5. 将不可变 Version 绑定到 Task Subtask、Company Position 或 Operation。

Console 可以引导这些步骤；API 也可查看 OpenAPI 以及 `src/agentmesh/api/agent_routes.py`。
已发布 Version 不等于一个 Worker 进程：deployment 和 instance 提供运行健康信号，而
PostgreSQL 才是任务执行权威来源。

确定性 Agent 是参考执行器，不是质量基准。使用真实模型时，只把凭据放到 Worker 环境：

```dotenv
AGENTMESH_MODEL_PROVIDER=openai
AGENTMESH_MODEL_NAME=gpt-5.6-terra
AGENTMESH_MODEL_REASONING_EFFORT=low
OPENAI_API_KEY=replace-with-a-local-secret
```

不要提交 `.env`，不要把原始 Key 放进 Task，也不要将 Key 暴露给 Console/API。Compose 只会
把模型凭据作为 `AGENTMESH_OPENAI_API_KEY` 传给 `worker`。删除 Key 并恢复
`AGENTMESH_MODEL_PROVIDER=deterministic` 即可回到无 Key 模式。

非 LangGraph Runtime 应实现公共 Runtime SDK/conformance 边界。仓库的
`examples/reference-agent` 是最小 subprocess 参考实现，独立的
[AgentMesh Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter) 展示了
外部扩展边界。第三方扩展以同进程可信代码运行；`extensions.lock` 是运维准入和审计校验，
不是沙箱。

## 4. 让 Task 契约完整明确

创建 Task 时写清目标、输入形状、验收标准、期望 Artifact、deadline 和预算。天然适合一次
执行的工作使用 Direct；需要独立质量判断时使用 Reviewed；只有拆分、并行或角色分离有可
衡量收益时才使用 Coordinated。

使用稳定的幂等 Key 创建并运行有界 Task：

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Summarize the project README","input":{"source":"README.md"}}'

curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs \
  -H "Idempotency-Key: readme-run-20260916-01"

curl http://localhost:8000/api/v1/tasks/<task-id>
```

每个逻辑命令使用一个唯一 Key；网络重试时复用完全相同的 Key。不要因为第一次 HTTP 响应
丢失就生成新 Key，持久化幂等记录正是防止重复 Run 的依据。

对于小型并行 DAG，声明依赖关系，不要让 Agent 通过非结构化群聊协调：

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Research and summarize","execution_mode":"COORDINATED","max_concurrency":2,"subtasks":[{"key":"research-a","objective":"Research source A"},{"key":"research-b","objective":"Research source B"},{"key":"synthesize","objective":"Compare the research","depends_on":["research-a","research-b"]}]}'
```

检查返回的 `subtasks`、Runs、Attempts、Handoffs 和输出 lineage。下游 Subtask 应消费上游
持久化输出或 Artifact 引用，而不是复制未跟踪的 Prompt 消息。

## 5. 把控制命令当作持久化工作流

通过 API 或 Console 暂停、恢复和取消，并在调用方保存命令的幂等 Key：

```bash
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/pause
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/resume
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/cancel \
  -H "Idempotency-Key: cancel-<task-id>-01"
```

排队中的 Run 可以立即暂停；运行中的 Run 会在持久化的节点边界暂停。恢复会创建带 fencing
的 Attempt，并使用 checkpoint，不会重新执行已经完成的节点。Coordinated 取消会锁定整个
aggregate、遵守 drain 优先级；如果已经越过 Provider 边界，可能要等待 reconciliation 证据。
无法证明 Provider 已取消时，不能仅因为本地请求返回就表示成功取消。

`OUTCOME_UNKNOWN`、`LOST`、`RECONCILIATION_REQUIRED` 以及 A2A/MCP unknown 都是安全状态。
不要把它们改成 `FAILED`，也不要盲目重试。先查看持久化 evidence，然后使用正确的特权
reconciliation 命令，并提供同租户授权、`outcome:reconcile`、reason/evidence digest 和
幂等 Key。Provider 晚到的结果只保留为 evidence，不能覆盖已经取消或收敛的业务状态。

## 6. 在副作用之前限制成本、时间和审批

启用 `budget_admission` 前先启用 `observability`。设置 Run/Attempt/token/cost 硬限制和
UTC deadline。并行 Worker 尤其需要 per-Attempt reservation，避免多个 Worker 消耗同一份
剩余预算。

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

用 `GET /api/v1/tasks/<task-id>/budget` 查看结果。超预算和 deadline 到期会保留候选并把
Task 置为 `WAITING_APPROVAL`；使用单调增加的预算或人工决定处理。不要直接修改预算行。

发布、支出、外部写入等高风险动作必须同时启用 Identity 和 Policy。使用独立审批角色、
结构化 reason 和一次性、绑定动作的 Permit。Agent 作者不应发布自己的 Version。完整的
Intent/Permit/Receipt/Reconciliation 链路和明确业务审批就绪前，保持外部商业写入关闭。

## 7. 把 MCP 和 A2A 当作安全边界

### MCP

从只读工具开始。受治理 Registry 需要 `identity_rbac`、`policy_approval`、`mcp_read_tools`
和 `governed_mcp`；模型工具循环还需要额外 gate。发布不可变 Server/Tool snapshot，声明
副作用级别和 schema，在 Agent Version 中允许逻辑工具，并在执行前确认 Catalog 精确解析。

写入只能使用要求字符串 `idempotency_key` 的 `IDEMPOTENT_WRITE` 工具，且必须有精确批准的
ActionIntent 和一次性 Permit。当前基线仍禁用 `NON_IDEMPOTENT_WRITE` 与 `IRREVERSIBLE`。
响应丢失时用同一 operation key 查询/收敛，不能重复未知的外部副作用。

认证 Provider 使用 Credential Broker。PostgreSQL 只保存 metadata-only SecretReference，
真实值放在 API 进程环境。Broker 发放短期 workload lease；不要把用户 Bearer Token 或原始
secret 传进 Agent state。

### A2A

注册同租户 Peer，发现其固定 HTTPS A2A Agent Card，检查候选并显式激活不可变 snapshot。
Federation、Delegation 和后台 reconciliation 是不同 gate。开启自动轮询时使用 Compose 的
`a2a-reconciler` profile：

```bash
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true,a2a_reconciliation=true docker compose --profile a2a up -d
```

使用稳定 correlation/idempotency identity、有界 timeout 和 workload-bound credential。远程
取消是 best effort；远程晚到完成只作为 evidence 保存，不会静默改写本地终态。Agent Card
声明的 Skill 只是候选，不代表可信能力。

## 8. 区分权威来源并观察它们

PostgreSQL 是 Task、Run、Attempt、Subtask、Agent 绑定、审批、预算、Artifact metadata、
Outbox/Inbox、MCP/A2A correlation 和 reconciliation evidence 的业务权威来源。Redis Streams
只是投递基础设施。Redis 丢失时应从持久化 Outbox 恢复 Relay/重放，而不是手工修改业务状态。

API、Relay 和 Worker 是独立进程。至少保持一个健康 Relay 和 Worker，关注 readiness，并监控：

- API readiness 和命令接收延迟；
- Outbox pending/quarantined 行和 Relay 发布延迟；
- Redis consumer pending 深度和 dead-letter 增长；
- Attempt lease 过期、Worker capacity 和 queue age；
- 预算/配额拒绝、MCP circuit 状态和 A2A unknown correlation；
- Artifact digest 失败和 reconciliation 时长。

Relay Prometheus 指标在 `http://localhost:9464/metrics`。启用 `observability` 后可查询 usage
和 trace。Langfuse 是可选项，先安装 `.[observability]`，再配置：

```dotenv
AGENTMESH_FEATURE_GATES=observability=true
AGENTMESH_LANGFUSE_ENABLED=true
AGENTMESH_LANGFUSE_PUBLIC_KEY=pk-lf-...
AGENTMESH_LANGFUSE_SECRET_KEY=sk-lf-...
AGENTMESH_LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

适配器只镜像 privacy-safe 的 Attempt/generation metadata，不导出 Task objective、Prompt、
输入/输出或 Tool body。Langfuse 故障不能阻塞执行或记账。使用持久化 Attempt Trace ID
关联 API、Worker 和外部观测记录。

## 9. 安全运行 PostgreSQL 和 Redis

本地或单节点评估使用内置 Compose：它创建 PostgreSQL volume，并把 PostgreSQL/Redis 绑定到
回环地址。不要把未认证的开发 API 直接暴露到公网。小型远程主机使用
`compose.test.yaml`、防火墙和 SSH tunnel：

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --build
ssh -L 8000:127.0.0.1:8000 user@test-host
```

实际生产部署应自行提供 TLS/认证边界、secret manager、备份、资源限制、镜像 digest 固定、
日志轮换和 PostgreSQL HA/PITR 方案；这些不属于本 Alpha 内置 Compose 的认证范围。

数据库 metadata 和内容寻址 Artifact 必须一起备份：

```bash
python scripts/operations/backup.py
python scripts/operations/restore.py backups/agentmesh-YYYYMMDDTHHMMSSZ --yes
python scripts/ci/compose_e2e.py
```

Redis 不进入业务备份，因为它只是传输状态。恢复期间停止 API/Worker/Relay/Reconciler，或
使用隔离的 Compose project。恢复后检查 `/ready`、Task→Run→Attempt lineage、Artifact
digest/download、审批、Outbox backlog、Replay Bookmark，并运行一个新的端到端 Task。
参见[SLO 与恢复手册](operations/slo-and-restore.md)。

## 10. 用兼容性证据升级和回退

发布前先备份，在临时环境运行 migration，并检查模型是否有未生成的变化：

```bash
alembic upgrade head
alembic check
```

普通发布应让 API/Relay/Worker 与 release 记录的 migration floor 一起升级。不要手改 migration
表，也不要降级越过已经写入新 evidence 的 schema。托管 direct-runtime 实验严格遵循
[Runtime 回退手册](operations/runtime-direct-cutover-rollback.md)：停止新 admission，只关闭
admission gate，保留既有 Run authority，并在不回退到 legacy worker 或盲目 redispatch 的前提
下收敛 crossed/unknown 执行。

## 11. 测试你真正准备运行的 Profile

每次变更运行快速检查：

```bash
ruff check .
pytest
```

PostgreSQL 和 Redis 可用时，运行真实传输/持久化/checkpoint 测试：

```bash
AGENTMESH_RUN_POSTGRES_TESTS=1 pytest -m postgres
```

PowerShell：

```powershell
$env:AGENTMESH_RUN_POSTGRES_TESTS="1"
pytest -m postgres
```

Compose 资格流程在服务 ready 后运行：

```bash
python scripts/ci/compose_e2e.py
```

Runtime authority 变更还应审查 parity/activation 机器可读 fixture、定向 PostgreSQL 原子性
测试、migration 升降级矩阵，以及 GitHub CI 的 PostgreSQL/Compose/coverage/CodeQL 门禁。只有
单元测试绿色不能证明 crash window、并发和回滚安全。

## 12. 故障排查清单

| 现象 | 首要检查 | 安全处理 |
| --- | --- | --- |
| `503` 或 `/ready` 未就绪 | `docker compose ps`、`docker compose logs migrate api relay worker`、PostgreSQL/Redis health | 修复依赖、migration 或配置；不要手工写业务行 |
| `403 feature_disabled` | `GET /api/v1/features`、Profile 和 gate 依赖 | 补齐依赖链、重启并重新检查实际状态 |
| Task 一直排队 | Relay 指标、Outbox pending、Worker health、Agent Version/deployment health | 修复投递或 Worker capacity；保留 Task 和幂等 Key |
| Run 是 `PAUSED`/`WAITING_APPROVAL` | Task detail、验收/预算/deadline evidence | 使用对应 resume、resolution 或 approval 命令；不要强改状态 |
| Runtime/A2A/MCP outcome unknown | evidence、correlation、operation key、reconciliation age | 查询/收敛；绝不盲目重试外部副作用 |
| Artifact 下载失败 | SHA-256、scan state、Artifact volume mount | 隔离可能的损坏，并从备份恢复/校验 |
| Langfuse 不可用 | `observability` gate 和适配器日志 | 继续使用 PostgreSQL trace/usage，单独修复 Langfuse |

拿不准时，优先保留显式、持久化的 `WAITING_APPROVAL` 或 reconciliation 状态，不要猜测成功
或失败。这正是 AgentMesh 的核心可靠性契约。

## 13. 推荐采用顺序

1. 用 Compose 运行确定性 Direct Task，并验证备份/恢复。
2. 加入 `standard`，注册不可变 Agent Version，引入 Reviewed 执行。
3. 加入 `full`，运行包含明确验收标准和有界预算的小型 Coordinated DAG。
4. 在 Identity/Policy 保护下开启只读 MCP，并保持工具白名单窄小。
5. 只对可信测试 Peer 开启 A2A 发现和后台 reconciliation。
6. 只为定义清楚的单团队工作流开启 Company/Memory/Finance/Pack；staffing、approval 和
   preflight 通过前，让 recurring Operation 保持 `DRAFT`。
7. 用 Office/Mission Map 做操作可视化，但以 Control API 和 PostgreSQL 为权威，不把视觉
   模拟当作业务状态。
8. 在任何 managed cutover 或外部写入前，先提出并完成 production-runtime/chaos 资格验证。

这一顺序让首次使用保持简单，同时保留未来多 Agent 虚拟公司所需的持久化契约。
