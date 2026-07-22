const state = {
  tasks: [], selectedId: null, selected: null, toolAudit: [], toolAuditError: "",
  agents: [], selectedAgentId: null, selectedAgent: null,
  artifacts: [], selectedArtifactId: null, selectedArtifact: null, artifactsError: "",
  approvals: [], selectedApprovalId: null, selectedApproval: null, approvalsError: "",
  activity: [], activityError: "",
  features: new Map(), view: "tasks", poll: null, streamAbort: null, streamCursor: "",
  streamGeneration: 0, streamConnected: false, streamRetryMs: 1000, reconnectTimer: null, refreshTimer: null,
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

async function artifactContent(versionId) {
  const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const response = await fetch(`/api/v1/artifact-versions/${versionId}/content`, { headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.message || `${response.status} ${response.statusText}`);
  }
  const text = await response.text();
  return { text, mediaType: response.headers.get("Content-Type")?.split(";")[0] || "text/plain" };
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
function base64Utf8(value) {
  const bytes = new TextEncoder().encode(value); let binary = "";
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return btoa(binary);
}
function bytesLabel(value) { return value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KiB`; }
function updateConnection(online = true) {
  $("connection").classList.toggle("online", online);
  $("connection").lastChild.textContent = online ? (featureEnabled("realtime_events") ? (state.streamConnected ? "实时连接" : "轮询回退") : "已连接") : "连接异常";
}

async function loadFeatures() {
  const result = await api("/api/v1/features");
  state.features = new Map(result.features.map((item) => [item.name, item.enabled]));
  $("agents-nav").classList.toggle("hidden", !featureEnabled("agent_registry_management"));
  $("artifacts-nav").classList.toggle("hidden", !featureEnabled("artifact_service"));
  $("approvals-nav").classList.toggle("hidden", !featureEnabled("policy_approval"));
  if (!featureEnabled("agent_registry_management") && state.view === "agents") switchView("tasks");
  if (!featureEnabled("artifact_service") && state.view === "artifacts") switchView("tasks");
  if (!featureEnabled("policy_approval") && state.view === "approvals") switchView("tasks");
}

async function loadArtifacts({ quiet = false } = {}) {
  if (!featureEnabled("artifact_service")) return;
  try {
    const result = await api("/api/v1/artifacts?limit=100&offset=0");
    state.artifacts = result.items; state.artifactsError = "";
    if (state.view === "artifacts") renderSidebarList();
    if (state.selectedArtifactId && state.view === "artifacts") selectArtifact(state.selectedArtifactId, { renderList: false });
    if (state.selected) renderTaskArtifacts();
  } catch (error) {
    state.artifacts = []; state.artifactsError = error.message;
    if (state.view === "artifacts") renderSidebarList();
    if (!quiet) toast(error.message, true);
  }
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
    updateConnection(true);
    if (state.view === "tasks") renderSidebarList();
    if (state.selectedId) await loadTask(state.selectedId, { quiet: true });
  } catch (error) {
    updateConnection(false);
    if (!quiet) toast(error.message, true);
  }
}

function renderSidebarList() {
  if (state.view === "agents") { renderAgentList(); return; }
  if (state.view === "artifacts") { renderArtifactList(); return; }
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

function renderArtifactList() {
  const query = $("search").value.trim().toLowerCase();
  const artifacts = state.artifacts.filter((item) => `${item.display_name} ${item.kind} ${item.classification} ${item.owner_id}`.toLowerCase().includes(query));
  $("task-list").innerHTML = state.artifactsError ? `<div class="empty-dag audit-error">无法读取 Artifact：${escapeHtml(state.artifactsError)}</div>` : artifacts.length ? artifacts.map((item) => `
    <button class="task-item artifact-item ${item.id === state.selectedArtifactId ? "active" : ""}" data-artifact-id="${item.id}">
      <strong>${escapeHtml(item.display_name)}</strong>
      <div><span class="status-dot available">${escapeHtml(item.kind)}</span><span>${item.version_count} 版本</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? "没有匹配 Artifact" : "还没有 Artifact"}</div>`;
  document.querySelectorAll("[data-artifact-id]").forEach((node) => node.addEventListener("click", () => selectArtifact(node.dataset.artifactId)));
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
  const artifacts = view === "artifacts";
  const approvals = view === "approvals";
  $("tasks-nav").classList.toggle("active", view === "tasks"); $("agents-nav").classList.toggle("active", agents); $("artifacts-nav").classList.toggle("active", artifacts); $("approvals-nav").classList.toggle("active", approvals);
  $("sidebar-eyebrow").textContent = agents ? "REGISTRY" : artifacts ? "EVIDENCE" : approvals ? "GOVERNANCE" : "WORKSPACE";
  $("sidebar-title").textContent = agents ? "Agent 目录" : artifacts ? "Artifact 目录" : approvals ? "审批队列" : "任务中心";
  $("search").value = ""; $("search").placeholder = agents ? "搜索 Agent" : artifacts ? "搜索 Artifact" : approvals ? "搜索审批" : "搜索任务";
  $("search").setAttribute("aria-label", agents ? "搜索 Agent" : artifacts ? "搜索 Artifact" : approvals ? "搜索审批" : "搜索任务");
  $("new-task-button").classList.toggle("hidden", approvals); $("new-task-button").setAttribute("aria-label", agents ? "创建 Agent" : artifacts ? "创建 Artifact" : "创建任务");
  $("empty-state").classList.toggle("hidden", view !== "tasks" || Boolean(state.selectedId));
  $("task-detail").classList.toggle("hidden", view !== "tasks" || !state.selectedId);
  $("agent-empty-state").classList.toggle("hidden", !agents || Boolean(state.selectedAgentId));
  $("agent-detail").classList.toggle("hidden", !agents || !state.selectedAgentId);
  $("artifact-empty-state").classList.toggle("hidden", !artifacts || Boolean(state.selectedArtifactId));
  $("artifact-detail").classList.toggle("hidden", !artifacts || !state.selectedArtifactId);
  $("approval-empty-state").classList.toggle("hidden", !approvals || Boolean(state.selectedApprovalId));
  $("approval-detail").classList.toggle("hidden", !approvals || !state.selectedApprovalId);
  renderSidebarList();
  if (agents) loadAgents({ quiet: true });
  if (artifacts) loadArtifacts({ quiet: false });
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

function selectArtifact(id, { renderList = true } = {}) {
  const artifact = state.artifacts.find((item) => item.id === id); if (!artifact) return;
  const changed = state.selectedArtifactId !== id;
  state.selectedArtifactId = id; state.selectedArtifact = artifact;
  if (renderList) renderArtifactList();
  $("artifact-empty-state").classList.add("hidden"); $("artifact-detail").classList.remove("hidden");
  renderArtifactDetail(artifact, { resetPreview: changed });
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

function renderArtifactDetail(artifact, { resetPreview = true } = {}) {
  const versions = [...artifact.versions].sort((left, right) => right.version_number - left.version_number); const latest = versions[0];
  $("artifact-id").textContent = shortId(artifact.id); $("artifact-name").textContent = artifact.display_name;
  $("artifact-kind").textContent = artifact.kind; $("artifact-classification").textContent = artifact.classification;
  $("artifact-updated").textContent = `更新于 ${age(artifact.updated_at)}`; $("artifact-version-count").textContent = artifact.version_count;
  $("artifact-owner").textContent = artifact.owner_id; $("artifact-media-type").textContent = latest?.media_type || "—";
  $("artifact-size").textContent = latest ? bytesLabel(latest.size_bytes) : "—"; if (resetPreview) $("artifact-preview-panel").classList.add("hidden");
  $("artifact-version-list").innerHTML = versions.length ? versions.map((version) => `
    <article class="artifact-version-card">
      <div class="version-heading"><div><span class="version-number">v${version.version_number}</span><span class="pill">${escapeHtml(version.status)}</span></div><code title="${escapeHtml(version.sha256)}">sha256:${escapeHtml(shortId(version.sha256))}</code></div>
      <div class="artifact-version-meta"><span>${escapeHtml(version.media_type)}</span><span>${bytesLabel(version.size_bytes)}</span><span>${escapeHtml(version.storage_class)}</span><span>Scan: ${escapeHtml(version.scan_status)}</span></div>
      <div class="artifact-lineage"><span>Producer Run</span><code>${escapeHtml(version.producer_run_id || "未绑定")}</code><small>${new Date(version.created_at).toLocaleString()}</small></div>
      <div class="version-actions"><button class="button subtle preview-artifact-version" type="button" data-preview-version="${version.id}">预览</button><button class="button subtle download-artifact-version" type="button" data-download-version="${version.id}" data-version-number="${version.version_number}" data-media-type="${escapeHtml(version.media_type)}">下载</button></div>
    </article>`).join("") : `<div class="empty-dag">Artifact 还没有可用版本。</div>`;
  bindArtifactVersionActions();
}

function bindArtifactVersionActions() {
  document.querySelectorAll("[data-preview-version]").forEach((button) => button.addEventListener("click", () => previewArtifactVersion(button.dataset.previewVersion)));
  document.querySelectorAll("[data-download-version]").forEach((button) => button.addEventListener("click", () => downloadArtifactVersion(button.dataset.downloadVersion, button.dataset.versionNumber, button.dataset.mediaType)));
}

async function previewArtifactVersion(versionId) {
  try {
    const content = await artifactContent(versionId); let preview = content.text;
    if (content.mediaType === "application/json") preview = JSON.stringify(JSON.parse(preview), null, 2);
    $("artifact-preview-title").textContent = `${content.mediaType} · ${shortId(versionId)}`; $("artifact-preview-content").textContent = preview;
    $("artifact-preview-panel").classList.remove("hidden"); $("artifact-preview-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) { toast(error.message, true); }
}

async function downloadArtifactVersion(versionId, versionNumber, mediaType) {
  try {
    const content = await artifactContent(versionId); const extension = mediaType === "application/json" ? "json" : "txt";
    const url = URL.createObjectURL(new Blob([content.text], { type: content.mediaType })); const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `artifact-${state.selectedArtifactId}-v${versionNumber}.${extension}`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
    toast("Artifact Version 下载已开始");
  } catch (error) { toast(error.message, true); }
}

function renderTaskArtifacts() {
  const panel = $("task-artifact-panel"); panel.classList.toggle("hidden", !featureEnabled("artifact_service"));
  if (!featureEnabled("artifact_service") || !state.selected) return;
  const runIds = new Set(state.selected.runs.map((run) => run.id));
  const linked = state.artifacts.flatMap((artifact) => artifact.versions.filter((version) => version.producer_run_id && runIds.has(version.producer_run_id)).map((version) => ({ artifact, version })));
  $("task-artifact-count").textContent = `${linked.length} 个版本`;
  $("task-artifact-list").innerHTML = linked.length ? linked.map(({ artifact, version }) => `
    <article class="artifact-lineage-item"><div><strong>${escapeHtml(artifact.display_name)} · v${version.version_number}</strong><small>${escapeHtml(artifact.kind)} · ${escapeHtml(version.media_type)} · ${bytesLabel(version.size_bytes)}</small></div><code>${escapeHtml(shortId(version.sha256))}</code><button class="button subtle open-linked-artifact" type="button" data-linked-artifact="${artifact.id}">打开</button></article>`).join("") : `<div class="empty-dag">当前任务的 Run 尚未绑定 Artifact Version。</div>`;
  document.querySelectorAll("[data-linked-artifact]").forEach((button) => button.addEventListener("click", () => { switchView("artifacts"); selectArtifact(button.dataset.linkedArtifact); }));
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

function openArtifactForm() {
  $("artifact-form").reset(); $("artifact-form-kind").value = "report"; $("artifact-form-classification").value = "INTERNAL"; $("artifact-form-media-type").value = "text/plain";
  $("artifact-form-error").textContent = ""; $("artifact-dialog").showModal(); setTimeout(() => $("artifact-form-name").focus(), 50);
}

async function createArtifact(event) {
  event.preventDefault(); $("artifact-create-button").disabled = true; $("artifact-form-error").textContent = "";
  const runId = $("artifact-form-run-id").value.trim(); const payload = {
    display_name: $("artifact-form-name").value.trim(), kind: $("artifact-form-kind").value.trim(), classification: $("artifact-form-classification").value,
    media_type: $("artifact-form-media-type").value, content_base64: base64Utf8($("artifact-form-content").value), ...(runId ? { producer_run_id: runId } : {})
  };
  try {
    const created = await api("/api/v1/artifacts", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(payload) });
    $("artifact-dialog").close(); await loadArtifacts({ quiet: false }); selectArtifact(created.id); toast("Artifact 与首个版本已创建");
  } catch (error) { $("artifact-form-error").textContent = error.message; }
  finally { $("artifact-create-button").disabled = false; }
}

function openArtifactVersionForm() {
  if (!state.selectedArtifact) return; const latest = [...state.selectedArtifact.versions].sort((left, right) => right.version_number - left.version_number)[0];
  $("artifact-version-form").reset(); $("artifact-version-media-type").value = latest?.media_type || "text/plain"; $("artifact-version-error").textContent = "";
  $("artifact-version-dialog").showModal(); setTimeout(() => $("artifact-version-content").focus(), 50);
}

async function createArtifactVersion(event) {
  event.preventDefault(); if (!state.selectedArtifactId) return;
  $("artifact-version-create-button").disabled = true; $("artifact-version-error").textContent = ""; const runId = $("artifact-version-run-id").value.trim();
  const payload = { media_type: $("artifact-version-media-type").value, content_base64: base64Utf8($("artifact-version-content").value), ...(runId ? { producer_run_id: runId } : {}) };
  try {
    const updated = await api(`/api/v1/artifacts/${state.selectedArtifactId}/versions`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(payload) });
    $("artifact-version-dialog").close(); await loadArtifacts({ quiet: false }); selectArtifact(updated.id); toast("不可变 Artifact Version 已追加");
  } catch (error) { $("artifact-version-error").textContent = error.message; }
  finally { $("artifact-version-create-button").disabled = false; }
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
    state.activity = []; state.activityError = "";
    if (featureEnabled("activity_timeline")) {
      try { state.activity = (await api(`/api/v1/tasks/${id}/activity?limit=100`)).items; }
      catch (error) { state.activityError = error.message; }
    }
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
  renderDag(task); renderRuns(task); renderActivityTimeline(); renderToolAudit(); renderTaskArtifacts();
  $("task-output").textContent = task.error ? `错误：${task.error}` : task.output ? JSON.stringify(task.output, null, 2) : "任务尚未产生输出。";
  $("result-label").textContent = task.output ? "最终输出" : task.error ? "执行异常" : "等待执行";
}

function renderActivityTimeline() {
  const panel = $("activity-panel");
  panel.classList.toggle("hidden", !featureEnabled("activity_timeline"));
  if (!featureEnabled("activity_timeline")) return;
  $("activity-count").textContent = state.activityError ? "不可用" : `${state.activity.length} 条事件`;
  $("activity-list").innerHTML = state.activityError ? `<div class="empty-dag audit-error">无法读取活动时间线：${escapeHtml(state.activityError)}</div>` : state.activity.length ? state.activity.map((item) => {
    const details = Object.entries(item.details || {}).slice(0, 4).map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(String(value))}`).join(" · ");
    return `<article class="audit-item activity-item">
      <span class="audit-marker ${statusClass(item.status)}"></span>
      <div><div class="audit-heading"><strong>${escapeHtml(item.title)}</strong><span class="pill">${escapeHtml(item.status)}</span></div>
      <p>${escapeHtml(item.category.toUpperCase())} · ${new Date(item.occurred_at).toLocaleString()}${item.actor ? ` · ${escapeHtml(item.actor)}` : ""}</p>
      ${details ? `<small class="activity-details">${details}</small>` : ""}
      <code>${escapeHtml(item.entity_type)} ${escapeHtml(shortId(item.entity_id))}${item.trace_id ? ` · trace ${escapeHtml(shortId(item.trace_id))}` : ""}</code></div>
    </article>`;
  }).join("") : `<div class="empty-dag">当前任务还没有活动记录。</div>`;
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

