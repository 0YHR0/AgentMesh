# Design and delivery roadmap

Status: Active
Last updated: 2026-07-18

路线图使用可验证的垂直切片推进。阶段编号描述交付成熟度，不等同于架构文档的 L0–L3。
各正式 L2 模块的当前代码成熟度与下一交付队列见
[Implementation status](implementation-status.md)。

## Phase 0 — Architecture baseline

目标：在不写运行时代码的前提下确定系统边界和设计方法。

- [x] 初始化开源仓库与许可证
- [x] 定义文档层级和贡献规则
- [x] 提出 L0 系统设计
- [x] 提出 L1 容器候选和设计顺序
- [x] 提出覆盖全部候选容器的正式 L2 设计基线
- [x] 选择首个真实落地场景：可恢复的异步单 Agent Task
- [ ] 评审并接受 L0
- [ ] 按依赖顺序评审并接受正式 L2 模块

## Phase 1 — Durable single-agent slice

目标：验证平台基础，而不是急于展示多 Agent 群聊。

- [x] 创建、查询、异步运行和取消 Task
- [x] 持久化暂停和恢复 Task
- [x] PostgreSQL 业务任务账本
- [x] Transactional Outbox、Redis Streams、Inbox 去重
- [x] Bounded Outbox/Inbox/Redis retention and Relay capacity metrics
- [x] Worker Attempt lease 和 fencing token
- [x] LangGraph PostgreSQL Checkpoint 与已完成结果恢复
- [x] 一个版本化本地 Agent 与 Agent Registry core
- [x] 一个只读 MCP 工具
- [x] 受限 inline-small Artifact 保存与下载（对象存储和内容扫描待后续）
- [x] Langfuse Trace、Token 和成本
- [x] Task 级 Run/Attempt/Token/成本/deadline 准入与保守预留
- [ ] 最小管理界面

Exit signal：进程重启后能够可靠恢复任务，业务状态与 Trace 可关联。

## Phase 2 — Reviewed execution

目标：加入独立验证和受控返工。

- [x] Executor + Reviewer
- [x] 结构化验收标准
- [x] 质量 Score
- [x] 有上限的修订循环
- [x] Task 总预算、deadline 和 `WAITING_APPROVAL` 升级
- [ ] 人工预算调整与恢复命令

Exit signal：能够解释为什么返工，并证明循环不会无限执行。

## Phase 3 — Coordinated local agents

目标：在同一控制平面内支持专业 Agent 的并行和交接。

- [x] Supervisor join（Planner/dynamic replanning 待后续）
- [x] 静态 Subtask DAG
- [x] 能力匹配和并行调度
- [x] Handoff Contract
- [ ] Agent 级权限和成本归属
- [ ] 冲突与合并策略

Exit signal：多 Agent 在目标场景中相对单 Agent具有可测量的质量、时延或风险收益。

## Phase 4 — Governed MCP ecosystem

Current delivered baseline:

- [x] Tenant-scoped immutable Server/Version/Tool Registry and Catalog
- [x] Governed read-only stdio and Streamable HTTP execution
- [x] Workload-bound MCP Bearer credentials
- [x] Controlled capability snapshot refresh and drift blocking
- [x] Permit-bound `IDEMPOTENT_WRITE` execution with stable keys and unknown outcomes
- [ ] Operator reconciliation, OAuth, health/circuit controls, Resources and Prompts

目标：将工具接入从代码配置升级为受治理的平台能力。

- 私有 MCP Registry
- MCP Gateway
- Tool 准入、版本和健康检查
- 凭证代理与最小权限
- 风险分级、审批和审计

Exit signal：Agent 无需获取长期密钥即可安全调用获准工具。

## Phase 5 — Federated A2A agents

目标：接入独立部署、跨语言或跨团队 Agent。

- [x] 本地 Agent Registry core
- [ ] A2A Agent Card 导入、验证与刷新
- A2A 同步、Streaming 和异步任务
- 状态、Artifact、取消与错误映射
- Peer 认证、限流、防重放和隔离
- 远程 Agent SLO 与降级策略

Exit signal：远程 Agent 断连、重复回调或超时后，内部任务状态仍能最终收敛。

## Phase 6 — Multi-tenant platform operations

目标：面向多团队或多租户稳定运营。

- 租户隔离与配额
- 高可用和容量治理
- 版本发布、回滚和迁移
- 质量基线与回归评估
- 成本分摊与运营仪表盘
- 插件/Agent/MCP 管理生态

每一阶段开始前应通过上一阶段的 Exit signal，而不是仅以功能清单完成为标准。
