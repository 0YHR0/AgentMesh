# Control API

Status: Proposed
Owners: Control plane maintainers
Depends on: [Task domain](task-and-execution-domain.md), [Identity and tenancy](identity-tenancy-and-secrets.md), [Cross-module contracts](cross-module-contracts.md)

## 1. Problem

正式版需要向 Web Console、CLI、自动化和外部系统提供稳定 API，同时隐藏领域存储、LangGraph、Redis、A2A 和 MCP 实现。长任务必须与 HTTP 请求生命周期解耦，并支持幂等、分页、实时状态和安全错误。

## 2. Responsibilities

- 认证请求并建立可信 Principal/Tenant/Trace context。
- 校验请求 schema、大小、版本、idempotency 和 precondition。
- 将命令映射到领域 Application Service，将查询映射到权威聚合/投影。
- 返回异步 operation/resource reference，不同步等待长 Run。
- 提供 SSE 实时事件和受控 webhook 管理。
- 统一错误、分页、过滤、版本和兼容策略。
- 实施 API rate limit、audit 和 OpenAPI/documentation。

## 3. Non-responsibilities

- 不包含领域状态转换、Agent 路由或 Policy 规则。
- 不直接查询/修改 LangGraph Checkpoint 或 Redis。
- 不代理大型 Artifact 正文。
- 不把内部 domain event 原样公开为永久公共契约。
- 不用 WebSocket/SSE ack 替代业务 commit。

## 4. API styles

- Resource query：GET Task、Run、Agent、Artifact、Approval 等。
- Command：POST create 或 `:pause/:resume/:cancel/:approve` 等显式动作，返回 command result。
- Async operation：耗时管理动作返回 `202` + Operation resource。
- Event stream：SSE 投影业务进度；客户端可断线恢复。
- Webhook：租户配置的外部通知，使用独立签名/重试，不接收任意内部 Event。

公共 `/api/v1` 与内部 `/internal/v1` 分开；内部 API 需要 workload identity，不能仅靠网络位置。

## 5. Resource surface

正式 v1 最小资源族：

- Tasks、Subtasks、Runs、Attempts、Handoffs、Criteria。
- Approvals、Policy decision summaries。
- Artifacts、Versions、Upload sessions、Access grants。
- Agent Definitions、Versions、Deployments、Instances/health。
- MCP Servers/Versions/Capabilities 和 A2A Peers/Cards。
- Operations、Audit entries、Usage/Score summaries。
- Event subscriptions/webhooks。

具体 L3 OpenAPI 可拆分发布；L2 要求资源 ID、tenant 和版本语义保持一致。

## 6. Task command semantics

代表性命令：create、request plan/run、pause、resume、cancel、provide input、request revision、accept result。执行命令返回：

- `200/201`：业务变化已提交并返回资源。
- `202`：命令已接受，返回 Operation/Run reference 和 status URL。
- `409`：状态/precondition/idempotency hash 冲突。
- `422`：schema/业务输入不合法。

创建 Run 默认 `202 Accepted`。短 deterministic demo 可以显式 `wait` 参数或专用 endpoint，但设置严格超时；超时后 Run 继续，不返回 500。

## 7. Idempotency and preconditions

- 所有 create/command 接受 `Idempotency-Key`，作用域为 tenant + principal/client + route/command。
- 服务保存 canonical request hash、status/result reference 和 expiry。
- 同 key 同请求返回首次结果；同 key 不同请求返回 `409 idempotency_conflict`。
- 更新可接受 `If-Match`/resource version，避免 UI 覆盖新状态。
- retryable 5xx/timeout 使用同 key；客户端不得为同一业务动作每次生成新 key。
- webhook/内部命令使用 Envelope idempotency，不依赖 HTTP header 单独存在。

## 8. Query and pagination

- 列表使用 opaque cursor，排序键稳定且包含唯一 ID；不使用大 offset。
- filter/sort allowlist，禁止把任意字段/SQL 暴露为查询语言。
- 返回 `projection_updated_at` 和可选 lag；Task detail/Approval action 可读取权威状态。
- field selection/expand 有数量和深度上限，避免 N+1 和超大响应。
- 搜索结果包含权限过滤后的 hit，不允许先搜索后在应用层隐藏造成侧信道。
- 导出是异步 Operation + Artifact，不在一个 HTTP 响应返回大量数据。

## 9. Realtime events

SSE 为首选 server-to-client 通道：

