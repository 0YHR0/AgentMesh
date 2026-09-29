# 从零跑通真实多 Agent：客户反馈简报

[English](customer-feedback-deepseek.md) | 简体中文

这篇教程给第一次使用 AgentMesh 的人。你是一名产品负责人，手上有三条客户反馈；希望“整理员”摘录证据，“分析员”提出优先级，“编辑员”写简报。你只需提供一次模型连接，再为三名员工分别发布职责和指令。这个场景只用核心任务能力，**不需要 MCP、A2A、公司记忆或外部 Memory 服务**。

本文截图来自 2026-09-28 的私有真实模型验收：从前端创建并运行了任务，三次 DeepSeek 执行均成功。截图只含虚构反馈，不含模型 Key 或平台令牌。示例内容可以复制；你的任务 ID、模型输出和用量会不同。

## 先认清这四样东西

| 名称 | 在本例中是什么 |
|---|---|
| 模型连接 | 一份 DeepSeek API Key 和模型名；三名员工可以共用 |
| 员工 / Agent Definition | “整理员”“分析员”“编辑员”三个独立身份 |
| 员工版本 / Agent Version | 每名员工已发布的一份职责、指令、能力和模型策略 |
| 任务 / Task | 一次客户反馈分析；包含三个按依赖顺序执行的工作单元 |

> 默认 `docker compose up --build` 启动的是**无需 Key 的演示模式**。要填写真实 Key，管理员必须先开启 Identity/RBAC、为 API 与 Worker 配置同一份模型连接加密密钥，并通过 HTTPS 或已验证的本地/SSH 通道访问 Console。请先完成[SSH 安全配置手册](../operations/model-connections-ssh.md)；普通任务发起人不需要接触 Key。不要在公网 HTTP 页面、任务文本、截图或 Git 中填写密钥。

## 1. 配置并测试模型连接（管理员一次性操作）

进入安全通道中的 Console，点击顶部 **Connection / 连接设置**，填写平台管理员 Bearer Token。它与 DeepSeek API Key 是两种不同的凭据。随后进入 **Setup → Manage model connections**，新增连接：

| 字段 | 示例值 |
|---|---|
| Name | `Customer feedback DeepSeek` |
| Provider | `DeepSeek` |
| Model | `deepseek-flash` |
| Credential source | `API key` |
| API key | 你的 DeepSeek Key，只在这个安全表单中输入 |

`deepseek-flash` 是本次验收使用的模型名；后续版本请以[DeepSeek 官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/)为准。下图的 Key 输入框故意留空。

![安全通道中的模型连接表单；Key 未显示](../assets/real-model-onboarding/00-model-connection-form.png)

点击 **Save connection** 后，再点击连接行的 **Test**。保存只表示凭据已加密入库；测试成功才证明平台能实际请求供应商。测试本身会发出一次模型请求，可能产生少量费用。图中的“Test passed (this session)”只表示当前浏览器会话里完成了测试，不是长期健康监控。

![DeepSeek 连接测试成功](../assets/real-model-onboarding/01-model-setup.png)

右侧“Memory setup needed”不影响本例；长期记忆是可选功能。

## 2. 创建并发布三名员工（管理员一次性操作）

进入 **Agents**，用左侧的 **＋** 创建三个 Agent Definition，名字分别为 `feedback-organizer`、`feedback-analyst`、`feedback-editor`。Owner ID 填团队标识；可见范围按部署策略选择。然后对每名员工点 **New version**，配置：

| 员工 | Role | System instructions 要点 |
|---|---|---|
| `feedback-organizer` | 客户反馈整理员 | 逐条提取原话和观察；不编造事实 |
| `feedback-analyst` | 客户反馈分析员 | 读取前序结果，分类并给优先级；三条样本不能推断普遍性 |
| `feedback-editor` | 客户反馈编辑员 | 读取前序结果，按“观察、优先级、下一步”写简报 |

三者的 **Capabilities** 都填 `general.task`，**Provider** 选 `DeepSeek`，**Saved connection** 选刚才测试通过的连接。建议此例的 **Maximum output tokens** 先设 `1200`；本次验收特意用了 `256`，结果出现截断。Tool 保持不勾选：材料已经直接粘贴到任务里，无须联网或读文件。每名员工依次点击 **Create immutable draft → 提交审核 → 发布版本**，并将发布版本设为默认版本。图中展示的是整理员的版本表单和已发布状态；截图里的 `acceptance-*` 名称是验收用例，实际使用时可用上表名称。

![为员工版本绑定 DeepSeek 连接、指令和能力](../assets/real-model-onboarding/02b-agent-version-form.png)

![已发布的整理员及其模型策略](../assets/real-model-onboarding/02-published-employee.png)

发布版本是不可变快照。修改指令或输出上限时创建新版本并重新发布；已开始的 Run 仍可追溯到原版本。

## 3. 下发一项三员工任务

进入 **Tasks → ＋**。把下面的虚构材料粘贴到 **Materials or context**，而不是只填写本机文件路径：

