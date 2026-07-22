const state = {
  tasks: [], selectedId: null, selected: null, toolAudit: [], toolAuditError: "",
  agents: [], selectedAgentId: null, selectedAgent: null,
  features: new Map(), view: "tasks", poll: null,
  token: sessionStorage.getItem("agentmesh-token") || ""
};
const $ = (id) => document.getElementById(id);
const terminal = new Set(["COMPLETED", "FAILED", "CANCELED"]);
const busy = new Set(["READY", "RUNNING", "REVIEWING", "REVISION_REQUIRED", "PAUSE_REQUESTED"]);

async function api(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.message || `${response.status} ${response.statusText}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}
function age(value) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`; if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`; if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`; return `${Math.floor(seconds / 86400)} 天前`;
}
function shortId(value) { return value ? value.slice(0, 8) : "—"; }
function statusClass(value) { return String(value || "").toLowerCase(); }
function toast(message, error = false) { const node = $("toast"); node.textContent = message; node.className = `toast show${error ? " error" : ""}`; clearTimeout(toast.timer); toast.timer = setTimeout(() => node.className = "toast", 2800); }
function featureEnabled(name) { return state.features.get(name) === true; }
function providerLabel(policy = {}) { return policy.provider === "openai" ? policy.model : policy.provider === "deterministic" ? "确定性运行" : "继承部署默认值"; }

async function loadFeatures() {
  const result = await api("/api/v1/features");
  state.features = new Map(result.features.map((item) => [item.name, item.enabled]));
  $("agents-nav").classList.toggle("hidden", !featureEnabled("agent_registry_management"));
  if (!featureEnabled("agent_registry_management") && state.view === "agents") switchView("tasks");
}

async function loadAgents({ quiet = false } = {}) {
  if (!featureEnabled("agent_registry_management")) return;
  try {
    const result = await api("/api/v1/agents?limit=100&offset=0");
    state.agents = result.items;
    $("agent-options").innerHTML = state.agents.map((agent) => `<option value="${escapeHtml(agent.name)}">${escapeHtml(agent.description)}</option>`).join("");
    if (state.view === "agents") renderSidebarList();
    if (state.selectedAgentId && state.view === "agents") selectAgent(state.selectedAgentId, { renderList: false });
  } catch (error) { if (!quiet) toast(error.message, true); }
}

async function loadTasks({ quiet = false } = {}) {
  try {
    const result = await api("/api/v1/tasks?limit=50&offset=0");
    state.tasks = result.items;
    $("connection").classList.add("online"); $("connection").lastChild.textContent = "已连接";
    if (state.view === "tasks") renderSidebarList();
    if (state.selectedId) await loadTask(state.selectedId, { quiet: true });
  } catch (error) {
    $("connection").classList.remove("online"); $("connection").lastChild.textContent = "连接异常";
    if (!quiet) toast(error.message, true);
  }
}

