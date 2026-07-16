# Persistence and consistency

Status: Accepted for MVP
Owners: AgentMesh maintainers
Depends on: [Task execution model](task-execution-model.md)

> Historical bootstrap baseline. The current implementation is defined by
> [Durable asynchronous execution](durable-async-execution.md); the formal target is
> [Persistence and consistency](formal/persistence-and-consistency.md).

## 1. Problem

AgentMesh 需要同时保存业务状态和 LangGraph Checkpoint。两者都可以位于 PostgreSQL，但必须拥有不同语义和迁移责任。

## 2. Responsibilities

- 持久化 Task 和 Run 业务实体。
- 提供事务、行锁和乐观版本能力。
- 为 LangGraph Postgres Checkpointer 提供独立 Schema/表空间语义。
- 定义迁移、连接和恢复边界。

## 3. Non-responsibilities

- 不用 Langfuse Trace 重建业务状态。
- 不将 Redis 视为权威存储。
- 不在业务事务中调用模型、MCP 或 A2A。

## 4. Database layout

MVP 使用一个 PostgreSQL Database。逻辑上分为：

- AgentMesh 业务表：由 Alembic 管理。
- LangGraph Checkpoint 表：由 `langgraph-checkpoint-postgres` 的 `setup()` 管理。

初版允许它们位于同一默认 Schema，以降低启动复杂度；生产化设计将评估独立 Schema 和数据库账号。业务代码不能直接查询 LangGraph 内部表。

## 5. Business tables

### tasks

- UUID primary key
- objective text
- input JSONB
- status varchar
- current_run_id UUID nullable
- output JSONB nullable
- error text nullable
- version integer
- created_at / updated_at timestamptz

### task_runs

- UUID primary key
- task_id UUID foreign key
- thread_id varchar unique
- agent_id varchar
- status varchar
- output JSONB nullable
- error text nullable
- started_at / completed_at timestamptz

`tasks.current_run_id` 的外键在 MVP 中不建立，以避免 tasks 与 task_runs 的循环建表依赖；一致性由同一事务中的 Repository 维护。后续迁移可以补充延迟约束。

## 6. Unit of Work

Application Service 只依赖 UnitOfWork Port：

- 进入时创建 Session。
- Repository 使用该 Session。
- 成功显式 commit。
- 异常自动 rollback。
- 离开时释放连接。

测试使用 InMemory UnitOfWork，不需要 PostgreSQL。

## 7. Migrations

- 业务表只通过 Alembic 迁移。
- LangGraph Checkpoint 初始化使用官方 `setup()`。
- 应用启动可以在开发环境检查 Schema，但生产迁移应作为独立部署步骤执行。
- Docker Compose 的启动命令先执行 Alembic，再启动 API。

## 8. Consistency boundaries

MVP 有两个明确事务：

1. 创建 Run 并将 Task 标记为 RUNNING。
2. 在 Workflow 返回后标记 COMPLETED 或 FAILED。

模型和工具执行位于事务之外。跨事务崩溃窗口是已知限制，将通过 Outbox、Worker 租约和 Reconciler 在下一阶段关闭。

## 9. Future extension points

- Transactional Outbox 和领域事件表。
- Worker lease、heartbeat 和 recovery deadline。
- Artifact metadata 表和对象存储引用。
- 租户字段与 Row-Level Security 评估。
- 将 Checkpoint 迁入独立 Schema。
- 数据保留、归档和删除策略。

## 10. Acceptance criteria

- Alembic 可以从空数据库建立业务表。
- Repository 可以创建、锁定、查询和更新 Task/Run。
- Domain Entity 不暴露 SQLAlchemy Model。
- LangGraph Checkpointer 初始化不修改业务表。
- 单元测试不依赖外部数据库，集成测试可选择 PostgreSQL。
