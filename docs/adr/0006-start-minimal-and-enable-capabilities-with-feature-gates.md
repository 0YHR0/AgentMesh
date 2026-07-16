# ADR 0006: Start minimal and enable optional capabilities with Feature Gates

Status: Accepted
Date: 2026-07-16

## Context

AgentMesh 的目标能力包括 Registry、部署管理、MCP、A2A、审批、可观测性和 Web Console。
如果首次启动就暴露所有配置与 API，使用者需要在理解完整平台前处理大量无关概念；如果维护多个精简版，
又会造成代码、数据库和升级路径分叉。

## Decision

AgentMesh 维护一套代码和一套向前兼容的数据库结构，通过进程启动时解析的 Feature Gate 决定可选能力是否可用：

1. 默认 profile 是 `minimal`，核心 Task 创建、执行、查询和内置 Agent 永远可用。
2. `standard` 开启 Agent Registry 管理；`full` 再开启 Agent Deployment 管理。
3. `AGENTMESH_FEATURE_GATES` 可以在 profile 之后逐项覆盖。
4. Feature 之间的依赖在启动时校验；未知名称、非法值和缺失依赖均使进程快速失败。
5. Gate 在服务端边界强制执行。关闭的 API 返回稳定的 `403 feature_disabled`，不能只靠 UI 隐藏。
6. Gate 配置在进程生命周期内不可变，修改后需要重启。
7. 数据库 migration 始终应用完整 schema；Gate 不用于回滚 schema，也不替代身份认证、授权或租户策略。

## Consequences

- 新用户获得稳定、低认知负担的最小路径，同时高级用户不需要更换发行版。
- 一套 schema 让 profile 切换可逆，也避免按功能组合维护 migration 分支。
- 启用高级功能需要显式重启，首版不支持动态灰度和按租户开关。
- 新模块必须声明 Gate、依赖、默认 profile、服务端执行点和测试，不能只增加一个环境变量。

## Alternatives considered

- 默认开启全部功能：拒绝，首次使用和故障排查成本过高。
- 分别发布 lite/pro/enterprise 代码库：拒绝，容易产生不可合并的实现和升级路径。
- 从数据库动态读取、按请求或租户切换：暂缓，当前没有动态发布的需求，且会引入缓存一致性与运行中状态迁移问题。