function renderSidebarList() {
  if (state.view === "agents") { renderAgentList(); return; }
  const query = $("search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => task.objective.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.length ? tasks.map((task) => `
    <button class="task-item ${task.id === state.selectedId ? "active" : ""}" data-task-id="${task.id}">
      <strong>${escapeHtml(task.objective)}</strong>
      <div><span class="status-dot ${statusClass(task.status)}">${escapeHtml(task.status)}</span><span>${age(task.updated_at)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? "没有匹配任务" : "还没有任务"}</div>`;
  document.querySelectorAll("[data-task-id]").forEach((node) => node.addEventListener("click", () => selectTask(node.dataset.taskId)));
}

function renderAgentList() {
  const query = $("search").value.trim().toLowerCase();
  const agents = state.agents.filter((agent) => `${agent.name} ${agent.description} ${agent.tags.join(" ")}`.toLowerCase().includes(query));
  $("task-list").innerHTML = agents.length ? agents.map((agent) => {
    const published = agent.versions.filter((version) => version.status === "PUBLISHED").length;
    return `<button class="task-item agent-item ${agent.id === state.selectedAgentId ? "active" : ""}" data-agent-id="${agent.id}">
      <strong>${escapeHtml(agent.name)}</strong>
      <div><span class="status-dot ${statusClass(agent.lifecycle)}">${escapeHtml(agent.lifecycle)}</span><span>${published} 已发布</span></div>
    </button>`;
  }).join("") : `<div class="empty-dag">${query ? "没有匹配 Agent" : "还没有 Agent"}</div>`;
  document.querySelectorAll("[data-agent-id]").forEach((node) => node.addEventListener("click", () => selectAgent(node.dataset.agentId)));
}

function switchView(view) {
  state.view = view;
  const agents = view === "agents";
  $("tasks-nav").classList.toggle("active", !agents); $("agents-nav").classList.toggle("active", agents);
  $("sidebar-eyebrow").textContent = agents ? "REGISTRY" : "WORKSPACE";
  $("sidebar-title").textContent = agents ? "Agent 目录" : "任务中心";
  $("search").value = ""; $("search").placeholder = agents ? "搜索 Agent" : "搜索任务";
  $("search").setAttribute("aria-label", agents ? "搜索 Agent" : "搜索任务");
  $("new-task-button").classList.toggle("hidden", agents);
  $("empty-state").classList.toggle("hidden", agents || Boolean(state.selectedId));
  $("task-detail").classList.toggle("hidden", agents || !state.selectedId);
  $("agent-empty-state").classList.toggle("hidden", !agents || Boolean(state.selectedAgentId));
  $("agent-detail").classList.toggle("hidden", !agents || !state.selectedAgentId);
  renderSidebarList();
  if (agents) loadAgents({ quiet: true });
}

async function selectTask(id) {
  state.selectedId = id; renderSidebarList();
  $("empty-state").classList.add("hidden"); $("task-detail").classList.remove("hidden");
  await loadTask(id);
}

function selectAgent(id, { renderList = true } = {}) {
  const agent = state.agents.find((item) => item.id === id); if (!agent) return;
  state.selectedAgentId = id; state.selectedAgent = agent;
  if (renderList) renderAgentList();
  $("agent-empty-state").classList.add("hidden"); $("agent-detail").classList.remove("hidden");
  renderAgentDetail(agent);
}

function renderAgentDetail(agent) {
  $("agent-id").textContent = shortId(agent.id); $("agent-name").textContent = agent.name;
  $("agent-description").textContent = agent.description || "未填写描述";
  $("agent-tags").innerHTML = agent.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  $("agent-lifecycle").textContent = agent.lifecycle; $("agent-version-count").textContent = agent.versions.length;
  $("agent-visibility").textContent = agent.visibility;
  const defaultVersion = agent.versions.find((version) => version.id === agent.default_version_id);
  $("agent-default-version").textContent = defaultVersion?.semantic_version || "—";
  const versions = [...agent.versions].sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
  $("agent-version-list").innerHTML = versions.length ? versions.map(renderAgentVersion).join("") : `<div class="empty-dag">还没有 Agent Version。</div>`;
}

function renderAgentVersion(version) {
  const model = version.model_policy || {}; const tools = version.tool_profile?.allowed_tools || [];
  return `<article class="version-card ${version.status === "PUBLISHED" ? "published" : ""}">
    <div class="version-heading"><div><span class="version-number">v${escapeHtml(version.semantic_version)}</span><span class="pill">${escapeHtml(version.status)}</span></div><code>${escapeHtml(shortId(version.content_digest?.replace("sha256:", "")))}</code></div>
    <div class="policy-grid">
      <div><span>角色</span><strong>${escapeHtml(version.role)}</strong></div>
      <div><span>模型</span><strong>${escapeHtml(providerLabel(model))}</strong><small>${model.reasoning_effort ? `${escapeHtml(model.reasoning_effort)} · ${escapeHtml(model.max_output_tokens)} tokens` : "部署级策略"}</small></div>
      <div><span>Tool 预算</span><strong>${tools.length ? `${tools.length} 个 / ${escapeHtml(version.tool_profile.max_calls)} 次` : "无模型 Tool"}</strong><small>${tools.map(escapeHtml).join(" · ") || "默认关闭"}</small></div>
      <div><span>已验证能力</span><strong>${escapeHtml(version.verified_capabilities.join(", ") || "尚未验证")}</strong><small>${escapeHtml(version.runtime_adapter)}</small></div>
    </div>
    <details><summary>查看指令与策略 JSON</summary><pre>${escapeHtml(JSON.stringify({ instructions: version.instructions, model_policy: model, tool_profile: version.tool_profile }, null, 2))}</pre></details>
  </article>`;
}

async function loadTask(id, { quiet = false } = {}) {
  try {
    state.selected = await api(`/api/v1/tasks/${id}`);
    state.toolAudit = []; state.toolAuditError = "";
    if (featureEnabled("mcp_read_tools")) {
      try { state.toolAudit = (await api(`/api/v1/tasks/${id}/tool-invocations`)).items; }
      catch (error) { state.toolAuditError = error.message; }
    }
    renderDetail();
  }
  catch (error) { if (!quiet) toast(error.message, true); }
}

function renderDetail() {
  const task = state.selected; if (!task) return;
  $("task-id").textContent = shortId(task.id); $("task-objective").textContent = task.objective;
  $("task-status").textContent = task.status; $("task-mode").textContent = task.execution_mode;
  $("task-updated").textContent = `更新于 ${age(task.updated_at)}`; $("poll-time").textContent = `自动刷新 · ${new Date().toLocaleTimeString()}`;
  const units = task.subtasks.length || (task.runs.length ? 1 : 0);
  const completed = task.subtasks.length ? task.subtasks.filter((item) => item.status === "COMPLETED").length : (task.status === "COMPLETED" ? 1 : 0);
  const progress = task.status === "COMPLETED" ? 100 : units ? Math.round(completed / units * 100) : 0;
  $("progress-value").textContent = `${progress}%`; $("progress-bar").style.width = `${progress}%`;
  $("unit-count").textContent = `${completed} / ${units}`; $("concurrency").textContent = task.max_concurrency; $("run-count").textContent = task.runs.length;
  $("run-button").disabled = task.status !== "CREATED";
  $("pause-button").disabled = !busy.has(task.status);
  $("resume-button").disabled = !["PAUSED", "WAITING_APPROVAL"].includes(task.status);
  $("cancel-button").disabled = terminal.has(task.status);
  renderDag(task); renderRuns(task); renderToolAudit();
  $("task-output").textContent = task.error ? `错误：${task.error}` : task.output ? JSON.stringify(task.output, null, 2) : "任务尚未产生输出。";
  $("result-label").textContent = task.output ? "最终输出" : task.error ? "执行异常" : "等待执行";
}

function renderToolAudit() {
  const panel = $("tool-audit-panel");
  panel.classList.toggle("hidden", !featureEnabled("mcp_read_tools"));
  if (!featureEnabled("mcp_read_tools")) return;
  $("tool-audit-count").textContent = state.toolAuditError ? "不可用" : `${state.toolAudit.length} 次调用`;
  $("tool-audit-list").innerHTML = state.toolAuditError ? `<div class="empty-dag audit-error">无法读取 Tool 审计：${escapeHtml(state.toolAuditError)}</div>` : state.toolAudit.length ? [...state.toolAudit].reverse().map((item) => `
    <article class="audit-item">
      <span class="audit-marker ${statusClass(item.status)}"></span>
      <div><div class="audit-heading"><strong>${escapeHtml(item.tool_key)}</strong><span class="pill">${escapeHtml(item.status)}</span></div>
      <p>${escapeHtml(item.server_name)} · ${escapeHtml(item.side_effect)} · ${age(item.started_at)}</p>
      <code>invocation ${escapeHtml(shortId(item.id))} · schema ${escapeHtml(shortId(item.schema_digest?.replace("sha256:", "")))}</code>
      ${item.error ? `<small class="audit-error">${escapeHtml(item.error)}</small>` : ""}</div>
    </article>`).join("") : `<div class="empty-dag">这个任务还没有调用 MCP Tool。</div>`;
}

function renderDag(task) {
  const runsBySubtask = new Map(task.runs.filter((run) => run.subtask_id).map((run) => [run.subtask_id, run]));
  if (!task.subtasks.length) {
    const run = task.runs[task.runs.length - 1];
    $("dag").innerHTML = `<article class="work-card ${statusClass(task.status)}"><div class="card-top"><span class="card-key">direct</span><span class="pill">${escapeHtml(task.status)}</span></div><h4>直接执行</h4><p>${escapeHtml(task.objective)}</p><div class="agent-line"><span class="avatar">A</span><div><strong>${escapeHtml(run?.agent_id || "等待分配")}</strong><small>general.task</small></div></div></article>`;
    return;
  }
  $("dag").innerHTML = task.subtasks.map((unit) => {
    const run = runsBySubtask.get(unit.id); const agent = run?.agent_id || unit.preferred_agent_id || "等待调度";
    return `<article class="work-card ${statusClass(unit.status)}">
      <div class="card-top"><span class="card-key">${escapeHtml(unit.key)}</span><span class="pill">${escapeHtml(unit.status)}</span></div>
      <h4>${escapeHtml(unit.input?.role || unit.key)}</h4><p>${escapeHtml(unit.objective)}</p>
      ${unit.depends_on.length ? `<div class="dependency">依赖 → ${unit.depends_on.map(escapeHtml).join(" · ")}</div>` : `<div class="dependency">起始节点 · 可立即调度</div>`}
      <div class="agent-line"><span class="avatar">${escapeHtml(agent.charAt(0).toUpperCase())}</span><div><strong>${escapeHtml(agent)}</strong><small>${escapeHtml(unit.required_capabilities.join(", "))}</small></div></div>
    </article>`;
  }).join("");
}

function renderRuns(task) {
  const subtaskById = new Map(task.subtasks.map((item) => [item.id, item]));
  const runs = [...task.runs].reverse();
  $("run-list").innerHTML = runs.length ? runs.map((run) => {
    const unit = subtaskById.get(run.subtask_id); const label = unit?.input?.role || unit?.key || run.role;
    return `<div class="run-item"><span class="avatar">${escapeHtml(run.agent_id.charAt(0).toUpperCase())}</span><div><strong>${escapeHtml(label)} · ${escapeHtml(run.agent_id)}</strong><small>${escapeHtml(run.role)} · ${age(run.queued_at)}</small></div><span class="pill">${escapeHtml(run.status)}</span></div>`;
  }).join("") : `<div class="empty-dag">开始执行后，Run 会出现在这里。</div>`;
}

async function taskAction(action) {
  if (!state.selectedId) return;
  try { await api(`/api/v1/tasks/${state.selectedId}/${action}`, { method: "POST", headers: action === "runs" ? { "Idempotency-Key": crypto.randomUUID() } : {} }); await loadTasks({ quiet: true }); toast(action === "runs" ? "任务已进入执行队列" : "操作已提交"); }
  catch (error) { toast(error.message, true); }
}

const roleDefaults = [
  { key: "research", role: "研究员", agent: "demo-researcher", objective: "收集事实、约束与关键背景", capability: "general.task" },
  { key: "analysis", role: "分析师", agent: "demo-analyst", objective: "分析材料并形成候选方案", capability: "general.task" },
  { key: "synthesis", role: "整合者", agent: "demo-synthesizer", objective: "综合前序结果，输出最终结论", capability: "general.task", depends: "research,analysis" }
];
function addRole(value = {}) {
  const row = document.createElement("div"); row.className = "role-row";
  row.innerHTML = `<label>角色<input class="role-name" required maxlength="40" value="${escapeHtml(value.role || "新角色")}"></label><label>Agent ID<input class="role-agent" required maxlength="63" list="agent-options" value="${escapeHtml(value.agent || "demo-agent")}"></label><label>工作目标<input class="role-objective" required maxlength="20000" value="${escapeHtml(value.objective || "完成分配的工作")}"></label><label>依赖 Key<input class="role-depends" placeholder="research,analysis" value="${escapeHtml(value.depends || "")}"></label><button class="icon-button remove-role" type="button" aria-label="删除角色">×</button><input class="role-key" type="hidden" value="${escapeHtml(value.key || `role-${crypto.randomUUID().slice(0, 8)}`)}"><input class="role-capability" type="hidden" value="${escapeHtml(value.capability || "general.task")}">`;
  row.querySelector(".remove-role").addEventListener("click", () => row.remove()); $("role-list").appendChild(row);
}
function openCreate() { $("create-form").reset(); $("role-list").innerHTML = ""; roleDefaults.forEach(addRole); $("form-error").textContent = ""; $("create-dialog").showModal(); setTimeout(() => $("objective").focus(), 50); }

async function createTask(event) {
  event.preventDefault(); const mode = $("execution-mode").value; const objective = $("objective").value.trim();
  const rows = [...document.querySelectorAll(".role-row")];
  const subtasks = mode === "COORDINATED" ? rows.map((row, index) => ({
    key: row.querySelector(".role-key").value.replace(/[^a-zA-Z0-9_-]/g, "-").toLowerCase() || `role-${index + 1}`,
    objective: row.querySelector(".role-objective").value.trim(), input: { role: row.querySelector(".role-name").value.trim() },
    required_capabilities: [row.querySelector(".role-capability").value],
    preferred_agent_id: row.querySelector(".role-agent").value.trim(),
    depends_on: row.querySelector(".role-depends").value.split(",").map((item) => item.trim()).filter(Boolean)
  })) : [];
  if (mode === "COORDINATED" && subtasks.length < 2) { $("form-error").textContent = "多 Agent 协作至少需要两个角色。"; return; }
  const payload = { objective, execution_mode: mode, ...(mode === "COORDINATED" ? { subtasks, max_concurrency: Number($("max-concurrency").value) } : {}) };
  $("create-button").disabled = true; $("form-error").textContent = "";
  try { const task = await api("/api/v1/tasks", { method: "POST", body: JSON.stringify(payload) }); $("create-dialog").close(); await loadTasks({ quiet: true }); await selectTask(task.id); toast("团队任务已创建"); }
  catch (error) { $("form-error").textContent = error.message; }
  finally { $("create-button").disabled = false; }
}

$("new-task-button").addEventListener("click", openCreate); $("empty-new-task").addEventListener("click", openCreate);
$("tasks-nav").addEventListener("click", () => switchView("tasks")); $("agents-nav").addEventListener("click", () => switchView("agents"));
$("add-role").addEventListener("click", () => addRole()); $("create-form").addEventListener("submit", createTask);
$("execution-mode").addEventListener("change", (event) => { const coordinated = event.target.value === "COORDINATED"; $("team-fields").classList.toggle("hidden", !coordinated); $("max-concurrency").disabled = !coordinated; });
$("run-button").addEventListener("click", () => taskAction("runs")); $("pause-button").addEventListener("click", () => taskAction("pause")); $("resume-button").addEventListener("click", () => taskAction("resume")); $("cancel-button").addEventListener("click", () => taskAction("cancel"));
$("search").addEventListener("input", renderSidebarList); $("token-button").addEventListener("click", () => { $("token").value = state.token; $("token-dialog").showModal(); });
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
$("token-form").addEventListener("submit", async (event) => { event.preventDefault(); state.token = $("token").value.trim(); state.token ? sessionStorage.setItem("agentmesh-token", state.token) : sessionStorage.removeItem("agentmesh-token"); $("token-dialog").close(); await loadConsole(); });

async function loadConsole() {
  try { await loadFeatures(); await Promise.all([loadTasks(), loadAgents({ quiet: true })]); }
  catch (error) { $("connection").classList.remove("online"); $("connection").lastChild.textContent = "连接异常"; toast(error.message, true); }
}
async function pollConsole() { if (state.view === "agents") await loadAgents({ quiet: true }); else await loadTasks({ quiet: true }); }
loadConsole(); state.poll = setInterval(pollConsole, 3000);