```text
甲：搜索结果偶尔延迟约五秒。
乙：手机端的保存按钮不够明显。
丙：导出报告时希望看到进度。
```

填写：

- **Goal**：整理三条客户反馈，形成一份有证据、有优先级的简短报告。
- **Expected output**：观察、优先级、下一步；每条判断引用原文，不能推断问题普遍性。
- **Success conditions**：引用所给反馈原文；不能将三条样本说成普遍趋势。
- **Execution**：`Coordinated · selected published employees`。

![输入目标、材料和预期产出](../assets/real-model-onboarding/03-create-coordinated-task.png)

在三个工作单元中按下表填写，并为每行选择已发布员工。界面默认带三行，可直接改名。**Depends on** 是多选框：先点中前置工作，后续员工才会收到它的输出。

| Work item | Published employee | Deliverable | Depends on |
|---|---|---|---|
| 整理 | `feedback-organizer` | 提取每条反馈的原文和问题 | 留空 |
| 分析 | `feedback-analyst` | 分类并建议优先级，标记不确定性 | 整理 |
| 编辑 | `feedback-editor` | 基于前两步输出三段中文简报 | 整理、分析 |

![给三个工作单元分配员工和前置依赖](../assets/real-model-onboarding/03b-assign-work-items.png)

点击 **Review task plan**，检查目标、人员和依赖；确认后点击 **Create and run**。若只点 **Create task**，任务会创建但不会开始执行。当前预览有时显示内部工作键（例如 `research`、`analysis`），它们对应上面三行，不是额外的员工。**Success conditions** 会进入任务上下文/目标合同，但在这个流程中不能替代人工核对最终内容。

![创建前确认人员与依赖](../assets/real-model-onboarding/04-review-plan.png)

## 4. 看协作过程并验收结果

任务详情会显示 `COMPLETED`、工作单元进度、Run 数量。切换到 **Mission Map / 任务地图**，可以看到 `AgentMesh HQ → 整理 → 分析 → 编辑` 的依赖路线；右侧可检查所选员工、Run 状态及事件。此次 UI 验收中，三个 DeepSeek 员工都完成了各自工作，另有一个系统 supervisor Run，因此是 **3 个工作单元、4 个成功 Run**。

![三名员工完成任务后的地图与事件](../assets/real-model-onboarding/05-mission-map.png)

继续下滑到 **Execution result / 执行结果** 和 **Run history / 运行记录**。当前界面会优先展示最终工作单元的可读简报，并标明产出员工；展开 **查看原始 JSON** 可核对底层任务输出。默认 supervisor 仍是演示型执行器，只给出通用流程摘要，因此结果视图会选择末端员工的输出。Run history 则能核实三名实际执行员工都成功，以及 supervisor 的身份。

![Alpha.1 验收时的原始任务结果、运行记录和活动时间线](../assets/real-model-onboarding/06-result-and-runs.png)

这张图保留了 Alpha.1 验收时的旧界面；当前界面会把同一份底层输出展示为可读结果，原始 JSON 可按需展开。

本次通过前端创建的真实模型任务记录了 **2,542 个供应商 tokens**、**28 条活动记录**。这些只是一次小样本验收，不是固定成本或质量保证。此任务的 `interactions` 数量为 0：依赖路线和活动时间线已经记录了调度顺序，但没有产生显式 Handoff 交互；不要把地图上的每条依赖线理解为已经发生了 A2A/MCP 消息。

验收时逐项检查：三个子任务是否按顺序完成；输出是否引用原文；“搜索延迟优先”的判断是否明确标注它只是建议而非统计结论；是否存在编造、遗漏或截断。**`COMPLETED` 表示执行流程完成，不代表内容自动正确。**

## 常见问题

- **连接保存了，但执行仍是 Demo**：检查员工的已发布默认版本是否绑定了 DeepSeek 连接，任务是否选中了该员工；只保存连接不会改变演示员工。
- **Coordinated 不可选**：请管理员检查 `coordinated_execution` 与 `agent_registry_management` 的 Feature Gate。
- **Key 或 Token 输入框不可用**：不要在公网 HTTP 上绕过限制，改走管理员配置的 HTTPS 或 SSH 本地通道。
- **结果被截断**：提高新员工版本的输出 token 上限、发布为默认版，然后创建新任务；旧 Run 不会被静默改写。
- **预算数字与供应商 tokens 不一致**：预算字段可能记录预留/结算额度；核对实际模型用量请看任务的 `/usage` 记录。未配置价格目录时，不应把空的成本字段当成免费。
- **需要联网、读文件、长期记忆或自动发送回复**：这些都不是提供一个模型 Key 就自动获得的能力；分别配置受治理的 MCP Tool、公司记忆和审批策略。

想先验证不付费的基础链路，可从[五分钟上手](../getting-started.zh-CN.md)的 Direct 演示任务开始；完成后再按本文接入真实模型。
