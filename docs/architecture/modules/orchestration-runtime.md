# Orchestration and Agent Runtime

Status: Accepted for MVP
Owners: AgentMesh maintainers
Depends on: [Task execution model](task-execution-model.md)

> Historical bootstrap baseline. The current implementation is defined by
> [Durable asynchronous execution](durable-async-execution.md); the formal targets are
> [Orchestrator and scheduler](formal/orchestrator-and-scheduler.md) and
> [Local Agent Runtime](formal/local-agent-runtime.md).

## 1. Problem

首版需要证明 Agent 执行确实由 LangGraph 驱动、能够获得稳定 Thread ID，并且业务层不依赖具体 Agent 或模型实现。

## 2. Responsibilities

- 将 Task 输入映射为 LangGraph State。
- 通过 AgentExecutor Port 调用一个本地 Agent。
- 使用 Checkpointer 保存工作流步骤。
- 返回可保存的结构化结果。
- 传播 task_id、run_id、thread_id 和 Trace metadata。

## 3. Non-responsibilities

- 不直接修改 Task 数据库状态。
- 不选择远程 A2A Agent。
- 不提供 MCP Gateway。
- 不在 MVP 中执行多 Agent 路由、Reviewer 或人工 Interrupt。

## 4. Ports

### AgentExecutor

```text
execute(objective, input, context) -> AgentResult
```

`context` 至少包含 task_id、run_id、thread_id 和 agent_id。MVP 使用 DeterministicAgentExecutor，因此无需模型密钥即可运行。后续模型 Agent、MCP Agent 和 A2A Agent 都实现相同上层语义或由 Adapter 转换。

### Checkpointer

WorkflowRunner 接受 LangGraph BaseCheckpointSaver。生产使用 PostgresSaver，测试使用 InMemorySaver。

## 5. MVP graph

```mermaid
flowchart LR
    START --> EXECUTE["execute_agent"] --> END
```

Graph State:

- task_id
- run_id
- objective
- input
- agent_id
- output

MVP 图刻意简单，但它建立了 State、Node、Checkpoint 和 Thread 约定。未来可以在不改变 Task API 的前提下增加 plan、review、approval 和 handoff 节点。

## 6. Thread and trace mapping

- 每个 Run 创建唯一 thread_id。
- MVP 使用 `str(run_id)` 作为 thread_id 值。
- task_id 作为 Langfuse session_id 候选。
- run_id 作为 Trace metadata 和运行关联键。
- Workflow `run_name` 固定为 `agentmesh-task-run`。

Langfuse 未配置时 Workflow 正常运行；配置后通过 CallbackHandler 接入。

## 7. Failure model

- AgentExecutor 异常向 Application Service 传播，由业务层标记 FAILED。
- Checkpointer 失败视为 Run 失败，不能静默降级为无持久化运行。
- AgentResult 必须是 JSON 可序列化对象，否则在边界处拒绝。
- 未来重试应创建新的 Run，而不是使用同一 Thread 重新开始。

## 8. Extension strategy

- `execute_agent` 可替换为 Agent 子图。
- Reviewer 作为独立节点和条件边。
- MCP 通过 AgentExecutor 内的 Tool Port 接入。
- A2A 通过 RemoteAgentExecutor Adapter 接入。
- 人工审批通过 `interrupt()` 增加，不改变业务 Task 身份。
- Scheduler 位于 WorkflowRunner 之前，不嵌入 Graph State。

## 9. Acceptance criteria

- 同一 Run 始终使用同一 thread_id。
- 测试可注入 InMemorySaver 和 Fake AgentExecutor。
- 生产配置使用 PostgresSaver。
- 不配置 Langfuse 时没有外部网络依赖。
- Graph 输出可以被 Task Application Service 保存。
