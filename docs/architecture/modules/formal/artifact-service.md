# Artifact Service

Status: Proposed
Owners: Data and content platform maintainers
Depends on: [Cross-module contracts](cross-module-contracts.md), [Policy and approval](policy-and-approval.md)

## 1. Problem

Agent 产物可能是 JSON、报告、代码、数据集、图片或外部文件。大型内容不能进入消息/Checkpoint，跨 Agent/A2A/MCP 的内容也不能在未扫描和授权时直接使用。

## 2. Responsibilities

- 管理 Artifact、immutable Version、Blob 和 lineage 元数据。
- 签发受限上传/下载授权并 finalize 内容。
- 校验哈希、大小、MIME、schema、恶意内容和数据分类。
- 管理版本、引用、派生关系、保留、legal hold 和删除。
- 为跨模块/跨组织传递提供 ArtifactRef 和临时 access grant。
- 发布 Artifact 生命周期事件。

## 3. Non-responsibilities

- 不决定 Task 是否完成。
- 不把对象存储 URL 当永久业务身份。
- 不允许 Agent 直接使用对象存储主凭证。
- 不对所有文件类型承诺语义正确性；只提供明确 validator/scan result。

## 4. Entity model

| Entity | Purpose |
|---|---|
| Artifact | 稳定逻辑产物，owner/tenant/kind/classification/lineage |
| ArtifactVersion | 不可变版本，media/schema/size/hash/status/producer |
| Blob | 内容寻址对象，storage key、encryption key ref、ref count |
| UploadSession | 临时上传授权、expected hash/size、expiry |
| AccessGrant | subject、version、operation、constraints、expiry |
| ScanResult | scanner/version、type、verdict、evidence ref |
| ArtifactRelation | derived-from、contains、supersedes、input-to、output-of |
| ExternalReference | peer/server、locator、integrity、access policy、expiry |

ArtifactVersion 的 producer 包含 Task/Run/Attempt/AgentVersion 和 trace link；人工上传记录 user principal。

## 5. Lifecycle

```text
DECLARED -> UPLOADING -> UPLOADED -> SCANNING -> AVAILABLE
                                  \-> QUARANTINED -> REJECTED
AVAILABLE -> DELETION_PENDING -> DELETED
Any retained state -> LEGAL_HOLD (overlay)
```

`AVAILABLE` 只表示内容通过配置的基础校验，不表示业务验收通过。QUARANTINED 只有安全角色/扫描服务可访问。

## 6. Upload flow

1. CreateArtifact 声明 kind/media/schema/classification、expected size/hash 和 producer。
2. Policy 检查写权限、配额和允许类型。
3. Service 创建 UploadSession 和限制 method/content-length/checksum 的短期 signed request。
4. 客户端直接上传临时对象；control plane 不代理大文件正文。
5. Finalize 读取对象 metadata/streaming hash，校验 expected values 并原子创建 Version/Blob 引用。
6. 异步扫描；全部 required checks 通过后 AVAILABLE 并发事件。

Finalize 可幂等；同 session+hash 重复返回相同 Version。未 finalize 的临时对象按 TTL 清理。

## 7. Download and consumption

- 调用者提交 ArtifactVersion + intended use；Service 重新验证 tenant/Policy/classification/scan/status。
- 返回短期单用途/少用途 signed URL 或受控 stream，不返回 storage credential。
- high-risk/restricted 下载可要求 approval、水印、内容转换或禁止外发。
- Runtime 可请求 bounded excerpt/manifest，而不必加载完整内容。
- access grant 不写入 Event/Checkpoint；只保存 grant ID，恢复时重新签发。

## 8. Content validation

分层 validator：

- transport：size、checksum、content length、encryption。
- type：magic bytes 与声明 MIME 一致，防扩展名欺骗。
- security：malware、archive bombs、active content、secret scanning。
- schema：JSON/CSV/structured output contract。
- domain：可选 lint/test/signature/SBOM/citation manifest。
- classification：DLP/PII/tenant policy。

Scanner/validator 版本与结果保留；规则升级可触发 re-scan，不修改原内容。

## 9. Deduplication and versioning

- Blob 以 tenant/key-domain + sha256 内容寻址候选，restricted tenant 默认不跨租户 dedupe，避免侧信道。
- Version immutable；编辑产生新 Version，并用 relation 连接。
- ref count 只是删除优化，权威引用通过关系/业务外键检查。
- 相同内容不同 classification/policy 可以共享或不共享 Blob，由 encryption domain 决定。
- inline-small 仅允许严格大小/类型，仍具有 hash 和 ArtifactVersion。

## 10. A2A/MCP exchange

- 入站外部 URL 先保存 ExternalReference，再由 egress-controlled fetcher 拉取到 quarantine。
- Fetcher 防 SSRF、redirect、私网地址、超时、无限流和 content-length 欺骗。
- 出站 URL 绑定已准入 Peer/Server、audience、expiry、method、hash 和最大大小。
- A2A Part/chunk 的 append/last 语义映射为临时 multipart upload；final 前不可用。
- MCP Resource 需要持久化时创建 ArtifactVersion 并记录 server/resource URI provenance。

## 11. Consistency and deletion

- Metadata 与 Outbox 同 PostgreSQL 事务；对象存储使用应用级 saga。
- DB 成功、对象不存在：status 保持 UPLOADING 并记录 `last_error`，Reconciler 检查、重试或过期拒绝。
- 对象存在、DB finalize 失败：相同 session 重试或 orphan cleanup。
- 删除先检查 policy/legal hold/引用，标记 DELETION_PENDING；后台删除 blob/key 后标 DELETED。
- 加密销毁可作为强删除手段；备份保留需在策略中明确。
- external reference 删除只撤销引用/credential，不声称删除第三方内容。

## 12. Security

- 对象键不可预测且不含文件名、tenant/user PII。
- bucket/container 默认 private、server-side encryption、TLS、versioning/immutability 按分类启用。
- signed URL 短期、audience/operation constrained；日志隐藏 query signature。
- 文件名在 UI 转义并单独保存 display name，不用于路径。
- quarantine 与 available 使用不同 prefix/role，Runtime 无权读取 quarantine。
- restricted Artifact 的 trace/log/content preview 默认关闭。

## 13. Observability and capacity

指标：upload/finalize latency、bytes、scan queue age/verdict、quarantine、dedupe ratio、download denied、signed URL issuance、orphan/delete backlog、classification distribution。

限制：单文件/单 Task bytes、archive expansion ratio、multipart count、download bandwidth、retention、external fetch size/time。配额在 Create 和 Finalize 两次校验，防 declared/actual size 不一致。

## 14. Testing

- hash/size/type mismatch、duplicate finalize、concurrent version、orphan cleanup。
- malware/archive bomb/active content/secret fixture 安全测试。
- signed URL scope/expiry/audience、SSRF/redirect 和 cross-tenant access。
- object store outage 和 DB/object commit window 故障注入。
- A2A chunk/MCP resource round-trip 保留 hash、media 和 provenance。

## 15. Acceptance criteria

- 消息、Event 和 Checkpoint 中的大内容都替换为稳定 ArtifactRef。
- AVAILABLE 内容具有可验证 hash、scan status、classification 和 producer lineage。
- Agent/Peer 不能获得对象存储长期凭证或访问其他租户对象。
- 上传、扫描、删除的所有崩溃窗口可自动清理或进入可见人工状态。
- 远程 completed 不会让未扫描 Artifact 被正式消费。
