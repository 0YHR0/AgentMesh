# Control API

Status: Accepted for MVP
Owners: AgentMesh maintainers
Depends on: [Task execution model](task-execution-model.md)

> Historical bootstrap baseline. The current implementation is defined by
> [Durable asynchronous execution](durable-async-execution.md); the formal target is
> [Control API](formal/control-api.md).

## 1. Problem

需要一个最小、稳定的 HTTP 边界来创建任务、启动执行和读取结果，同时避免把 SQLAlchemy、LangGraph 或外部协议对象暴露为公共 API。

## 2. Responsibilities

- 验证请求和响应 Schema。
- 将 HTTP 请求映射为 Application Service 命令与查询。
- 将领域错误映射为稳定的 HTTP 错误。
- 提供健康和就绪检查。
- 注入请求关联信息。

## 3. Non-responsibilities

- 不包含任务状态转换规则。
- 不直接操作 SQLAlchemy Session。
- 不直接 invoke LangGraph。
- 不在 MVP 中提供认证、多租户或实时 SSE。

## 4. MVP endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | 进程存活检查 |
| GET | `/ready` | 数据库与 Runtime 就绪检查 |
| POST | `/api/v1/tasks` | 创建 Task |
| GET | `/api/v1/tasks` | 分页列出 Task |
| GET | `/api/v1/tasks/{task_id}` | 查询 Task 和 Runs |
| POST | `/api/v1/tasks/{task_id}/runs` | 同步启动一次 Run |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消未完成 Task |
| POST | `/api/v1/tasks/{task_id}/pause` | 持久化暂停或请求安全边界暂停 |
| POST | `/api/v1/tasks/{task_id}/resume` | 恢复同一 Run/Thread 并重新投递 |
| GET | `/api/v1/tasks/{task_id}/tool-invocations` | 查询脱敏的 MCP Tool 调用审计 |

MVP 的 Run 接口同步等待 Workflow 完成，只适合短任务。异步 `202 Accepted`、Worker 队列和实时事件将在下一阶段加入，但 Application Service 接口不会依赖同步 HTTP 生命周期。

## 5. Error mapping

| Domain/Application error | HTTP status |
|---|---|
| TaskNotFound | 404 |
| InvalidTaskTransition | 409 |
| InvalidInput | 422 |
| ConcurrentUpdate | 409 |
| Unexpected execution failure | 500，且不返回内部堆栈或密钥 |

错误响应包含稳定 `code`、人类可读 `message` 和可选 `details`。

## 6. Versioning

- 公共 API 从 `/api/v1` 开始。
- Domain Event 和 A2A/MCP 版本不与 HTTP API 版本绑定。
- 新增可选字段优先保持向后兼容。
- 枚举扩展需要客户端按未知值安全处理。

## 7. Dependency direction

```text
FastAPI routes
    -> TaskApplicationService
        -> UnitOfWork Port
        -> WorkflowRunner Port

Infrastructure adapters implement the ports and are assembled in bootstrap.
```

## 8. Acceptance criteria

- OpenAPI 能展示全部 MVP Endpoint。
- API 测试可使用 InMemory UnitOfWork，不启动 PostgreSQL。
- 领域冲突返回 409，而不是 500。
- Task 输出中包含 Runs，便于首版观察执行历史。
- API 模块不导入 SQLAlchemy Models 或 LangGraph Graph。
