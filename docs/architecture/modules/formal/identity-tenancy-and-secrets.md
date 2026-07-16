# Identity, tenancy and secrets

Status: Proposed
Owners: Platform security maintainers
Depends on: [Cross-module contracts](cross-module-contracts.md)

## 1. Problem

AgentMesh 同时代表用户、服务、Agent 和外部 Peer 执行动作。正式版必须在整个委托链中保留真实身份、租户和最小权限，并让 Agent 使用目标系统的短期凭证而不是长期密钥。

## 2. Responsibilities

- 管理 Tenant、Project/Workspace、Principal、Group、RoleBinding 和 ServiceAccount。
- 将外部 OIDC/SAML/企业 IdP 身份映射为内部 PrincipalContext。
- 为服务/Worker/Integration 提供 workload identity。
- 管理 RBAC 基线和 Policy Engine 所需属性。
- 保存 SecretReference、CredentialBinding 和授权/同意元数据。
- 通过 Credential Broker 换取短期、目标 audience、最小 scope 凭证。
- 管理租户生命周期、隔离、配额归属和审计上下文。

## 3. Non-responsibilities

- 不实现完整企业 IdP。
- 不在 PostgreSQL 保存明文 secret/token/private key。
- 不让角色声明替代资源/动作级 Policy。
- 不把人类身份伪装成 Agent 或服务身份。
- 不向模型、Prompt、Artifact、Event 或 Checkpoint暴露凭证值。

## 4. Principal model

Principal 类型：

- `user`：由外部 IdP subject 映射。
- `service`：API、Worker、Relay、Scanner、Gateway 等 workload。
- `agent`：绑定 Agent Version + Run 的短期执行主体。
- `external_peer`：A2A Client/Server 或受信系统身份。

Principal 具有稳定内部 ID、tenant、status、external identities、display metadata。Email/username 不作为稳定主键。

`DelegationGrant` 表示 user/service 将有限动作委托给 Agent/服务：issuer、subject、audience、scopes/actions、resource constraints、run/task、issued/expiry、policy version、revocation status。

## 5. Tenant and workspace model

- Tenant 是安全、计费、配额和数据保留最高边界。
- Project/Workspace 是 Tenant 内协作和资源组织边界，不默认等于数据库 schema。
- 所有业务实体和异步 Envelope 必须含可信 tenant_id。
- tenant_id 由认证/路由上下文确定，payload 自报仅用于与上下文比对。
- system tenant 只用于平台维护元数据，不得容纳普通用户 Task。
- 跨 Tenant 共享通过显式 published resource + 双方 Policy/Grant，禁止直接外键访问另一租户私有资源。

## 6. Authentication

### Human/API clients

- Web Console 使用 OIDC Authorization Code + PKCE，优先 BFF/secure HttpOnly session，避免长期 token 存浏览器 localStorage。
- API automation 使用 service account、OAuth client credentials、workload identity 或短期 PAT；PAT 需 scope/expiry/hash storage/rotation。
- MFA/assurance level 进入 PrincipalContext，高风险审批可要求 step-up。

### Workloads

- 容器环境优先 workload identity/mTLS/SPIFFE 类机制或平台 service account。
- 内部 API/queue consumer 验证 audience、issuer、expiry 和 service role。
- 每个部署单元独立身份，禁止所有服务共享一个万能 token。

### External integrations

- A2A/MCP 各自遵循协议 security scheme，但先映射到内部 external_peer/service principal。
- callback/webhook 使用独立 audience、签名或 mTLS，并验证 replay window。

## 7. Authorization model

RBAC 提供粗粒度角色：Task Owner、Operator、Approver、Agent Author、Tool Provider、Tenant Admin、Platform Admin、Read-only Auditor。

最终授权为 RBAC + ABAC/Policy：subject、tenant/project、resource owner/classification、action、environment、delegation、risk。默认拒绝。

关键原则：

- 创建权限不等于执行权限，执行权限不等于批准权限。
- Agent Author 不能自动发布生产 Version；Tool Provider 不能自动批准高风险 Tool。
- Platform Admin 的基础设施权限不自动授予查看 restricted Prompt/Artifact 内容。
- 历史消息中的 role snapshot 仅审计，实时动作重新授权。
- list/query 强制 tenant/visibility filter，避免对象级接口正确但列表泄露。

## 8. Secret and credential model

`SecretReference`：provider、path/key ID、version selector、owner tenant、purpose、allowed audiences、rotation policy、status；不保存 value。

`CredentialBinding`：principal/agent/tool/peer 与 SecretReference 的受限关系、scope、resource/audience、consent owner、environment、expiry。

`CredentialLease`：运行时短期租约，包含 opaque lease ID、subject、audience、scope、run/invocation、issued/expiry；具体 token 仅在 Broker 和目标 adapter 的受保护内存中存在。

