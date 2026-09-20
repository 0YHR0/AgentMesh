# AgentMesh 普通用户 5 分钟上手

[English](getting-started.md) | 简体中文

这份指南面向第一次使用 AgentMesh 的人。你不需要理解 LangGraph、DAG、Redis、Runtime
或幂等键，只需要知道：**创建一项工作、选择执行方式、观察员工协作、检查结果**。

> 当前版本是 Alpha，适合体验、本地开发和非关键单团队使用。默认的确定性执行器用于验证
> 整条流程是否工作，不代表真实模型的内容质量。

## 1. 启动并打开界面

确保电脑已经安装 Docker，然后运行：

```bash
git clone https://github.com/0YHR0/AgentMesh.git
cd AgentMesh
docker compose up --build
```

打开 <http://localhost:8000>。界面默认是英文，点击右上角 **中文** 即可切换，选择会保存在
浏览器中。

## 2. 先运行一个单员工任务

1. 点击 **Run a single-agent task first / 先运行单 Agent 任务**。
2. 在“总体目标”中填写：`阅读 AgentMesh README，用三点说明它解决了什么问题。`
3. 保持执行方式为 **Direct / 单 Agent 直接执行**。
4. 点击“创建并查看”，再点击“开始执行”。
5. 在任务页面查看状态、运行记录、事件和执行结果。

默认模式不需要 API Key，也不会访问外部模型。它的用途是确认从任务创建、排队、Worker
执行到结果落库的完整链路已经跑通。

## 3. 什么时候选择多员工协作

只有当工作确实需要分工、并行或独立审核时，才选择 **Coordinated / 多 Agent 协作**。

例如“制作新品市场分析”可以拆成：

| 角色 | 工作目标 | 依赖 |
|---|---|---|
| 研究员 | 收集并整理输入材料 | 无 |
| 分析师 | 比较竞品并提炼趋势 | 研究员 |
| 产品经理 | 根据分析提出定位建议 | 分析师 |
| 审核员 | 检查事实、逻辑和遗漏 | 产品经理 |

在创建任务时选择多 Agent 协作，为每一行填写：

- **角色**：用户能理解的员工名称；
- **Agent ID**：管理员预先配置好的执行者；
- **工作目标**：这个员工必须交付什么；
- **依赖 Key**：必须先完成的工作，没有依赖时留空。

如果一个员工就能完成，不要为了“看起来像多 Agent”而拆分任务。额外的交接会增加延迟、
模型费用和信息损失。

## 4. 使用真实模型前，谁需要配置什么

### 普通用户

普通用户只需要提供：

- 清晰的目标；
- 必要的背景资料或材料位置；
- 希望得到的结果格式；
- 需要哪些角色参与；
- 截止条件和人工确认点。

### 平台管理员

管理员负责一次性配置：

- 模型 Provider 和 API Key；
- 可用的 Agent 及其已发布版本；
- 联网搜索、文件读取等 MCP 工具；
- 哪些动作需要人工审批；
- 模型和工具预算。

当前内置真实模型适配器使用 OpenAI Responses API。只把 Key 配置在 Worker 环境中：

```dotenv
AGENTMESH_MODEL_PROVIDER=openai
AGENTMESH_MODEL_NAME=gpt-5.6-terra
AGENTMESH_MODEL_REASONING_EFFORT=low
OPENAI_API_KEY=replace-with-your-local-secret
```

不要把 Key 填入任务目标、浏览器表单或提交到 Git。其他模型服务需要对应 Runtime/Provider
适配器；不要默认认为所有“OpenAI 兼容接口”都已经通过验证。

## 5. 运行时看什么

- **Queued**：任务已经接收，正在等待员工处理；
- **Running**：员工正在工作；
- **Paused / Approval required**：需要人工确认或已暂停；
- **Failed / Unknown**：任务失败，或外部结果暂时无法确认；
- **Succeeded**：流程完成，可以检查输出和 Artifact；
- **Mission Map**：谁正在做什么、依赖谁、发生了哪些交接；
- **Run history**：同一项工作每次执行的历史；
- **Artifact**：报告、JSON、代码、音频等可交付结果。

遇到 `Unknown` 时不要直接重新创建任务。先让管理员检查已有证据并执行 reconciliation，避免
外部动作被重复执行。

## 6. AgentMesh 当前能做与不能自动做到的事

| 能力 | 当前说明 |
|---|---|
| 单 Agent 与多 Agent 执行 | 已支持 Direct、Reviewed 和 Coordinated |
| 角色分工与依赖 | 已支持显式工作单元和依赖关系 |
| 观察与干预 | 已支持状态、事件、暂停、恢复、取消和审批 |
| 失败恢复 | 已支持持久化状态、重试边界和未知结果收敛 |
| 工具与外部 Agent | 可通过 MCP 与 A2A 接入，但必须先配置 |
| 实时互联网信息 | 默认没有；需要接入搜索/读取类 MCP 工具 |
| 自动发邮件、发布、付款 | 默认没有；需要外部工具、策略和人工审批 |
| 长期员工记忆 | 需要启用或接入 Memory 后端并配置治理策略 |
| 真实内容质量 | 取决于模型、Prompt、工具、材料和验收流程 |

下一步可以跟随[市场研究场景教程](scenarios/market-research.zh-CN.md)。部署、治理和故障恢复
细节请交给管理员参考[管理员与运维最佳实践](best-practices.zh-CN.md)。

## 7. 常用词汇

| 产品词汇 | 可以理解为 |
|---|---|
| Agent | 一名虚拟员工 |
| Agent Version | 这名员工一次固定、可追溯的培训版本 |
| Capability | 员工会做什么 |
| Tool / MCP | 员工可以使用的外部工具 / 接入工具的统一方式 |
| Task | 用户交办的一项工作 |
| Run | 这项工作的一次正式执行 |
| Direct | 一名员工独立完成 |
| Reviewed | 一名员工完成，另一名员工独立审核 |
| Coordinated | 多名员工按照依赖关系协作 |
| Handoff | 一名员工把结构化工作交给另一名员工 |
| Artifact | 报告、代码、音频等交付物 |
| Approval | 高风险动作执行前的人工确认 |
| Budget | 允许使用的模型、工具或费用上限 |

