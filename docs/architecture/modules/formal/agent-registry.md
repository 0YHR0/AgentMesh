# Agent Registry

Status: Proposed
Owners: Agent platform maintainers
Depends on: [Cross-module contracts](cross-module-contracts.md), [Identity and tenancy](identity-tenancy-and-secrets.md)

## 1. Problem

调度器需要可靠回答“哪个版本的 Agent 能做什么、允许做什么、当前在哪里运行”。Agent 角色描述、A2A Agent Card 和进程健康具有不同生命周期，必须分离静态定义、发布版本和动态实例。

## 2. Responsibilities

- 管理 Agent Definition、immutable Version、Deployment 和 Instance。
- 标准化 capability、input/output contract、model/tool/policy profile。
- 管理 draft、review、publish、deprecate、revoke 生命周期。
- 导入并缓存远程 A2A Agent Card，保留签名与来源。
- 提供能力查询、版本解析、健康/负载快照和兼容性信息。
- 发布版本和实例状态事件。

## 3. Non-responsibilities

- 不执行 Agent 或决定最终 Assignment。
- 不保存 Prompt Trace、Task 状态或明文 secret。
- 不把实时 heartbeat 覆盖到 immutable Definition Version。
- 不相信 Agent 自报能力等于已验证能力。

## 4. Entity model

### AgentDefinition

稳定逻辑身份：tenant/owner、name、description、visibility、lifecycle、default version、tags。删除 Definition 不删除历史 Run 引用。

### AgentVersion

发布后不可变，包含：

- semantic/internal version 和 content digest
- role/instructions/prompt template refs
- declared 与 verified capabilities
- input/output JSON Schema refs
- model policy、tool profile、knowledge/memory profile
- required policy profile、risk class、data classification ceiling
- resource/concurrency/budget defaults
- runtime adapter + signed artifact/image digest
- supported execution modes 和 compatibility metadata

### AgentDeployment

将 Version 部署到 environment/runtime/remote peer，包含 desired/current status、traffic weight、region、endpoint reference 和 rollout policy。

### AgentInstance

动态实例：deployment、instance ID、health、last heartbeat、capacity/active slots、protocol endpoint、lease epoch。短期实例记录可按保留策略清理。

### Capability

使用 namespaced stable key，例如 `code.review.python`，包含版本、描述、input/output constraints、evidence requirements。能力层级用于检索，不自动授予权限。

## 5. Lifecycle

```text
DRAFT -> IN_REVIEW -> PUBLISHED -> DEPRECATED -> RETIRED
                  \-> REJECTED
PUBLISHED/DEPRECATED -> REVOKED (security emergency)
```

- Published Version 不修改；修复创建新 Version。
- Deprecated 可继续完成已绑定 Run，默认不接新 Assignment。
- Revoked 阻止新调用并触发 active Run 风险处置；是否终止由 Policy/Operator 决定。
- Default Version 更新不影响已经创建的 Assignment。

## 6. Registration and publication flow

1. Author 创建 Draft 并声明 capability/schema/tool/model/policy。
2. 静态验证：schema、引用、版本、资源上限、secret 禁止项。
3. 安全验证：image/plugin scan、tool permission、data/model policy。
4. capability verification：contract tests、benchmark/evaluation evidence。
5. Reviewer 批准并生成 immutable digest。
6. 发布 Version/Deployment 事件，Scheduler cache 失效。

生产发布和 definition author 权限分离；紧急 revoke 需要强审计。

## 7. Local and remote normalization

内部 `NormalizedAgentDescriptor` 隔离来源：

- local version 来源是受控配置和 runtime artifact。
- remote version 来源是 A2A Agent Card snapshot + extended card + peer policy。
- imported Agent Card 原文、protocol version、URL、ETag/cache、signature verification result 保留为 evidence。
- A2A skill 映射为 capability candidate，需验证后才进入 `verified_capabilities`。
- remote endpoint/authorization scheme 由 A2A Gateway 使用，Scheduler 不接触凭证。

## 8. Capability query

Query 输入：tenant、environment、required capabilities、schema/media、risk/data class、execution mode、deadline/cost hints。输出候选 Version/Deployment 和硬性兼容结果，不返回最终评分。

Cache key 包含 registry revision 和 policy-relevant fields。安全 revoke 必须主动失效缓存；缓存不可用时 fail closed 对高风险任务，低风险可使用未过期 snapshot。

## 9. Health and capacity

- heartbeat 由 trusted Runtime/A2A health probe 写入，不接受用户直接写。
- 状态：UNKNOWN、HEALTHY、DEGRADED、UNHEALTHY、DRAINING。
- readiness 和 liveness 分离；capability-specific probe 可独立降级。
- 实例负载是调度 hint，不是 reservation 真相；reservation 由 Scheduler 管理。
- remote peer health 使用 circuit breaker + active probe + request outcome 综合，不仅依赖 Agent Card 可访问。

## 10. Consistency and events

- Definition/Version 使用乐观版本；publish/revoke 使用事务锁。
- 唯一约束：tenant + normalized name；Version digest 全局内容寻址候选。
- 发布/默认版本切换/撤销与 Outbox 同事务。
- Scheduler Assignment 保存 immutable Version ID + digest，避免 registry update 改写历史。
- instance heartbeat 可批量/时序表保存，不为每个 heartbeat 产生全局领域事件。

Events：AgentDefinitionCreated、AgentVersionPublished/Deprecated/Revoked、DeploymentChanged、InstanceHealthChanged、CapabilityVerificationRecorded、AgentCardRefreshed。

## 11. Security

- Author、Reviewer、Deployer、Operator 分权。
- instruction/template 视为代码，经过 review、签名、diff 和敏感信息扫描。
- Secret 只保存 reference 和 required audience/scope，不在 Version 中保存值。
- Remote Card URL、redirect、DNS 和 signature 验证防 SSRF/替换攻击。
- 跨租户共享 Agent 使用发布者/消费者双向 policy，不允许隐式共享 memory 或 credentials。
- revoke 和供应链告警能定位所有绑定的 active/history Run。

## 12. Observability and evaluation

指标：published versions、draft age、verification failure、deprecated assignment、instance health/capacity、card refresh/signature failure、version quality/cost trends。

Registry 页面显示 declared 与 verified capability 差异、最近评估、tool/model/policy dependencies 和受影响 Run。Trace 使用 immutable version/digest 作为属性。

## 13. Capacity and limits

- Definition 默认最多 100 Versions、Version 最多 200 capabilities/100 tools。
- heartbeat 写路径与 definition 事务分离，并设置过期/降采样。
- Agent Card/Schema/Prompt 大内容转 Artifact 或 versioned config blob，只在表中保存 digest/ref。
- capability search 初期使用 PostgreSQL 索引；规模/语义检索需求明确后才引入外部搜索。

## 14. Testing

- schema compatibility、immutable publish、default switch、revoke cache invalidation。
- malicious Agent Card、signature failure、redirect/SSRF 和 oversized card。
- local/remote descriptor normalization golden fixtures。
- active Assignment 在 default version 改变后仍解析原 Version。
- concurrent publish/deprecate/revoke property tests。

## 15. Acceptance criteria

- 任一 Run 可永久解析到当时的 Agent Version 和依赖摘要。
- Definition、Version、Deployment、Instance 生命周期互不覆盖。
- Scheduler 只能选择 published、compatible、policy-allowed 且可用候选。
- 自报 capability 与 verified capability 明确区分。
- 紧急 revoke 能阻止新 Assignment 并列出受影响 active Run。
