# 场景教程：用虚拟团队完成新品市场分析

[English](market-research.md) | 简体中文

## 用户想解决的问题

假设你计划推出一款 AI 智能耳机，希望虚拟公司回答：

> 目前有哪些主要竞品？用户最在意什么？我们应该面向谁、以什么价格和卖点进入市场？

最终交付物是一份可以追溯来源、经过独立复核的市场分析报告，而不是一段无法确认依据的
聊天回答。

## 最简单的体验方式

当前仓库包含确定性的 Market Intelligence Studio 示例。它使用固定 fixture，不访问互联网、
不消耗模型费用，也不代表真实市场结论，但可以展示完整交付链路：

```bash
export AGENTMESH_FEATURE_PROFILE=full
export AGENTMESH_FEATURE_GATES=company_model=true,company_goals=true,company_operations=true,business_objects=true,organizational_memory=true,company_finance_read=true,financial_governance=true,company_packs=true
docker compose up -d postgres redis migrate api
python examples/market-intelligence-studio/run.py
```

PowerShell 用户把前两行改为 `$env:变量名="值"`。这个离线示例只需要启动 API 侧的持久化
和治理能力，不需要模型 Worker。

具体命令和产物位置以
[Market Intelligence Studio 示例说明](../../examples/market-intelligence-studio/README.md)为准。

## 真实场景需要准备什么

### 平台管理员一次性准备

| 项目 | 作用 |
|---|---|
| 真实模型 Provider 和 Key | 让员工理解材料、分析和写作 |
| 搜索与网页读取 MCP 工具 | 获取实时公开信息；默认环境不包含实时互联网数据 |
| 已发布的 Agent Version | 固定每名员工的角色、模型、能力和工具权限 |
| 审批策略 | 在对外发布或执行写操作前等待人工确认 |
| 预算 | 限制模型和工具消耗，防止任务无限循环 |

建议准备四类职责。当前内置 Live Research 模板会把研究工作进一步分成
`research-lead` 与 `research-specialist`，并使用 `fact-reviewer` 和 `editorial-reviewer`
完成事实与编辑复核：

| 员工 | 职责 | 建议工具 | 不应拥有的权限 |
|---|---|---|---|
| 研究负责人/研究专员 | 规划范围并收集竞品、价格、评价和来源 | 搜索、网页读取 | 发布、付款 |
| 数据分析师 | 对材料进行比较、归类和证据评级 | 读取研究材料 | 修改原始来源 |
| 产品经理 | 提出目标用户、定位、价格和验证计划 | 读取分析结果 | 把估算描述为事实 |
| 报告审核员 | 检查引用、矛盾、遗漏和结论强度 | 读取全部中间结果 | 绕过审批直接发布 |

### 普通用户每次任务填写

在 Console 选择 **多 Agent 协作**，总体目标可以写成：

```text
为一款计划售价 999–1299 元、面向中国一二线城市知识工作者的 AI 智能耳机制作市场分析。
优先使用最近 90 天的公开资料；区分事实、推断和建议；所有关键结论附来源；最后输出竞品表、
用户痛点、差异化定位、主要风险，以及三项两周内可以执行的验证实验。报告发布前等待人工确认。
```

然后配置工作单元：

| Key | 角色 | Agent ID | 工作目标 | 依赖 Key |
|---|---|---|---|---|
| `research` | 市场研究员 | 管理员发布的研究 Agent | 收集带来源的竞品证据 | 留空 |
| `analysis` | 数据分析师 | 管理员发布的分析 Agent | 比较证据并标记置信度 | `research` |
| `positioning` | 产品经理 | 管理员发布的产品 Agent | 形成定位和验证计划 | `analysis` |
| `review` | 报告审核员 | 管理员发布的审核 Agent | 检查并给出通过或修改意见 | `positioning` |

Agent ID 必须替换成环境中已经发布且能力匹配的真实 ID。首次使用时将最大并发设为 `2` 即可；
本流程大部分工作有先后依赖，提高并发不会明显加速。

## AgentMesh 如何执行

1. 把用户目标保存为可追踪的 Task。
2. 为每个角色选择并固定一个 Agent Version。
3. 只调度依赖已经完成的工作，独立工作可以并行。
4. 将研究结果以结构化 Handoff 交给下一个角色。
5. 保存状态、事件、模型/工具用量和中间结果。
6. 审核不通过时，只修改仍允许调整的后续计划，不改写已完成历史。
7. 涉及受控写操作时暂停并等待人工审批。
8. 生成最终输出和可追溯 Artifact。

用户可以在 Mission Map 中看到每个角色的状态、依赖、交接、工具调用和审批，而不是只能等待
一个黑盒聊天窗口返回结果。

## 最终可以得到什么

- 竞品与价格对比；
- 每个关键结论的来源和时间；
- 用户痛点与证据强弱；
- 建议的目标用户、定位、价格区间和卖点；
- 风险、未知信息和下一步验证实验；
- 审核结论以及被退回修改的原因；
- 每个角色和每次运行的完整追踪记录。

模型生成的内容仍可能出错。AgentMesh 提供的是**分工、治理、过程证据和可恢复执行**，不是对
市场结论真实性的自动担保。

## 这个场景为什么不直接使用一个 Agent

单 Agent 适合快速草稿。多 Agent 只有在以下收益值得额外成本时才使用：

- 研究和判断可以分开，减少“先入为主”；
- 审核员不与作者共享同一个交付目标；
- 中间证据可以独立检查和复用；
- 某一步失败时可以从边界恢复，而不是整项工作重来；
- 用户能在发布前介入，而不是只接受最终答案。

如果只是总结一份已有文档，请使用 Direct，不要使用这套四角色流程。

返回[普通用户 5 分钟上手](../getting-started.zh-CN.md)，或由管理员继续阅读
[管理员与运维最佳实践](../best-practices.zh-CN.md)。
