(() => {
  "use strict";

  const STORAGE_KEY = "agentmesh-language";
  const ENGLISH = "en";
  const CHINESE = "zh-CN";
  const translations = {
    "切换语言": "Switch language",
    "AgentMesh 首页": "AgentMesh home",
    "主导航": "Primary navigation",
    "任务": "Tasks",
    "审批": "Approvals",
    "正在连接": "Connecting",
    "连接设置": "Connection",
    "API 文档": "API Docs",
    "任务中心": "Task Center",
    "创建任务": "Create task",
    "搜索任务": "Search tasks",
    "让一组 Agent": "Move one goal forward",
    "共同推进一个目标": "with a team of agents",
    "创建任务、拆分角色，并在同一处观察每个工作流的依赖、执行状态与结果。": "Create a task, assign roles, and observe dependencies, execution state, and results in one place.",
    "创建第一个任务": "Create your first task",
    "开始执行": "Run",
    "暂停": "Pause",
    "恢复": "Resume",
    "取消": "Cancel",
    "总体进度": "Overall progress",
    "工作单元": "Work units",
    "已完成 / 全部": "Completed / total",
    "并发上限": "Concurrency limit",
    "当前任务": "Current task",
    "运行次数": "Runs",
    "持久化 Runs": "Durable runs",
    "Agent 工作流": "Agent workflow",
    "任务视图": "Task view",
    "任务地图": "Mission Map",
    "工作卡片": "Work cards",
    "交互过滤器": "Interaction filters",
    "地图图例": "Map legend",
    "等待": "Queued",
    "执行中": "Running",
    "已完成": "Completed",
    "异常": "Failed",
    "事件甲板": "Event Deck",
    "执行结果": "Execution result",
    "任务尚未产生输出。": "The task has not produced output yet.",
    "运行记录": "Run history",
    "最新优先": "Newest first",
    "剩余计划治理": "Remaining-plan governance",
    "提出 Plan Patch": "Propose Plan Patch",
    "仅在执行前或预算屏障的安全静止点替换未开始工作；已完成历史、外部副作用与预算边界不可绕过。": "Replace only unstarted work before execution or at a safe budget barrier; completed history, external side effects, and budget boundaries cannot be bypassed.",
    "任务活动时间线": "Task activity timeline",
    "Tool 调用审计": "Tool invocation audit",
    "任务产物": "Task artifacts",
    "选择一个 Agent": "Select an agent",
    "检查执行边界": "to inspect its execution boundary",
    "查看不可变版本、模型策略、能力声明和允许调用的 MCP Tool。": "Inspect immutable versions, model policy, declared capabilities, and allowed MCP tools.",
    "新建版本": "New version",
    "生命周期": "Lifecycle",
    "版本数量": "Versions",
    "不可变快照": "Immutable snapshots",
    "默认版本": "Default version",
    "运行时绑定": "Runtime binding",
    "可见范围": "Visibility",
    "版本与执行策略": "Versions and execution policies",
    "选择一个审批请求": "Select an approval",
    "检查执行意图": "to inspect the action intent",
    "审批绑定规范化参数摘要；任何参数变化都必须重新申请 Permit。": "Approvals bind the canonical argument digest; any argument change requires a new permit.",
    "拒绝": "Reject",
    "批准": "Approve",
    "请求者": "Requester",
    "资源": "Resource",
    "策略版本": "Policy version",
    "复制 Permit": "Copy permit",
    "规范化执行参数": "Canonical action arguments",
    "审批决定": "Approval decisions",
    "选择一个 Artifact": "Select an artifact",
    "检查版本与完整性": "to inspect versions and integrity",
    "浏览安全的文本与 JSON 产物、追溯生产 Run，并验证每个不可变版本的 SHA-256。": "Browse safe text and JSON artifacts, trace producer runs, and verify every immutable version with SHA-256.",
    "添加版本": "Add version",
    "最新媒体类型": "Latest media type",
    "最新大小": "Latest size",
    "不可变内容版本": "Immutable content versions",
    "内容预览": "Content preview",
    "关闭": "Close",
    "创建 Agent 团队任务": "Create an agent-team task",
    "总体目标": "Objective",
    "例如：调研 AgentMesh 的竞品并给出差异化方案": "Example: Research AgentMesh competitors and propose differentiation",
    "执行方式": "Execution mode",
    "多 Agent 协作": "Multi-agent coordination",
    "单 Agent 直接执行": "Direct single-agent execution",
    "最大并发": "Maximum concurrency",
    "角色与工作流": "Roles and workflow",
    "每个工作单元绑定一个已发布的 Agent": "Each work unit binds to a published agent",
    "＋ 添加角色": "+ Add role",
    "创建并查看": "Create and open",
    "启用 Identity/RBAC 时填写 Bearer Token。它只保存在当前浏览器标签页。": "When Identity/RBAC is enabled, provide a Bearer token. It is stored only in this browser tab.",
    "可选": "Optional",
    "保存并重连": "Save and reconnect",
    "更新剩余工作计划": "Update remaining work plan",
    "变更原因": "Change reason",
    "说明为什么需要改变剩余计划": "Explain why the remaining plan must change",
    "候选计划 JSON": "Candidate plan JSON",
    "保留已完成节点的完整定义；剩余节点数和最大并发不能增加。提交后先生成可审计的验证证据，不会立即应用。": "Preserve completed node definitions; remaining node count and maximum concurrency cannot increase. Submission creates auditable verification evidence and does not apply immediately.",
    "验证候选计划": "Verify candidate plan",
    "创建 Agent Definition": "Create Agent Definition",
    "Agent 名称": "Agent name",
    "描述": "Description",
    "说明职责、边界和适用任务": "Describe responsibilities, boundaries, and suitable tasks",
    "标签": "Tags",
    "创建 Definition": "Create definition",
    "创建 Agent Version": "Create Agent Version",
    "语义版本": "Semantic version",
    "角色": "Role",
    "市场研究员": "Market researcher",
    "能力": "Capabilities",
    "系统指令": "System instructions",
    "描述 Agent 的工作方式、约束和输出要求": "Describe how the agent works, its constraints, and output requirements",
    "模型策略": "Model policy",
    "继承部署默认值": "Inherit deployment default",
    "模型": "Model",
    "推理强度": "Reasoning effort",
    "最大输出 Tokens": "Maximum output tokens",
    "可选；SecretReference ID": "Optional; SecretReference ID",
    "模型 Tool 边界": "Model tool boundary",
    "允许的逻辑 Tool Key": "Allowed logical tool keys",
    "最大调用次数": "Maximum calls",
    "创建不可变草稿": "Create immutable draft",
    "发布 Agent Version": "Publish Agent Version",
    "验证通过的能力": "Verified capabilities",
    "设为 Definition 默认版本": "Set as definition default",
    "Policy Approval 开启时必填": "Required when Policy Approval is enabled",
    "申请 Permit": "Request permit",
    "发布版本": "Publish version",
    "审批执行意图": "Review action intent",
    "理由会作为不可变 ApprovalDecision 写入审计记录。": "The reason is written to the audit log as an immutable ApprovalDecision.",
    "决定理由": "Decision reason",
    "说明批准或拒绝的依据": "Explain the basis for approval or rejection",
    "提交决定": "Submit decision",
    "创建安全内联 Artifact": "Create safe inline artifact",
    "显示名称": "Display name",
    "媒体类型": "Media type",
    "可选；用于任务 lineage": "Optional; used for task lineage",
    "内容": "Content",
    "安全 UTF-8 文本或有效 JSON": "Safe UTF-8 text or valid JSON",
    "内容由服务端验证大小、UTF-8/JSON 语法并计算 SHA-256；不支持二进制和主动媒体。": "The server validates size and UTF-8/JSON syntax and computes SHA-256; binary and active media are not supported.",
    "创建 Artifact": "Create artifact",
    "添加 Artifact Version": "Add Artifact Version",
    "新版本的完整内容": "Complete content for the new version",
    "追加不可变版本": "Append immutable version",
    "确定性运行": "Deterministic runtime",
    "{count} 秒前": "{count}s ago",
    "{count} 分钟前": "{count}m ago",
    "{count} 小时前": "{count}h ago",
    "{count} 天前": "{count}d ago",
    "实时连接": "Live",
    "轮询回退": "Polling fallback",
    "已连接": "Connected",
    "连接异常": "Connection error",
    "没有匹配任务": "No matching tasks",
    "还没有任务": "No tasks yet",
    "无法读取 Artifact：": "Unable to load artifacts: ",
    "{count} 版本": "{count} versions",
    "没有匹配 Artifact": "No matching artifacts",
    "还没有 Artifact": "No artifacts yet",
    "无法读取审批：": "Unable to load approvals: ",
    "没有匹配审批": "No matching approvals",
    "还没有审批请求": "No approval requests yet",
    "{count} 已发布": "{count} published",
    "没有匹配 Agent": "No matching agents",
    "还没有 Agent": "No agents yet",
    "Agent 目录": "Agent Catalog",
    "Artifact 目录": "Artifact Catalog",
    "审批队列": "Approval Queue",
    "搜索 Agent": "Search agents",
    "搜索 Artifact": "Search artifacts",
    "搜索审批": "Search approvals",
    "创建 Agent": "Create agent",
    "创建 Artifact": "Create artifact",
    "未填写描述": "No description",
    "还没有 Agent Version。": "No agent versions yet.",
    "部署级策略": "Deployment policy",
    "Tool 预算": "Tool budget",
    "{tools} 个 / {calls} 次": "{tools} tools / {calls} calls",
    "无模型 Tool": "No model tools",
    "默认关闭": "Disabled by default",
    "已验证能力": "Verified capabilities",
    "尚未验证": "Not verified",
    "查看指令与策略 JSON": "View instructions and policy JSON",
    "提交审核": "Submit for review",
    "未绑定": "Not bound",
    "更新于 {time}": "Updated {time}",
    "预览": "Preview",
    "下载": "Download",
    "Artifact 还没有可用版本。": "This artifact has no available versions.",
    "Artifact Version 下载已开始": "Artifact version download started",
    "{count} 个版本": "{count} versions",
    "打开": "Open",
    "当前任务的 Run 尚未绑定 Artifact Version。": "No run in this task is bound to an artifact version.",
    "已消费": "Consumed",
    "已签发": "Issued",
    "未签发": "Not issued",
    "到期 {time}": "Expires {time}",
    "{count} 条记录": "{count} records",
    "尚未作出决定。": "No decision has been made.",
    "批准执行意图": "Approve action intent",
    "拒绝执行意图": "Reject action intent",
    "确认批准": "Confirm approval",
    "确认拒绝": "Confirm rejection",
    "Permit 已签发": "Permit issued",
    "执行意图已拒绝": "Action intent rejected",
    "Permit 已复制；仅可用于完全匹配的操作一次": "Permit copied; it can be used once for the exact matching action",
    "浏览器无法访问剪贴板，请从 API 响应复制 Permit": "The browser cannot access the clipboard; copy the permit from the API response",
    "Artifact 与首个版本已创建": "Artifact and first version created",
    "不可变 Artifact Version 已追加": "Immutable artifact version appended",
    "Agent Definition 已创建": "Agent definition created",
    "Agent Version 草稿已创建": "Agent version draft created",
    "版本已提交审核": "Version submitted for review",
    "发布受 Policy Approval 保护，请填写与本次发布参数完全匹配的一次性 Permit。": "Publishing is protected by Policy Approval. Provide a one-time permit that exactly matches these publish arguments.",
    "当前未启用 Policy Approval；发布仍由 Registry 状态机和 API 权限保护。": "Policy Approval is disabled; publishing remains protected by the Registry state machine and API permissions.",
    "审批请求 {id} 已创建；请由独立 APPROVER 审核。": "Approval request {id} created; an independent APPROVER must review it.",
    "Policy 结果：{result}；{detail}": "Policy result: {result}; {detail}",
    "Permit 已填入。": "Permit populated.",
    "审批请求已创建": "Approval request created",
    "Policy 已完成决策": "Policy decision completed",
    "Agent Version 已发布": "Agent version published",
    "自动刷新 · {time}": "Auto refresh · {time}",
    "错误：{error}": "Error: {error}",
    "最终输出": "Final output",
    "执行异常": "Execution failed",
    "等待执行": "Waiting to run",
    "无法读取计划治理信息：": "Unable to load plan governance: ",
    "应用已验证方案": "Apply verified plan",
    "尚未提出 Plan Patch；任务进入安全静止点后可替换未开始的工作。": "No Plan Patch has been proposed. Unstarted work can be replaced when the task reaches a safe quiescent point.",
    "Plan Patch 已通过安全验证": "Plan Patch passed safety verification",
    "剩余计划已原子替换": "Remaining plan replaced atomically",
    "不可用": "Unavailable",
    "{count} 条事件": "{count} events",
    "无法读取活动时间线：": "Unable to load activity timeline: ",
    "当前任务还没有活动记录。": "This task has no activity records yet.",
    "{count} 次调用": "{count} calls",
    "无法读取 Tool 审计：": "Unable to load tool audit: ",
    "这个任务还没有调用 MCP Tool。": "This task has not invoked an MCP tool.",
    "直接执行": "Direct execution",
    "等待分配": "Awaiting assignment",
    "等待调度": "Awaiting scheduling",
    "依赖": "Depends on",
    "起始节点 · 可立即调度": "Start node · ready to schedule",
    "开始执行后，Run 会出现在这里。": "Runs will appear here after execution starts.",
    "任务已进入执行队列": "Task queued for execution",
    "操作已提交": "Operation submitted",
    "研究员": "Researcher",
    "收集事实、约束与关键背景": "Collect facts, constraints, and key context",
    "分析师": "Analyst",
    "分析材料并形成候选方案": "Analyze evidence and form candidate approaches",
    "整合者": "Synthesizer",
    "综合前序结果，输出最终结论": "Synthesize prior results into the final conclusion",
    "新角色": "New role",
    "工作目标": "Work objective",
    "完成分配的工作": "Complete the assigned work",
    "依赖 Key": "Dependency keys",
    "删除角色": "Remove role",
    "多 Agent 协作至少需要两个角色。": "Multi-agent coordination requires at least two roles.",
    "团队任务已创建": "Team task created"
  };

  const bindings = [];
  let language = localStorage.getItem(STORAGE_KEY) === CHINESE ? CHINESE : ENGLISH;

  function interpolate(value, variables = {}) {
    return Object.entries(variables).reduce(
      (result, [name, replacement]) => result.replaceAll(`{${name}}`, String(replacement)),
      value,
    );
  }

  function t(source, variables = {}) {
    const template = language === CHINESE ? source : (translations[source] || source);
    return interpolate(template, variables);
  }

  function bindText(node) {
    const source = node.nodeValue.trim();
    if (!source || !translations[source]) return;
    const leading = node.nodeValue.match(/^\s*/)[0];
    const trailing = node.nodeValue.match(/\s*$/)[0];
    bindings.push(() => { node.nodeValue = `${leading}${t(source)}${trailing}`; });
  }

  function bindAttribute(element, name) {
    const source = element.getAttribute(name);
    if (!source || !translations[source]) return;
    bindings.push(() => { element.setAttribute(name, t(source)); });
  }

  function bindStatic(root = document) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement?.closest("script, style")) bindText(node);
    }
    root.querySelectorAll("[placeholder], [aria-label], [title]").forEach((element) => {
      ["placeholder", "aria-label", "title"].forEach((name) => {
        if (element.hasAttribute(name)) bindAttribute(element, name);
      });
    });
  }

  function applyLanguage() {
    document.documentElement.lang = language;
    bindings.forEach((apply) => apply());
    const toggle = document.getElementById("language-toggle");
    if (toggle) {
      toggle.textContent = language === ENGLISH ? "中文" : "EN";
      toggle.setAttribute(
        "aria-label",
        language === ENGLISH ? "Switch to Chinese" : "切换为英文",
      );
      toggle.title = toggle.getAttribute("aria-label");
    }
  }

  function setLanguage(nextLanguage) {
    language = nextLanguage === CHINESE ? CHINESE : ENGLISH;
    localStorage.setItem(STORAGE_KEY, language);
    window.location.reload();
  }

  bindStatic();
  applyLanguage();
  document.getElementById("language-toggle")?.addEventListener("click", () => {
    setLanguage(language === ENGLISH ? CHINESE : ENGLISH);
  });

  window.AgentMeshI18n = Object.freeze({
    get language() { return language; },
    t,
    setLanguage,
    supportedLanguages: Object.freeze([ENGLISH, CHINESE]),
  });
})();