- 客户端订阅 tenant/project/task scope，服务验证每个 scope。
- 事件是公共 `TaskViewChanged`、`ApprovalChanged`、`RunProgressChanged` 等投影，不直接暴露 internal Event payload。
- 每 event 有 public event ID/cursor、type、resource/version、occurred_at 和最小 patch/ref。
- 支持 `Last-Event-ID` 受限重放；超出窗口返回 resync required，客户端重新查询。
- heartbeat 保持连接；代理/浏览器断线使用 backoff+jitter。
- 事件可能重复，客户端按 event/resource version 幂等。

双向低延迟交互确有需求时再增加 WebSocket；命令仍走正常 HTTP/Application Service。

## 10. External webhooks

- 只能选择 allowlisted public event types 和租户资源 scope。
- endpoint 创建需 URL 验证、防 SSRF 和 challenge；禁止 loopback/private IP/redirect 逃逸。
- payload 版本化、签名、timestamp/replay window、delivery ID。
- 至少一次投递，独立 retry/DLQ；接收方按 delivery/event ID 去重。
- secret 保存 SecretReference，支持 rotation/current+previous 短窗口。
- webhook failure 不阻塞 Task 完成，除非业务明确将通知建模为 required Subtask。

## 11. Authentication, authorization, security and tenancy

- 人类/API 身份按 Identity 模块；每请求重新确定 tenant，禁止从 JSON body 选择任意 tenant。
- 路由级 scope + resource-level Policy；列表也必须过滤。
- Agent/Worker 使用 internal audience，不得调用 tenant admin endpoint。
- 高风险 command 要求 assurance/step-up 和可选 reason。
- CORS default deny，Console origin allowlist；cookie session 使用 CSRF protection。
- rate limit 分 IP/client/principal/tenant/route，返回 retry hints。

## 12. Error model

使用统一 error contract：code、category、safe message、retryable/retry_after、correlation、field details。代表性 code：

- `resource_not_found`
- `invalid_state_transition`
- `concurrent_update`
- `idempotency_conflict`
- `policy_denied`
- `approval_required`
- `quota_exceeded`
- `dependency_unavailable`
- `projection_resync_required`

404/403 是否隐藏资源存在按安全策略一致处理。内部堆栈、provider response、Prompt、token 和 secret 永不返回。

## 13. Versioning and compatibility

- URL major version；响应 schema 内可带 resource version/revision。
- 新增可选字段兼容；枚举客户端必须容忍 unknown。
- 删除/改义使用新 major 或长弃用期。
- OpenAPI、examples、SDK 和 contract fixtures随 release 发布。
- `Deprecation`/`Sunset` headers 和 changelog 通知客户端。
- API version 与 A2A/MCP/Event schema 独立。

## 14. Failure and overload

- Task Service/DB 不可用：写/关键读返回明确 503，不接受未持久化命令。
- Redis/Event stream 不可用：命令仍可提交；SSE 明确 unavailable，客户端轮询。
- Projection lag：返回 freshness，关键 detail fallback owner schema。
- Dependency timeout：API request 取消不取消已提交 Run。
- overload：优先 health/cancel/approval/system recovery；新批量任务 429/503。
- graceful shutdown：停止接入新请求，完成短事务，关闭 SSE 并提示 reconnect。

## 15. Observability and capacity

指标：request rate/latency/status、auth/policy、idempotency hit/conflict、command accept、SSE connections/replay/resync、projection lag、rate limit、payload size。

日志：route template、principal type、tenant、resource ID、command/idempotency hash、correlation，不记录 authorization header/body sensitive fields。

限制：request/response/body、batch size、filter complexity、expand depth、SSE connections per tenant、webhook endpoints 和 export frequency。

## 16. Testing

- OpenAPI schema compatibility/golden fixtures。
- authn/authz/cross-tenant/list-filter/IDOR、CSRF/CORS/rate limit 安全测试。
- duplicate idempotency、If-Match、timeout-after-commit、202 polling。
- SSE duplicate/disconnect/Last-Event-ID/resync 和 Redis outage。
- error redaction、oversized/invalid JSON/content type、unknown enum。
- previous supported client contract suite。

## 17. Acceptance criteria

- 所有长任务在业务 commit 后返回 202，不依赖 HTTP 连接完成执行。
- 任一重试 command 可用相同 idempotency key 获得稳定结果。
- Web Console 不需要读取 LangGraph/Redis/Langfuse 私有 API获得业务状态。
- API、列表、SSE 和 webhook 均执行 tenant/resource policy。
- OpenAPI 与 contract tests 能阻止意外破坏 v1 客户端。
