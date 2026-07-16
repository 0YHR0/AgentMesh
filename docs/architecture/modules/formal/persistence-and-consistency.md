# Persistence and consistency

Status: Proposed
Owners: Data platform maintainers
Depends on: [Cross-module contracts](cross-module-contracts.md), [Task domain](task-and-execution-domain.md)

## 1. Problem

AgentMesh 同时包含业务账本、LangGraph Checkpoint、对象存储、队列和遥测。正式版必须明确各自所有权，并在数据库提交、事件发布、Worker 执行和外部副作用之间实现可恢复的最终一致性。

## 2. Responsibilities

- 定义 PostgreSQL schema、事务、锁、版本和迁移边界。
- 提供 Unit of Work、Outbox、Inbox、Idempotency、Lease 和 Audit 基础能力。
- 定义 Checkpoint 与业务 Run 的绑定和 Reconciliation 机制。
- 定义备份、保留、归档、删除和读模型刷新原则。

## 3. Non-responsibilities

- 不把跨系统操作包装成分布式事务。
- 不让 Redis、Langfuse 或对象存储成为 Task 状态真相。
- 不允许模块直接写另一个模块拥有的表。
- 不规定每张表的全部 L3 DDL。

## 4. PostgreSQL logical schemas

| Schema | Owner | Examples |
|---|---|---|
| `task` | Task Service | tasks、subtasks、runs、attempts、handoffs、criteria |
| `registry` | Agent/MCP/A2A Registry | definitions、versions、peers、capability snapshots |
| `artifact` | Artifact Service | artifacts、versions、blobs、access grants、scan results |
| `policy` | Policy/Approval | bundles、decisions、approvals、action intents |
| `identity` | Identity/Tenancy | tenants、principals、role bindings、secret refs |
| `event` | Platform data layer | outbox、inbox、idempotency records、audit entries |
| `projection` | Query consumers | denormalized list/dashboard views |
| `langgraph` | Checkpointer adapter | official Checkpointer-managed tables |

初期可位于同一 Database，但使用独立数据库角色和 search path。生产 API migration 与 LangGraph `setup()` 分开执行；业务 migration 不修改 Checkpointer 内部表。

## 5. Unit of Work

每个写用例遵循：

1. 从可信上下文固定 tenant/principal。
2. 查询 IdempotencyRecord；存在则返回已保存结果。
3. 加载拥有模块的聚合并执行领域命令。
4. 保存聚合、StateTransition、AuditEntry 和 OutboxEvent。
5. 保存 idempotency outcome。
6. 单次事务 commit。

事务中禁止模型调用、MCP/A2A 网络调用、对象内容上传和等待队列。

## 6. Outbox

Outbox row 至少包含 envelope、aggregate identity/version、partition key、visibility、available_at、attempt count、published_at 和 last error。写业务状态与 Outbox 必须同事务。

Relay 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 批量领取；发布成功后标记 published。若 publish 成功但标记前崩溃，会重复发布，因此所有消费者必须 Inbox 去重。

Outbox 不是永久审计库。达到保留期后可归档/删除；AuditEntry 和业务历史独立保留。

## 7. Inbox and idempotency

- Inbox key：tenant + consumer + message_id。
- 业务幂等 key：tenant + command scope + idempotency key。
- 外部 webhook key：peer/server + remote delivery identity；无稳定 ID 时记录 payload digest 和短期窗口。
- Inbox 插入与消费结果在同一事务，保证“业务变化发生则消息已记为处理”。
- 失败可重试时不标 completed；永久拒绝保存 rejection outcome，避免 poison message 无限重试。

IdempotencyRecord 保存请求 hash。相同 key 不同请求 hash 返回冲突，而不是复用旧结果。

## 8. Concurrency control

- 默认乐观版本列和 compare-and-swap。
- 竞争性终态、租约和预算扣减使用行锁或原子更新。
- Lock order 固定：Task → Subtask → Run → Attempt；批量锁按 UUID 排序。
- 不跨网络持有锁；长事务阈值触发监控。
- Scheduler 用 fencing token 防止过期 Worker 提交结果。
- 对高吞吐计数使用 append ledger/分片 counter，避免热点 Task 行反复更新。

## 9. LangGraph binding

业务 `RunWorkflowBinding` 保存：run_id、thread_id、graph name/version、checkpoint namespace、last observed checkpoint ID/step、resume token version 和状态摘要。

规则：

- 一个 Run 默认对应一个 Thread；fork 创建新 Run/Thread 并保存 parent checkpoint reference。
- Checkpoint State 只保存恢复执行所需的 JSON 可序列化值和 ArtifactRefs，不保存大型内容、凭证或 ORM 对象。
- Graph version 与 Run 绑定；进行中的 Thread 不自动切换 graph definition。
- `interrupt()` 前的副作用必须幂等或拆为独立节点，因为恢复会从节点起点重放。
- Checkpointer 可用性是 Worker readiness 条件，不允许静默降级为内存。

