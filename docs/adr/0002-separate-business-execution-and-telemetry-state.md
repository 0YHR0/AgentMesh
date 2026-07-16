# ADR 0002: Separate business, execution and telemetry state

Status: Proposed
Date: 2026-07-16

## Context

Task/Run 状态、LangGraph Checkpoint 和 LLM Trace 都描述一次执行，但服务对象、恢复语义、保留期和可用性不同。将任意一类作为其他两类的权威来源，会让产品状态绑定执行框架或观测平台。

## Decision

- PostgreSQL business schemas 保存 Task、Subtask、Run、Approval、Artifact metadata 等权威业务事实。
- LangGraph Checkpointer 使用独立 execution schema 保存 Thread/Checkpoint，只负责恢复、interrupt 和 replay。
- Langfuse/OTel 保存 Trace、Span、Token、成本分析和 Score，不拥有业务状态。
- RunWorkflowBinding 和稳定关联 ID 连接三类状态。
- 不一致通过 Reconciler 产生正常领域命令收敛，禁止业务查询直接读取 Checkpointer 表推断状态。

## Consequences

- 可以替换编排或观测实现而不重建业务模型。
- 业务页面在 Langfuse 不可用时仍然工作。
- 需要显式处理 Checkpoint 已推进但业务事务未提交等一致性窗口。
- 会增加关联表、Reconciler 和三类数据保留策略的实现成本。

## Alternatives considered

- 以 LangGraph State 作为 Task 数据库：拒绝，产品查询、授权和迁移被执行框架锁定。
- 以 Langfuse Trace 重建 Task：拒绝，采样、导出失败和保留策略不满足业务账本要求。
- 所有状态放入一个 JSON 文档：拒绝，并发、查询、审计和演进成本过高。
