# Cross-module contracts

Status: Proposed
Owners: Platform architecture
Depends on: [Formal L2 baseline](README.md)

## 1. Problem

AgentMesh 的模块通过进程内 Port、HTTP、Redis Streams、A2A、MCP 和对象存储协作。如果每个模块分别定义身份、幂等、关联、Artifact 和错误字段，重复投递与协议升级会造成状态无法收敛。本设计定义与传输技术无关的逻辑契约。

## 2. Responsibilities

- 定义 Command、Event 和异步 Job 的统一 Envelope。
- 定义 Principal、ArtifactRef、Assignment、Handoff、Approval、PolicyDecision 等共享值对象。
- 定义版本、兼容、幂等、追踪、错误和扩展字段规则。
- 定义外部协议对象进入内部领域前必须保留的关联信息。

## 3. Non-responsibilities

- 不规定所有 HTTP 路径、SQL 列或 Python 类。
- 不把 A2A/MCP Schema 原样复制为内部模型。
- 不选择具体序列化库或 Schema Registry 产品。
- 不定义各领域实体的状态转换。

## 4. Base envelope

所有跨进程 Command、Event 和 Job 至少包含：

| Field | Required | Semantics |
|---|---:|---|
| `schema_name` | yes | 稳定契约名，例如 `task.run.requested` |
| `schema_version` | yes | 独立语义版本，推荐整数主版本 |
| `message_id` | yes | 全局唯一消息 ID，用于 Inbox 去重 |
| `tenant_id` | yes | 租户边界；系统维护任务使用显式 system tenant |
| `occurred_at` | yes | Event 发生或 Command 创建的 UTC 时间 |
| `producer` | yes | 服务、模块和版本 |
| `actor` | yes | 发起 Principal；机器操作不得伪装成人 |
| `correlation_id` | yes | 通常是顶层 `task_id` 或入口请求 ID |
| `causation_id` | no | 直接导致本消息的 command/event/message ID |
| `idempotency_key` | command/job | 在明确作用域内稳定的业务去重键 |
| `trace_context` | no | W3C `traceparent`、`tracestate` 和受控 baggage |
| `expires_at` | no | 过期后不得开始执行的新工作 |
| `payload` | yes | 由 `schema_name/version` 定义的内容 |
| `extensions` | no | 命名空间化扩展；核心消费者必须可忽略未知扩展 |

Envelope 元数据不应复制到模型 Prompt；Runtime 只选择执行所需的最小上下文。

## 5. Command contract

Command 使用祈使语义，例如 `StartRun`、`CancelTask`、`ResumeWorkflow`。处理结果只有三类：

- `accepted`：命令已持久化，可能尚未完成。
- `rejected`：前置条件、授权、策略或输入不满足，不应原样重试。
- `duplicate`：同一幂等键已处理，返回首次处理的稳定引用。

Command handler 必须在同一事务中完成：检查幂等记录、校验状态、修改业务实体、写 Outbox、保存处理结果。网络响应丢失时，调用方用相同 idempotency key 查询或重试。

## 6. Domain event contract

Event 使用过去式，例如 `TaskCreated`、`RunLeased`、`ApprovalGranted`。额外字段包括：

| Field | Semantics |
|---|---|
| `aggregate_type` / `aggregate_id` | 事件所属聚合 |
| `aggregate_version` | 事务提交后的聚合版本 |
| `event_sequence` | 同一聚合单调递增序号 |
| `visibility` | `internal`、`tenant`、`audit` 或 `public-webhook` |

消费者按 `message_id` 去重，并使用 aggregate version/sequence 检测乱序。检测到缺口时不得猜测状态，应重新读取权威聚合或触发回补。

## 7. Principal context

`PrincipalContext` 至少包含：