## 10. Reconciliation

Reconciler 定期扫描：

- active Run 无有效 lease/heartbeat。
- Run 显示 RUNNING 但 Checkpoint 长时间无推进。
- Checkpoint 已终止而 Run 未终态。
- Artifact 已保存但对应 Attempt 未确认。
- remote task 终态但内部仍 WAITING_REMOTE。
- outbox/inbox 超过重试或时延阈值。

Reconciler 生成带幂等键的正常命令；所有自动修复记录 reason 和 source snapshot。无法确定外部副作用结果时转 `OUTCOME_UNKNOWN` 和人工队列。

## 11. Object storage consistency

上传采用两阶段应用协议：先创建 PENDING ArtifactVersion 和 upload grant；客户端上传临时对象；Finalize 校验哈希/大小后原子标记并发事件。孤儿临时对象由 TTL 清理。

数据库 commit 后对象删除失败不会恢复业务行；版本先标记 deletion pending，后台删除内容并记录结果。受 legal hold 的内容不可删除。

## 12. Read models

- 强一致命令后查询可直接读取 owner schema。
- 列表、仪表盘、全文检索和统计使用 projection schema。
- Projection consumer 保存 cursor/inbox，允许重复重建。
- API 返回 `projection_updated_at` 或 lag hint，避免把延迟视为状态丢失。
- 关键审批和取消页面不得只依赖明显滞后的缓存。

## 13. Migration strategy

1. Expand：新增 nullable 字段/表/索引，保持旧代码可运行。
2. Backfill：限速、可恢复、记录 cursor。
3. Dual read/write 或切换 feature flag。
4. Contract：确认无旧版本实例和旧事件后收紧/删除。

数据库 migration 由独立 Job 执行并使用 advisory lock；API/Worker 启动只做版本兼容检查。事件 upcaster 与数据库 migration 独立版本化。

## 14. Failure model

| Window | Outcome |
|---|---|
| DB commit 前崩溃 | 无事实提交，命令重试 |
| DB commit 后、Outbox publish 前 | Relay 后续发布 |
| publish 后、published 标记前 | 重复发布，Inbox 去重 |
| 消费业务 commit 后、ack 前 | 重复投递，Inbox 返回已处理 |
| Checkpoint commit 后、业务更新前 | Reconciler 发完成/等待命令 |
| 外部副作用成功但响应丢失 | 查询 external operation ID；不能确认则 outcome unknown |
| PostgreSQL 不可用 | 写接口和 Worker 停止领取新工作；只读按策略失败或短暂使用明确陈旧缓存 |

## 15. Security

- 数据库角色按 process/schema 最小授权；Migration role 与 Runtime role 分离。
- tenant_id 是所有业务主键/索引和查询条件的一部分；正式多租户阶段评估并启用 RLS 作为第二道防线。
- 敏感字段使用应用层 envelope encryption，密钥来自外部 KMS/secret manager。
- AuditEntry 采用 append-only 权限、哈希链/周期签名或外部 WORM 导出增强篡改检测。
- 备份与 replica 使用同等级加密、访问控制和保留策略。

## 16. Observability

指标：事务延迟/冲突/死锁、连接池、Outbox lag、Inbox duplicate rate、projection lag、migration/backfill、checkpoint size/step、reconciliation counts、orphan object。

日志不输出 SQL 参数中的敏感正文。慢查询按 fingerprint 聚合。数据库 Trace span 与业务 correlation 关联但不记录密钥或完整 Prompt。

## 17. Capacity and partitioning

- 高频索引以 tenant + status/updated_at、task + sequence 为主。
- Outbox、audit、state transition 和高量 invocation 表按时间或 tenant 哈希分区候选。
- Checkpoint 大小设置软/硬阈值；大 channel 使用 ArtifactRef，必要时评估增量 channel。
- 连接池总量由所有副本统一预算，不能让每个 Worker 使用默认大池。
- 清理任务按小批量执行，避免长锁和复制延迟。

## 18. Testing

- PostgreSQL 真数据库集成测试覆盖锁、版本、SKIP LOCKED、JSONB、RLS 候选和 migration。
- 故障注入覆盖每个 commit/publish/ack 崩溃窗口。
- 从空库升级、前一正式版本升级和 rollback-compatible window 均在 CI 验证。
- Reconciler 使用构造的不一致状态验证只发合法幂等命令。
- 备份恢复演练验证业务 schema、Checkpointer 和对象元数据可共同恢复到一致时间点。

## 19. Acceptance criteria

- 任一业务事实与 Event 同事务提交。
- 重复 Event/Command 不产生重复 Run、Approval、Artifact 或副作用。
- Checkpoint 与业务状态不一致时有确定的自动或人工收敛路径。
- schema owner、migration role、runtime role 和保留策略明确。
- 从空数据库和上一版本数据库都能完成可验证升级。
