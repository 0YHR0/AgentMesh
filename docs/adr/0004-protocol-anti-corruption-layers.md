# ADR 0004: Isolate A2A and MCP behind anti-corruption layers

Status: Proposed
Date: 2026-07-16

## Context

A2A 和 MCP 都在快速演进，并拥有各自 Task、Message、Artifact、Tool、Resource、状态和版本语义。直接复用协议对象作为内部数据库模型会导致外部升级改写领域状态，也容易混淆 Agent 委托与 Tool 调用。

## Decision

- A2A 只用于独立 Agent 服务边界；映射到 RemoteTaskCorrelation 和内部 Run command/event。
- MCP 只用于 Tool/Resource/Prompt 等能力访问；实验性 MCP Task 仅属于一次 invocation。
- 每个协议先解析为版本化 DTO，再通过 adapter 映射到内部 canonical contracts。
- 保存原始协议 snapshot/digest/关联作为 evidence，但外部 ID 不作为内部主键。
- 协议版本和 binding 是部署兼容性配置，不进入核心业务枚举。

## Consequences

- 可以支持多个协议版本/binding，并在升级时保持内部历史稳定。
- 需要维护 mapping、conformance fixture、unknown state 和扩展策略。
- 外部 completed/tool success 仍需本地 Artifact、Policy 和验收验证。
- Adapter 会增加一次转换成本，但换来更清晰的安全和所有权边界。

## Alternatives considered

- 内部全面使用 A2A Task：拒绝，本地工作流、审批、租约和业务状态需求超出协议语义。
- 用 MCP 连接所有 Agent：拒绝，MCP 解决工具/上下文，不是跨 Agent 全局调度。
- 只支持一个固定协议版本并直接序列化 SDK 类：拒绝，升级和跨语言兼容风险过高。