- `principal_id`
- `principal_type`: `user | service | agent | external_peer`
- `tenant_id`
- `subject`：外部 IdP 的稳定 subject，可选
- `roles`：入口处解析的角色快照
- `delegation_chain`：用户 → 服务 → Agent → Tool/Peer 的链路
- `authentication_strength`：认证方式或 assurance level
- `request_ip/device`：仅在策略允许且确有用途时记录

角色快照仅用于审计，授权必须基于当前策略或带有效期的签名授权上下文，不能永久信任旧消息中的角色。

## 8. Artifact reference

跨模块只传 `ArtifactRef`，不传大型内容：

| Field | Meaning |
|---|---|
| `artifact_id` / `version_id` | 业务身份和不可变版本 |
| `media_type` / `kind` | MIME 类型和平台分类 |
| `size_bytes` | 内容大小 |
| `sha256` | 内容完整性 |
| `storage_class` | managed、external-reference、inline-small |
| `locator` | 受控对象键或外部引用 ID，不是永久公开 URL |
| `classification` | public/internal/confidential/restricted |
| `scan_status` | pending/clean/rejected/unknown |
| `producer_run_id` | 来源 Run |
| `expires_at` | 临时引用有效期，可空 |

消费者在使用前重新授权并检查 scan/classification；签名下载 URL 按请求临时生成，不能进入 Event 或 Checkpoint。

## 9. Agent assignment

`AgentAssignment` 是调度器对一次 Run 的不可变选择记录：

- assignment ID、Run/Attempt ID
- Agent Definition ID + immutable version
- Agent Instance/remote peer 候选，可在 lease 前为空
- execution mode 和 required capabilities
- tool/policy profile version
- input ArtifactRefs 和 expected output contract
- budget slice、deadline、priority
- lease duration、heartbeat interval
- routing reasons 和 score breakdown

修改 Agent、预算或策略必须创建新 Assignment/Attempt，不能覆盖历史。

## 10. Handoff contract

Handoff 至少包含：

- source/target work item 和 source/target Agent
- objective、reason、required capability
- completed work summary 和 unresolved questions
- input ArtifactRefs，不复制完整对话
- constraints、acceptance criteria、budget/deadline remainder
- requested/accepted/rejected timestamps and actors
- parent Run、Trace 和 causation IDs

目标 Agent 可以接受、拒绝或请求澄清。接受 Handoff 不等于 Task 完成，也不自动授权源 Agent 拥有的工具。

## 11. Policy decision and approval

`PolicyDecision`：

- decision ID、policy bundle/version
- subject、action、resource、environment attributes
- result: `allow | deny | require_approval | allow_with_constraints`
- obligations/constraints，例如脱敏、最大金额、限定工具参数
- reason codes、evaluated_at、expires_at
- canonical action hash

`ApprovalRequest` 绑定 decision ID、action hash、风险、证据、到期时间和允许的决策者集合。`ApprovalDecision` 包含 approve/reject/modify、actor、理由和替代参数。任何动作参数变化都会使旧批准失效并重新评估。

## 12. MCP invocation audit

每次 MCP 调用形成 `ToolInvocationRecord`：

- invocation ID、server registration/version、tool name/schema digest
- Agent、Task、Run、Attempt、Principal delegation chain
- policy decision/approval ID
- canonicalized argument hash；参数正文按数据策略保存或脱敏
- started/completed time、result classification、error category
- side-effect class、idempotency strategy、external operation ID
- input/output ArtifactRefs 和 Trace IDs

模型看到的 ToolResult 与审计记录分离，避免把内部凭证和策略细节反馈给模型。

## 13. A2A correlation

`RemoteTaskCorrelation` 使用内部 ID 与外部复合 ID 关联：

- peer ID + peer version/card digest
- internal Task/Subtask/Run/Attempt ID
- remote context ID、task ID、message IDs
- protocol version、binding、endpoint ID
- last remote state、last event cursor/version
- delivery mode: stream/push/poll
- created/updated/terminal timestamps

远程 ID 只在 peer scope 内唯一。重复回调按 peer + remote event identity 去重；没有事件 ID 时使用规范化 payload hash + remote state version 的受限去重策略。

