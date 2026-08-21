# Design and delivery roadmap

Status: Alpha
Last updated: 2026-08-20

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
- [x] 评审并接受 L0
- [x] 按依赖顺序形成正式 L2 模块基线

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
- [x] 最小管理界面

Exit signal：进程重启后能够可靠恢复任务，业务状态与 Trace 可关联。

Status：通过。

## Phase 2 — Reviewed execution

目标：加入独立验证和受控返工。

- [x] Executor + Reviewer
- [x] 结构化验收标准
- [x] 质量 Score
- [x] 有上限的修订循环
- [x] Task 总预算、deadline 和 `WAITING_APPROVAL` 升级
- [x] 人工预算调整与恢复命令

Exit signal：能够解释为什么返工，并证明循环不会无限执行。

Status：通过。

## Phase 3 — Coordinated local agents

目标：在同一控制平面内支持专业 Agent 的并行和交接。

- [x] Supervisor join（Planner/dynamic replanning 待后续）
- [x] 静态 Subtask DAG
- [x] 能力匹配和并行调度
- [x] Handoff Contract
- [x] Agent 级权限和成本归属
- [x] 基线冲突与合并策略（Supervisor join、Handoff、Plan Patch）

Exit signal：多 Agent 在目标场景中相对单 Agent具有可测量的质量、时延或风险收益。

Status：通过。Research Brief Showcase 提供可重复的多 Agent 治理与流转证据。

## Phase 4 — Governed MCP ecosystem

Current delivered baseline:

- [x] Tenant-scoped immutable Server/Version/Tool Registry and Catalog
- [x] Governed read-only stdio and Streamable HTTP execution
- [x] Workload-bound MCP Bearer credentials
- [x] Controlled capability snapshot refresh and drift blocking
- [x] Permit-bound `IDEMPOTENT_WRITE` execution with stable keys and unknown outcomes
- [x] Evidence-backed operator reconciliation without replay
- [x] Health/circuit controls
- [ ] OAuth, Resources and Prompts（外部适配器，v1 不阻塞）

目标：将工具接入从代码配置升级为受治理的平台能力。

- 私有 MCP Registry
- MCP Gateway
- Tool 准入、版本和健康检查
- 凭证代理与最小权限
- 风险分级、审批和审计

Exit signal：Agent 无需获取长期密钥即可安全调用获准工具。

## Phase 5 — Federated A2A agents

Delivered increment:

- [x] Idempotent best-effort remote cancellation with durable intent, lease recovery and polling convergence
- [x] Operator binding/non-delivery convergence for initial send outcomes without a remote Task ID

目标：接入独立部署、跨语言或跨团队 Agent。

- [x] 本地 Agent Registry core
- [x] A2A Agent Card 导入、验证、受控发现与显式激活（自动定时刷新待后续）
- [x] 持久化自动状态轮询、SKIP LOCKED 领取、崩溃租约恢复与失败退避
- A2A 同步、Streaming 和异步任务
- 状态、Artifact、取消与错误映射
- Peer 认证、限流、防重放和隔离
- 远程 Agent SLO 与降级策略

Exit signal：远程 Agent 断连、重复回调或超时后，内部任务状态仍能最终收敛。

## Alpha release — `v0.1.0-alpha.1`

- [x] 单团队 v1 范围实现完成
- [x] 版本化公共契约和 80% 覆盖率门槛
- [x] PostgreSQL/Redis 集成测试与 Compose E2E
- [x] 真实备份、清空、恢复和恢复后 E2E 演练
- [x] Mission Map、共享 Replay Bookmark 和系统 Showcase
- [x] 免费 GitHub CI、CodeQL、依赖审查和版本发布工作流

Exit signal：新用户可从干净 checkout 启动系统、运行 Showcase、观察 Agent 流转，并可
恢复权威业务状态。Alpha 不代表生产高可用、多租户隔离或云基础设施认证。

## Post-alpha — Virtual Company templates

- [x] 通用 Company、目标、Operations、Business Object、Memory、Finance 和 Pack 基础
- [x] 市场情报工作室模板预览、事务安装和中英文管理员界面
- [x] 无 API Key 的离线问题—来源—Claim—审核报告证据链
- [x] 可选 Operations Pack 驱动的 Operating Cycle、Objective/KR、Initiative、Memory Policy、预算与草稿 recurring Operation
- [x] 模板驱动的 Agent Appointment、岗位预检与显式 recurring Operation 激活向导
- [x] 自动 Memory 上下文与受治理的 Task 结果候选沉淀
- [x] Admin/Office Memory Inspector、学习审核队列与召回轨迹
- [ ] Task 成本归集和公司 Metrics
- [ ] 审批后的真实研究、发布、客户及财务适配器

Exit signal：用户可从模板创建公司、绑定真实 Agent，在不伪造收入或进度的前提下完成
可恢复、可审核的周期性业务闭环。

## Post-alpha — Multi-tenant platform operations

目标：面向多团队或多租户稳定运营。

- 租户隔离与配额
- 高可用和容量治理
- 版本发布、回滚和迁移
- 质量基线与回归评估
- 成本分摊与运营仪表盘
- 插件/Agent/MCP 管理生态

每一阶段开始前应通过上一阶段的 Exit signal，而不是仅以功能清单完成为标准。

## Control Plane refocus — P0

目标：将 AgentMesh 从 LangGraph-centric 多 Agent 平台收敛为管理任意 Agent Runtime 的可靠
控制平面。设计基线于 2026-08-16 接受，实施由 [Epic #134](https://github.com/0YHR0/AgentMesh/issues/134)
跟踪。

- [x] 接受 framework-neutral Control Plane ADR
- [x] 完成 Managed Agent Runtime API v0.1 可开发设计
- [x] 完成 Governed Action Protocol v0.1 可开发设计
- [x] 完成 Reliability Model/Chaos Qualification 可开发设计
- [x] 提供按 PR、迁移、回滚、测试和停止条件拆分的实施计划
- [x] LangGraph Adapter 通过统一 Runtime conformance（A4.0，PR #150 已交付）
- [x] 非 LangGraph subprocess Agent 通过同一 conformance（A4.0，PR #150 已交付）
- [x] A4.1a CI-only deterministic DIRECT 新 Run admission（gate、域模型、0047 持久化约束、
  内置 LangGraph v2 校验）
- [x] A4.1b.1 CI/test-only managed DIRECT Worker authority（fenced dispatch、原子终结、
  reconciliation-required 停车；不含 reconcile command）
- [x] A4.1b.2a reconciliation reader/schema compatibility（0048 expand-only；不含 writer/API）
- [ ] MCP write 和 fake external action 通过统一 Intent/Permit/Receipt/Reconciliation
- [ ] Chaos smoke 证明核心 crash windows 收敛且无重复不可逆副作用

Exit signal：同一部署管理 LangGraph 与非 LangGraph Agent；两者使用同一 Task/Run/Attempt、
身份、治理、Artifact 和恢复语义，并由机器可读故障报告证明关键不变量。

当前 A4.0 conformance harness 已在 PR #150 完成，A4.1a admission 与 A4.1b.1 managed DIRECT
Worker authority/atomic parking 已交付，A4.1b.2a reader/schema compatibility 已完成。
A4.1b.2b 仍需受权限控制、证据驱动的 reconcile command；
完整 A4 还需 chaos、parity、reviewed/coordinated cutover 和生产 durable runtime。#135/#136
继续保持开放。
