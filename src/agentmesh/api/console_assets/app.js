const state = {
  tasks: [], selectedId: null, selected: null, toolAudit: [], toolAuditError: "",
  agents: [], selectedAgentId: null, selectedAgent: null,
  approvals: [], selectedApprovalId: null, selectedApproval: null, approvalsError: "",
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
function csv(value) { return [...new Set(String(value || "").split(",").map((item) => item.trim()).filter(Boolean))]; }

async function loadFeatures() {
  const result = await api("/api/v1/features");
  state.features = new Map(result.features.map((item) => [item.name, item.enabled]));
  $("agents-nav").classList.toggle("hidden", !featureEnabled("agent_registry_management"));
  $("approvals-nav").classList.toggle("hidden", !featureEnabled("policy_approval"));
  if (!featureEnabled("agent_registry_management") && state.view === "agents") switchView("tasks");
  if (!featureEnabled("policy_approval") && state.view === "approvals") switchView("tasks");
}

async function loadApprovals({ quiet = false } = {}) {
  if (!featureEnabled("policy_approval")) return;
  try {
    const result = await api("/api/v1/approvals?limit=100&offset=0");
    state.approvals = result.items; state.approvalsError = "";
    if (state.view === "approvals") renderSidebarList();
    if (state.selectedApprovalId && state.view === "approvals") selectApproval(state.selectedApprovalId, { renderList: false });
  } catch (error) {
    state.approvalsError = error.message; state.approvals = [];
    if (state.view === "approvals") renderSidebarList();
    if (!quiet) toast(error.message, true);
  }
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
  if (state.view === "approvals") { renderApprovalList(); return; }
  const query = $("search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => task.objective.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.length ? tasks.map((task) => `
    <button class="task-item ${task.id === state.selectedId ? "active" : ""}" data-task-id="${task.id}">
      <strong>${escapeHtml(task.objective)}</strong>
      <div><span class="status-dot ${statusClass(task.status)}">${escapeHtml(task.status)}</span><span>${age(task.updated_at)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? "没有匹配任务" : "还没有任务"}</div>`;
  document.querySelectorAll("[data-task-id]").forEach((node) => node.addEventListener("click", () => selectTask(node.dataset.taskId)));
}

function renderApprovalList() {
  const query = $("search").value.trim().toLowerCase();
  const approvals = state.approvals.filter((item) => `${item.action_type} ${item.requester_id} ${item.resource_type} ${item.resource_id} ${item.approval_status}`.toLowerCase().includes(query));
  $("task-list").innerHTML = state.approvalsError ? `<div class="empty-dag audit-error">无法读取审批：${escapeHtml(state.approvalsError)}</div>` : approvals.length ? approvals.map((item) => `
    <button class="task-item approval-item ${item.id === state.selectedApprovalId ? "active" : ""}" data-approval-action-id="${item.id}">
      <strong>${escapeHtml(item.action_type)}</strong>
      <div><span class="status-dot ${statusClass(item.approval_status)}">${escapeHtml(item.approval_status)}</span><span>${age(item.created_at)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? "没有匹配审批" : "还没有审批请求"}</div>`;
  document.querySelectorAll("[data-approval-action-id]").forEach((node) => node.addEventListener("click", () => selectApproval(node.dataset.approvalActionId)));
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
  const approvals = view === "approvals";
  $("tasks-nav").classList.toggle("active", view === "tasks"); $("agents-nav").classList.toggle("active", agents); $("approvals-nav").classList.toggle("active", approvals);
  $("sidebar-eyebrow").textContent = agents ? "REGISTRY" : approvals ? "GOVERNANCE" : "WORKSPACE";
  $("sidebar-title").textContent = agents ? "Agent 目录" : approvals ? "审批队列" : "任务中心";
  $("search").value = ""; $("search").placeholder = agents ? "搜索 Agent" : approvals ? "搜索审批" : "搜索任务";
  $("search").setAttribute("aria-label", agents ? "搜索 Agent" : approvals ? "搜索审批" : "搜索任务");
  $("new-task-button").classList.toggle("hidden", approvals); $("new-task-button").setAttribute("aria-label", agents ? "创建 Agent" : "创建任务");
  $("empty-state").classList.toggle("hidden", view !== "tasks" || Boolean(state.selectedId));
  $("task-detail").classList.toggle("hidden", view !== "tasks" || !state.selectedId);
  $("agent-empty-state").classList.toggle("hidden", !agents || Boolean(state.selectedAgentId));
  $("agent-detail").classList.toggle("hidden", !agents || !state.selectedAgentId);
  $("approval-empty-state").classList.toggle("hidden", !approvals || Boolean(state.selectedApprovalId));
  $("approval-detail").classList.toggle("hidden", !approvals || !state.selectedApprovalId);
  renderSidebarList();
  if (agents) loadAgents({ quiet: true });
  if (approvals) loadApprovals({ quiet: false });
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

function selectApproval(id, { renderList = true } = {}) {
  const approval = state.approvals.find((item) => item.id === id); if (!approval) return;
  state.selectedApprovalId = id; state.selectedApproval = approval;
  if (renderList) renderApprovalList();
  $("approval-empty-state").classList.add("hidden"); $("approval-detail").classList.remove("hidden");
  renderApprovalDetail(approval);
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
  bindVersionActions();
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
    ${version.status === "DRAFT" ? `<div class="version-actions"><button class="button subtle submit-version" type="button" data-version-id="${version.id}">提交审核</button></div>` : version.status === "IN_REVIEW" ? `<div class="version-actions"><button class="button primary publish-version" type="button" data-version-id="${version.id}">发布版本</button></div>` : ""}
  </article>`;
}

function renderApprovalDetail(action) {
  $("approval-id").textContent = shortId(action.approval_id || action.id); $("approval-action-type").textContent = action.action_type;
  $("approval-status").textContent = action.approval_status; $("approval-result").textContent = action.policy_result;
  $("approval-expiry").textContent = `到期 ${new Date(action.expires_at).toLocaleString()}`;
  $("approval-requester").textContent = action.requester_id; $("approval-resource").textContent = action.resource_type;
  $("approval-resource-id").textContent = action.resource_id; $("approval-policy-version").textContent = action.policy_version;
  $("approval-policy-bundle").textContent = action.policy_bundle; $("approval-action-hash").textContent = shortId(action.action_hash);
  $("approval-action-hash").title = action.action_hash; $("approval-arguments").textContent = JSON.stringify(action.arguments, null, 2);
  const pending = action.approval_status === "PENDING";
  $("approval-actions").classList.toggle("hidden", !pending);
  $("approval-permit-state").textContent = action.permit_id ? (action.approval_status === "CONSUMED" ? "已消费" : "已签发") : "未签发";
  $("copy-permit-button").classList.toggle("hidden", !action.permit_id || action.approval_status === "CONSUMED");
  $("copy-permit-button").dataset.permitId = action.permit_id || "";
  $("approval-decision-count").textContent = `${action.decisions.length} 条记录`;
  $("approval-decisions").innerHTML = action.decisions.length ? [...action.decisions].reverse().map((decision) => `
    <div class="decision-item"><div><strong>${escapeHtml(decision.outcome)}</strong><small>${escapeHtml(decision.approver_id)} · ${age(decision.created_at)}</small></div><p>${escapeHtml(decision.reason)}</p></div>`).join("") : `<div class="empty-dag">尚未作出决定。</div>`;
}

function openDecision(outcome) {
  const action = state.selectedApproval; if (!action?.approval_id) return;
  $("decision-form").reset(); $("decision-approval-id").value = action.approval_id; $("decision-outcome").value = outcome;
  $("decision-title").textContent = outcome === "approve" ? "批准执行意图" : "拒绝执行意图";
  $("decision-submit-button").textContent = outcome === "approve" ? "确认批准" : "确认拒绝";
  $("decision-submit-button").classList.toggle("primary", outcome === "approve"); $("decision-submit-button").classList.toggle("danger", outcome === "reject"); $("decision-error").textContent = "";
  $("decision-dialog").showModal(); setTimeout(() => $("decision-reason").focus(), 50);
}

async function submitDecision(event) {
  event.preventDefault(); $("decision-submit-button").disabled = true; $("decision-error").textContent = "";
  const approvalId = $("decision-approval-id").value; const outcome = $("decision-outcome").value;
  try {
    const decided = await api(`/api/v1/approvals/${approvalId}/${outcome}`, { method: "POST", body: JSON.stringify({ reason: $("decision-reason").value.trim() }) });
    $("decision-dialog").close(); await loadApprovals({ quiet: false }); selectApproval(decided.id); toast(outcome === "approve" ? "Permit 已签发" : "执行意图已拒绝");
  } catch (error) { $("decision-error").textContent = error.message; }
  finally { $("decision-submit-button").disabled = false; }
}

async function copySelectedPermit() {
  const permit = $("copy-permit-button").dataset.permitId; if (!permit) return;
  try { await navigator.clipboard.writeText(permit); toast("Permit 已复制；仅可用于完全匹配的操作一次"); }
  catch { toast("浏览器无法访问剪贴板，请从 API 响应复制 Permit", true); }
}

function bindVersionActions() {
  document.querySelectorAll(".submit-version").forEach((button) => button.addEventListener("click", () => submitVersion(button.dataset.versionId)));
  document.querySelectorAll(".publish-version").forEach((button) => button.addEventListener("click", () => openPublish(button.dataset.versionId)));
}

function openAgentForm() {
  $("agent-form").reset(); $("agent-form-owner").value = "local-user"; $("agent-form-visibility").value = "TENANT";
  $("agent-form-error").textContent = ""; $("agent-dialog").showModal(); setTimeout(() => $("agent-form-name").focus(), 50);
}

async function createAgent(event) {
  event.preventDefault(); $("agent-create-button").disabled = true; $("agent-form-error").textContent = "";
  const payload = { owner_id: $("agent-form-owner").value.trim(), name: $("agent-form-name").value.trim(), description: $("agent-form-description").value.trim(), visibility: $("agent-form-visibility").value, tags: csv($("agent-form-tags").value) };
  try {
    const created = await api("/api/v1/agents", { method: "POST", body: JSON.stringify(payload) });
    $("agent-dialog").close(); await loadAgents({ quiet: false }); selectAgent(created.id); toast("Agent Definition 已创建");
  } catch (error) { $("agent-form-error").textContent = error.message; }
  finally { $("agent-create-button").disabled = false; }
}

function openVersionForm() {
  if (!state.selectedAgent) return;
  $("version-form").reset(); $("version-semver").value = `0.1.${state.selectedAgent.versions.length}`;
  $("version-capabilities").value = "general.task"; $("version-provider").value = "inherit";
  $("version-model").value = "gpt-5.6-terra"; $("version-effort").value = "low"; $("version-max-tokens").value = "1200"; $("version-max-calls").value = "3";
  $("version-form-error").textContent = ""; syncProviderFields(); $("version-dialog").showModal(); setTimeout(() => $("version-role").focus(), 50);
}

function syncProviderFields() {
  const openai = $("version-provider").value === "openai";
  document.querySelectorAll("[data-openai-field]").forEach((field) => field.classList.toggle("hidden", !openai));
}

async function createVersion(event) {
  event.preventDefault(); if (!state.selectedAgentId) return;
  $("version-create-button").disabled = true; $("version-form-error").textContent = "";
  const provider = $("version-provider").value; const tools = csv($("version-tools").value);
  const modelPolicy = provider === "inherit" ? {} : provider === "deterministic" ? { provider } : {
    provider, model: $("version-model").value.trim(), reasoning_effort: $("version-effort").value,
    max_output_tokens: Number($("version-max-tokens").value),
    ...($("version-credential").value.trim() ? { credential_reference_id: $("version-credential").value.trim() } : {})
  };
  const payload = {
    semantic_version: $("version-semver").value.trim(), role: $("version-role").value.trim(), instructions: $("version-instructions").value.trim(),
    declared_capabilities: csv($("version-capabilities").value), input_schema: { type: "object" }, output_schema: { type: "object" },
    model_policy: modelPolicy, tool_profile: tools.length ? { allowed_tools: tools, max_calls: Number($("version-max-calls").value) } : {},
    runtime_adapter: "local", execution_modes: ["async"]
  };
  try {
    await api(`/api/v1/agents/${state.selectedAgentId}/versions`, { method: "POST", body: JSON.stringify(payload) });
    $("version-dialog").close(); await refreshSelectedAgent(); toast("Agent Version 草稿已创建");
  } catch (error) { $("version-form-error").textContent = error.message; }
  finally { $("version-create-button").disabled = false; }
}

async function submitVersion(versionId) {
  try { await api(`/api/v1/agent-versions/${versionId}/submit-review`, { method: "POST" }); await refreshSelectedAgent(); toast("版本已提交审核"); }
  catch (error) { toast(error.message, true); }
}

function openPublish(versionId) {
  const version = state.selectedAgent?.versions.find((item) => item.id === versionId); if (!version) return;
  $("publish-version-id").value = versionId; $("publish-capabilities").value = version.declared_capabilities.join(", ");
  $("publish-default").checked = true; $("publish-permit").value = ""; $("publish-form-error").textContent = "";
  $("publish-request-status").textContent = ""; $("publish-request-status").classList.add("hidden");
  const governed = featureEnabled("policy_approval"); $("publish-permit-field").classList.toggle("hidden", !governed); $("request-publish-approval").classList.toggle("hidden", !governed);
  $("publish-form-note").textContent = governed ? "发布受 Policy Approval 保护，请填写与本次发布参数完全匹配的一次性 Permit。" : "当前未启用 Policy Approval；发布仍由 Registry 状态机和 API 权限保护。";
  $("publish-dialog").showModal();
}

function publishArguments() {
  return { verified_capabilities: csv($("publish-capabilities").value), make_default: $("publish-default").checked };
}

async function requestPublishApproval() {
  const button = $("request-publish-approval"); button.disabled = true; $("publish-form-error").textContent = "";
  try {
    const action = await api("/api/v1/policy/actions", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({
      action_type: "agent.version.publish", resource_type: "agent_version", resource_id: $("publish-version-id").value, arguments: publishArguments()
    }) });
    if (action.permit_id) $("publish-permit").value = action.permit_id;
    const status = $("publish-request-status"); status.classList.remove("hidden");
    status.textContent = action.approval_status === "PENDING" ? `审批请求 ${shortId(action.approval_id)} 已创建；请由独立 APPROVER 审核。` : `Policy 结果：${action.policy_result}；${action.permit_id ? "Permit 已填入。" : action.reason_code}`;
    toast(action.approval_status === "PENDING" ? "审批请求已创建" : "Policy 已完成决策");
  } catch (error) { $("publish-form-error").textContent = error.message; }
  finally { button.disabled = false; }
}

async function publishVersion(event) {
  event.preventDefault(); $("publish-button").disabled = true; $("publish-form-error").textContent = "";
  const permit = $("publish-permit").value.trim(); const headers = permit ? { "Execution-Permit-Id": permit } : {};
  const payload = publishArguments();
  try {
    await api(`/api/v1/agent-versions/${$("publish-version-id").value}/publish`, { method: "POST", headers, body: JSON.stringify(payload) });
    $("publish-dialog").close(); await refreshSelectedAgent(); toast("Agent Version 已发布");
  } catch (error) { $("publish-form-error").textContent = error.message; }
  finally { $("publish-button").disabled = false; }
}

async function refreshSelectedAgent() {
  const id = state.selectedAgentId; await loadAgents({ quiet: false }); if (id) selectAgent(id);
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

$("new-task-button").addEventListener("click", () => state.view === "agents" ? openAgentForm() : openCreate()); $("empty-new-task").addEventListener("click", openCreate);
$("tasks-nav").addEventListener("click", () => switchView("tasks")); $("agents-nav").addEventListener("click", () => switchView("agents")); $("approvals-nav").addEventListener("click", () => switchView("approvals"));
$("new-version-button").addEventListener("click", openVersionForm); $("agent-form").addEventListener("submit", createAgent); $("version-form").addEventListener("submit", createVersion); $("publish-form").addEventListener("submit", publishVersion); $("request-publish-approval").addEventListener("click", requestPublishApproval); $("version-provider").addEventListener("change", syncProviderFields);
$("approve-approval-button").addEventListener("click", () => openDecision("approve")); $("reject-approval-button").addEventListener("click", () => openDecision("reject")); $("decision-form").addEventListener("submit", submitDecision); $("copy-permit-button").addEventListener("click", copySelectedPermit);
$("add-role").addEventListener("click", () => addRole()); $("create-form").addEventListener("submit", createTask);
$("execution-mode").addEventListener("change", (event) => { const coordinated = event.target.value === "COORDINATED"; $("team-fields").classList.toggle("hidden", !coordinated); $("max-concurrency").disabled = !coordinated; });
$("run-button").addEventListener("click", () => taskAction("runs")); $("pause-button").addEventListener("click", () => taskAction("pause")); $("resume-button").addEventListener("click", () => taskAction("resume")); $("cancel-button").addEventListener("click", () => taskAction("cancel"));
$("search").addEventListener("input", renderSidebarList); $("token-button").addEventListener("click", () => { $("token").value = state.token; $("token-dialog").showModal(); });
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
$("token-form").addEventListener("submit", async (event) => { event.preventDefault(); state.token = $("token").value.trim(); state.token ? sessionStorage.setItem("agentmesh-token", state.token) : sessionStorage.removeItem("agentmesh-token"); $("token-dialog").close(); await loadConsole(); });

async function loadConsole() {
  try { await loadFeatures(); await Promise.all([loadTasks(), loadAgents({ quiet: true }), loadApprovals({ quiet: true })]); }
  catch (error) { $("connection").classList.remove("online"); $("connection").lastChild.textContent = "连接异常"; toast(error.message, true); }
}
async function pollConsole() { if (state.view === "agents") await loadAgents({ quiet: true }); else if (state.view === "approvals") await loadApprovals({ quiet: true }); else await loadTasks({ quiet: true }); }
loadConsole(); state.poll = setInterval(pollConsole, 3000);