## 14. Trace context propagation

- HTTP、队列、A2A 和 MCP 传播 W3C Trace Context。
- `task_id` 进入受控 baggage 之前需评估泄露风险；跨组织边界默认只传 opaque correlation ID。
- 新的异步消费者创建 linked span，而不是假设所有长任务保持一个无限长 Trace。
- Langfuse `session_id = task_id`；一个 Run 可以跨多个 Trace，业务表保存 Trace link 列表或首要 Trace ID。
- Trace 采样不能影响审计事件和业务状态保存。

## 15. Error contract

稳定错误对象包含：

- `code`：机器可读、版本稳定
- `category`: validation/authentication/authorization/conflict/rate_limit/transient/dependency/permanent/unknown
- `message`：安全的用户或调用者说明
- `retryable` 和可选 `retry_after`
- `correlation_id`
- 可选字段级 `details`，不得包含堆栈、Prompt、Token 或密钥

内部异常在边界映射；原始异常只进入受控日志/Trace，并执行脱敏。

## 16. Versioning and compatibility

- 新增可选字段属于向后兼容；删除、改义或收紧约束需要新主版本。
- Consumer 必须忽略未知可选字段和扩展，但不能忽略未知安全 obligation。
- Producer 在兼容窗口内支持当前和前一主版本；更长窗口由模块 SLO 决定。
- Event 不做原地重写；需要时通过 upcaster 在读取边界升级。
- 协议对象先解析为版本化 DTO，再映射为内部契约。
- Schema fixture 和 contract test 随代码发布，CI 验证向后兼容。

## 17. Capacity and limits

默认限制必须可配置并在入口、队列和 Runtime 三处一致执行：

- Envelope 最大 256 KiB；更大内容转 Artifact。
- extensions 总大小默认不超过 32 KiB。
- delegation chain 默认最多 8 跳。
- 单命令有效期默认不超过 24 小时；长任务用业务 deadline。
- error details 和 reason 列表需要数量与长度上限。

超过限制返回稳定错误，不得静默截断安全关键字段。

## 18. Security and trust

- Envelope 的 tenant、actor 和 delegation 只能由可信入口/producer 写入，消费者不得接受 payload 覆盖。
- Contract validator 对大小、深度、URL、media type 和 unknown security obligation fail closed。
- 外部 DTO 原文按 classification 保存并在进入内部 contract 前执行 schema/来源验证。
- 签名、credential、signed URL 和 secret value 只通过引用/受保护 channel 传递，不进入共享 Envelope。
- Schema/fixture 发布链需要代码评审、版本和 provenance，防供应链替换。

## 19. Observability

- 每个 Command/Event/Job 的处理日志和 Span 使用 message、correlation、causation、tenant、schema/version 和 result category。
- 监控 schema rejection、unknown version/obligation、idempotency conflict、duplicate、sequence gap 和 payload limit。
- payload 正文默认不采集；只记录 hash、size、classification 和受控字段。
- Trace 采样不影响 Inbox、Audit、StateTransition 或 idempotency outcome 的持久化。

## 20. Testing requirements

- 每个契约维护 canonical valid/invalid fixtures。
- 验证重复、乱序、未知字段、旧版本和缺失安全 obligation。
- Envelope 在 HTTP、Redis 和持久化往返后保持语义一致。
- ArtifactRef、Action hash 和 idempotency scope 有跨语言测试向量。
- A2A/MCP adapter 使用官方 conformance fixtures 或 mock peer/server 验证。

## 21. Acceptance criteria

- 所有正式模块只引用本文件定义的共享值对象，不创建冲突变体。
- 任一 Command/Event 可定位 tenant、actor、correlation、causation 和 schema version。
- 所有副作用契约显式声明幂等或不可自动重试。
- 外部协议 ID 不直接充当内部主键。
- 未知字段和版本不导致静默授权放宽。
