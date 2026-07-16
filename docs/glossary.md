# Glossary

## Agent

具有明确指令、模型策略、工具、知识、权限和输入输出契约的智能执行单元。Agent 可以是本地定义，也可以是通过 A2A 接入的远程服务。

## Agent Definition

Agent 的版本化静态声明。描述它是什么、能做什么、允许做什么，但不表示当前有一个运行进程。

## Agent Instance

Agent Definition 的可执行实例，具有端点、健康、负载、租约或 Runtime 信息。

## Agent Assignment

调度器为一次 Run 选择的不可变执行契约，绑定 Agent Version、能力、输入输出、预算、策略、截止时间和路由理由。

## Agent Registry

保存 Agent Definition、版本、能力、A2A Agent Card、端点和运行状态的目录服务。

## A2A

Agent-to-Agent 协议。用于独立 Agent 之间的能力发现、任务委托、消息、状态和 Artifact 交换。A2A 不是全局调度器，也不要求使用消息队列。

## Approval

需要授权主体对高风险或受控动作做出的允许、拒绝或修改决定。

## Action Intent

副作用执行前创建的不可变动作规范，包含目标、规范化参数、风险、幂等策略和 action hash。Policy 和 Approval 必须绑定该 Intent。

## Artifact

任务产生的可复用结果。Artifact 的元数据进入业务账本，大型内容进入对象存储。

## Attempt

Run 内由一个 Worker lease 承担的一次执行尝试。Worker 崩溃、租约过期和重领会创建新 Attempt；它与业务重试产生的新 Run 不同。

## Checkpoint

LangGraph 在执行步骤之间保存的工作流状态快照，用于中断、恢复、回放和容错。

## Control plane

用于创建和治理 Task、Agent、策略、审批、配额、版本与审计的系统能力。

## Event

已经发生且不可变的领域事实。Event 可用于审计、通知和派生视图，但不能在未定义一致性语义时替代业务状态。

## Event Relay

读取 PostgreSQL Transactional Outbox 并向 Redis Streams 或其他目标进行至少一次发布的后台进程。

## Handoff

一个执行者将工作显式交给另一个执行者的行为和记录。Handoff 应携带结构化目标、约束、输入与验收标准。

## Idempotency Key

在明确作用域内标识同一业务意图的稳定键。响应丢失或重复投递时复用该键，确保返回首次结果而不是重复产生副作用。

## Inbox

消费者用于记录已处理消息和结果的持久化去重表。Inbox 更新与消费产生的业务变化必须在同一事务中提交。

## Langfuse

用于记录和分析 LLM/Agent Trace、模型调用、工具调用、Prompt、Token、成本和质量 Score 的可观测与评估平台。

## LangGraph

用于构建有状态、可持久化、可中断和可恢复 Agent 工作流的编排 Runtime。

## MCP

Model Context Protocol。Agent 通过 MCP Client 从 MCP Server 发现和使用 Tool、Resource 与 Prompt。MCP 不负责 Agent 之间的全局任务编排。

## MCP Gateway

位于 Agent 与 MCP Server 之间的治理边界，可负责连接、权限、策略、凭证、限流、审计和脱敏。

## MCP Registry

记录已准入 MCP Server、版本、地址、认证方式、工具能力、所有者和风险信息的内部目录。

## Operation

Control API 对异步管理或执行命令返回的可查询资源，表示命令处理进度；它不是 Task 或 Run 的替代品。

## Outbox

与业务状态在同一 PostgreSQL 事务中写入的待发布事件表，用于消除数据库提交与消息发布之间的丢失窗口。

## Policy Decision

Policy Engine 对 subject、action、resource 和 environment 的版本化判定，结果为允许、拒绝、需要审批或附带约束的允许。

## Principal

可被认证和授权的用户、服务、Agent 或外部 Peer。AgentMesh 保留 Principal 的委托链，避免机器动作伪装成人类动作。

## Projection

由领域事件或权威表派生的读模型，用于列表、搜索、仪表盘和实时视图。Projection 可以重建且可能存在可见延迟。

## Run

Task 或 Subtask 的一次可追踪执行。重新执行、改派或从历史状态分叉通常创建新 Run。

## Session

Langfuse 中聚合多个 Trace 的逻辑会话。在 AgentMesh 中通常与顶层 Task 关联，但不作为业务任务实体。

## Subtask

Task 的可调度分解单元，可以具有依赖、独立执行者、状态、预算和 Artifact。

## Task

用户或外部系统提交的顶层工作单元，包括目标及可选的约束、预算、优先级、截止时间和验收标准。

## Thread

LangGraph Checkpoint 的持久化执行游标。AgentMesh 的 Task、Run 和 Thread 不应默认使用同一个数据库实体。

## Tenant

AgentMesh 中数据、身份、配额、成本和保留策略的最高隔离边界。Project/Workspace 位于 Tenant 内部。

## Lease

Scheduler 在有限时间内授予 Worker 执行某 Attempt 的权利。Lease 携带 fencing token，过期 Worker 不能提交权威结果。

## Trace

一次端到端执行或请求的诊断链路，由多个 Span 组成。Trace 是观测数据，不是业务状态权威来源。

## Worker

消费就绪工作、获得执行租约并运行 Orchestrator 或 Agent 的进程。
