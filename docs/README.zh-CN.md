# 文档导航

[English](README.md) | [简体中文](README.zh-CN.md) | [项目首页](../README.zh-CN.md)

**先看教程，再看设计。**教程介绍当前可用的产品；架构文档解释实现，提案则可能讨论未来能力。
准确的已交付范围以[实现状态](implementation-status.md)为准。

| 你的目标 | 从这里开始 | 然后阅读 |
| --- | --- | --- |
| 不用 Key 试用 AgentMesh | [5 分钟上手](getting-started.zh-CN.md) | [真实模型客户反馈教程](scenarios/customer-feedback-deepseek.zh-CN.md) |
| 配置员工、模型或记忆 | [模型与记忆配置](model-and-memory-setup.zh-CN.md) | [仅通过 SSH 管理凭据](operations/model-connections-ssh.md) |
| 通知飞书群 | [飞书通知配置](integrations/feishu-notifications.zh-CN.md) | [Feature Gate](architecture/modules/feature-gates.md) |
| 跑一个业务场景 | [客户反馈协作教程](scenarios/customer-feedback-deepseek.zh-CN.md) | [市场研究场景](scenarios/market-research.zh-CN.md) |
| 部署或维护 AgentMesh | [管理员最佳实践](best-practices.zh-CN.md) | [SLO 与恢复](operations/slo-and-restore.md)、[Runtime 回退](operations/runtime-direct-cutover-rollback.md) |
| 开发一个扩展 | [Runtime 扩展协议](architecture/modules/runtime-extension-protocol.md) | [Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter)、[正式版模块契约](architecture/modules/formal/README.md) |

## 参考资料与项目状态

- **已实现什么：**[实现状态](implementation-status.md)、[v1 范围](v1-completion-scope.md)、
  [版本记录](../CHANGELOG.md)和[最新发布说明](releases/v0.2.0-alpha.4.md)。
- **配置与 API：**[Feature Gate](architecture/modules/feature-gates.md)、
  [Control API 设计](architecture/modules/formal/control-api.md)；运行后还可打开服务的 `/docs` 查看 OpenAPI。
- **集成边界：**[MCP Registry](architecture/modules/governed-mcp-registry-implementation.md)、
  [A2A 委派](architecture/modules/a2a-outbound-delegation-implementation.md)、
  [Agent 记忆](architecture/modules/organizational-memory-implementation.md)。
- **架构：**[L0 设计](architecture/L0-system-design.md)、[L1 设计](architecture/L1-design-plan.md)、
  [L2 模块索引](architecture/modules/README.md)、[架构决策](adr/README.md)和[术语表](glossary.md)。
- **规划：**[路线图](roadmap.md)与[提案](proposals/README.md)。

实现文档是技术参考，不是新用户操作手册。提案讨论未来工作，不代表功能已交付；
判断当前行为时请先查实现状态。L0/L1 是当前架构概览，详细行为以已接受的 ADR 和代码为准。