## 9. Credential broker flow

1. Runtime/Gateway 提交 authenticated principal、Assignment、target audience、scope、PolicyDecision/Approval。
2. Broker 验证 Binding、delegation、tenant、environment、risk 和 expiry。
3. 从 Secret Manager/OAuth issuer 换取短期凭证，尽量使用 token exchange/workload identity。
4. 凭证通过进程内安全 channel 或一次性 handle 交给目标 adapter。
5. Adapter 调用后丢弃值；只记录 lease/invocation ID、scope/audience 和结果。

Broker 不返回超出请求 audience/scope 的 token。refresh token/private key 只在 Secret Manager/Broker 边界。

## 10. Lifecycle and revocation

- Principal：ACTIVE → SUSPENDED → DEACTIVATED；删除保留审计 tombstone。
- Tenant：ACTIVE → SUSPENDED → CLOSING → CLOSED；关闭是长流程，先阻止新工作再处理保留/导出/删除。
- RoleBinding/Delegation/SecretBinding 都有 effective/expiry/revocation。
- 用户离职、Peer revoke、Agent Version revoke 或 Secret compromise 触发缓存失效、CredentialLease 停发和 active Run 影响分析。
- 短期已签发 token 无法即时撤销时依赖短 TTL、target revocation 和 gateway denylist。

## 11. Tenant isolation

- 应用层所有 Repository/query 强制 tenant scope。
- PostgreSQL RLS 在多租户正式阶段作为第二道防线，并由独立 integration tests 验证。
- 对象存储按 tenant/encryption domain 隔离 key/prefix，signed URL 绑定 tenant/resource。
- Redis stream message 含 tenant，consumer 在处理前与 workload scope 校验；缓存 key 必须 tenant prefix。
- Langfuse project/metadata、metrics labels 和日志访问按 tenant policy 隔离。
- sandbox、MCP stdio process、memory namespace 和 vector/search index 同样隔离。

## 12. Failure model

| Failure | Behavior |
|---|---|
| IdP unavailable | 已验证短会话按 TTL 可继续；新登录/step-up 失败，不降级匿名 |
| Policy/identity DB unavailable | 高风险 fail closed；只读缓存需显式 freshness |
| Secret Manager unavailable | 不发新 credential，现有短 lease 到期；Run 保持可恢复等待并记录 dependency reason |
| token expired during call | 安全 refresh/retry；non-idempotent call 先确认 outcome |
| principal/secret revoked | 主动失效 cache，阻止新工作，扫描 active assignments |
| tenant mismatch | 拒绝、审计并触发安全告警，不自动纠正 payload |

## 13. Security and privacy

- 密码、token、key、signed URL 使用统一 secret redaction，支持 entropy/pattern scanner。
- 审计记录“谁使用了哪个 secret ref/lease 调什么 audience”，不记录值。
- break-glass credentials 保存在独立 vault，双人控制、短期、即时告警。
- PII 最小化；display profile 与安全 subject 分离，按保留策略删除。
- session 防 CSRF/fixation，API token 防 replay，service credentials 定期轮换。
- 管理跨 tenant 资源需要显式 platform scope 和 reason，不允许仅靠隐藏 UI。

## 14. Observability

指标：login/auth failure、token exchange latency/error、credential lease、scope step-up、revocation propagation、cross-tenant deny、stale role cache、secret rotation age、break-glass use。

安全日志包含 principal type/id、tenant、action/resource、auth method/assurance、decision ID、correlation；IP/device 按隐私策略采集。禁止记录 credential value 和完整 authorization header。

## 15. Capacity and limits

- RoleBinding/Group expansion 有深度和数量上限，解析结果短 TTL cache。
- CredentialLease TTL 默认分钟级且不超过 Run/Approval/Binding expiry。
- 每 principal/client/token exchange 和 failed auth 有速率限制。
- Tenant suspended 必须在配置 SLO 内传播到 API、Worker、Gateway 和 Broker。
- Secret rotation 支持 current+previous 短重叠窗口，禁止永久多版本有效。

## 16. Testing

- object/list 跨 tenant、RLS、cache key、Artifact/Trace/queue isolation tests。
- role/delegation/expiry/revoke/step-up property tests。
- token audience/scope confusion、passthrough、CSRF/session fixation、replay 安全测试。
- Secret Manager outage、rotation、previous version overlap 和 active Run impact。
- external Peer 与 workload identity mapping contract tests。

## 17. Acceptance criteria

- 任一受控动作可还原 human/service/agent/external delegation chain。
- 任何模块都不能从数据库获取明文 secret。
- Agent 获得的凭证绑定 Run、target audience、最小 scope 和短 expiry。
- list、query、Artifact、queue、Trace 和对象存储都通过跨租户测试。
- revoke 在定义的传播 SLO 内阻止新访问并列出受影响 active work。