$("new-task-button").addEventListener("click", () => state.view === "agents" ? openAgentForm() : state.view === "artifacts" ? openArtifactForm() : openCreate()); $("empty-new-task").addEventListener("click", openCreate);
$("tasks-nav").addEventListener("click", () => switchView("tasks")); $("agents-nav").addEventListener("click", () => switchView("agents")); $("artifacts-nav").addEventListener("click", () => switchView("artifacts")); $("approvals-nav").addEventListener("click", () => switchView("approvals"));
$("new-version-button").addEventListener("click", openVersionForm); $("agent-form").addEventListener("submit", createAgent); $("version-form").addEventListener("submit", createVersion); $("publish-form").addEventListener("submit", publishVersion); $("request-publish-approval").addEventListener("click", requestPublishApproval); $("version-provider").addEventListener("change", syncProviderFields);
$("artifact-form").addEventListener("submit", createArtifact); $("new-artifact-version-button").addEventListener("click", openArtifactVersionForm); $("artifact-version-form").addEventListener("submit", createArtifactVersion); $("close-artifact-preview").addEventListener("click", () => $("artifact-preview-panel").classList.add("hidden"));
$("approve-approval-button").addEventListener("click", () => openDecision("approve")); $("reject-approval-button").addEventListener("click", () => openDecision("reject")); $("decision-form").addEventListener("submit", submitDecision); $("copy-permit-button").addEventListener("click", copySelectedPermit);
$("add-role").addEventListener("click", () => addRole()); $("create-form").addEventListener("submit", createTask);
$("execution-mode").addEventListener("change", (event) => { const coordinated = event.target.value === "COORDINATED"; $("team-fields").classList.toggle("hidden", !coordinated); $("max-concurrency").disabled = !coordinated; });
$("run-button").addEventListener("click", () => taskAction("runs")); $("pause-button").addEventListener("click", () => taskAction("pause")); $("resume-button").addEventListener("click", () => taskAction("resume")); $("cancel-button").addEventListener("click", () => taskAction("cancel"));
$("search").addEventListener("input", renderSidebarList); $("token-button").addEventListener("click", () => { $("token").value = state.token; $("token-dialog").showModal(); });
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
$("token-form").addEventListener("submit", async (event) => { event.preventDefault(); state.token = $("token").value.trim(); state.token ? sessionStorage.setItem("agentmesh-token", state.token) : sessionStorage.removeItem("agentmesh-token"); $("token-dialog").close(); await loadConsole(); });

