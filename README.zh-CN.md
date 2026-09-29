# AgentMesh

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml)

AgentMesh 是一个可自行部署的 AI Agent 团队控制平面。你给出目标和验收标准，安排不同角色的
“员工”协作，并在同一个 Console 中查看进度、交接、决策与结果。简单任务也可以只用一个 Agent。

**当前状态：**Alpha，适合单团队评估和非关键场景；尚不能视为生产级多租户或高可用服务。
准确边界见[实现状态](docs/implementation-status.md)和 [v1 范围](docs/v1-completion-scope.md)。

## 本地试用

只需要 Docker。默认确定性执行器不需要模型 API Key。

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

打开 [Console](http://localhost:8000)，创建一个 Direct 任务并查看结果。如果想使用真实模型、
创建员工和运行协同任务，请跟随下面的教程。**不要在未认证的公网 HTTP 部署中输入模型密钥。**

## 按目的找文档

| 我想…… | 从这里开始 |
| --- | --- |
| 不用 Key 创建第一个任务 | [5 分钟上手](docs/getting-started.zh-CN.md) · [English](docs/getting-started.md) |
| 配置 DeepSeek，观察三名员工协作 | [带截图的真实模型教程](docs/scenarios/customer-feedback-deepseek.zh-CN.md) · [English](docs/scenarios/customer-feedback-deepseek.md) |
| 配置模型连接和员工记忆 | [模型与记忆配置](docs/model-and-memory-setup.zh-CN.md) · [English](docs/model-and-memory-setup.md) |
| 部署、开启功能或维护服务 | [管理员最佳实践](docs/best-practices.zh-CN.md) · [English](docs/best-practices.md) |
| 理解架构或扩展平台 | [文档导航](docs/README.zh-CN.md) · [English](docs/README.md) |

[文档导航](docs/README.zh-CN.md)还可以找到更多场景、API 与功能参考、运维手册、架构决策、
提案和路线图。

## 核心包含什么？

- Direct、Reviewed 和 Coordinated 任务执行，版本化 Agent 和持久化结果。
- 创建任务、查看状态、人工介入和回放 Mission Map 的 Console。
- 可选的 MCP 工具、A2A 委派、人工审批、预算、公司记录和记忆。
- PostgreSQL 保存业务真相，Redis Streams 负责投递；LangGraph 是执行适配器，高级能力由
  Feature Gate 按需开启。

参与贡献请看 [CONTRIBUTING](CONTRIBUTING.md)，版本变化请看 [Changelog](CHANGELOG.md)。
项目采用 [Apache 2.0 许可证](LICENSE)。