function stopUpdates() {
  state.streamGeneration += 1; state.streamConnected = false; state.streamCursor = "";
  if (state.streamAbort) state.streamAbort.abort(); state.streamAbort = null;
  clearInterval(state.poll); state.poll = null; clearTimeout(state.reconnectTimer); clearTimeout(state.refreshTimer);
}

function configureUpdates() {
  stopUpdates(); const generation = state.streamGeneration;
  state.poll = setInterval(pollConsole, featureEnabled("realtime_events") ? 15000 : 3000);
  if (featureEnabled("realtime_events")) connectRealtime(generation);
  else updateConnection(true);
}

function scheduleActiveRefresh() {
  clearTimeout(state.refreshTimer); state.refreshTimer = setTimeout(() => pollConsole(), 150);
}

function processSseBlock(block) {
  let event = "message"; let eventId = ""; const data = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("id:")) eventId = line.slice(3).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  if (eventId) state.streamCursor = eventId;
  if (event === "domain") scheduleActiveRefresh();
  if (event === "unavailable") throw new Error(data.join("\n") || "Realtime Stream unavailable");
}

async function connectRealtime(generation) {
  const controller = new AbortController(); state.streamAbort = controller;
  const headers = { Accept: "text/event-stream", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...(state.streamCursor ? { "Last-Event-ID": state.streamCursor } : {}) };
  try {
    const response = await fetch("/api/v1/events", { headers, signal: controller.signal });
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => null); throw new Error(payload?.message || payload?.detail || `${response.status} ${response.statusText}`);
    }
    state.streamConnected = true; state.streamRetryMs = 1000; updateConnection(true);
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (generation === state.streamGeneration) {
      const { value, done } = await reader.read(); if (done) throw new Error("Realtime Stream closed");
      buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2); if (block.trim()) processSseBlock(block);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (error) {
    if (controller.signal.aborted || generation !== state.streamGeneration) return;
    state.streamConnected = false; updateConnection(true);
    const delay = state.streamRetryMs; state.streamRetryMs = Math.min(state.streamRetryMs * 2, 15000);
    state.reconnectTimer = setTimeout(() => connectRealtime(generation), delay);
  }
}

async function loadConsole() {
  stopUpdates();
  try { await loadFeatures(); await Promise.all([loadTasks(), loadAgents({ quiet: true }), loadArtifacts({ quiet: true }), loadApprovals({ quiet: true })]); configureUpdates(); }
  catch (error) { $("connection").classList.remove("online"); $("connection").lastChild.textContent = "连接异常"; toast(error.message, true); }
}
async function pollConsole() { if (state.view === "agents") await loadAgents({ quiet: true }); else if (state.view === "artifacts") await loadArtifacts({ quiet: true }); else if (state.view === "approvals") await loadApprovals({ quiet: true }); else await loadTasks({ quiet: true }); }
loadConsole();
