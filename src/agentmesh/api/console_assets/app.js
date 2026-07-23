function storedMissionFilter() {
  const fallback = { transport: "ALL", agent: "ALL", status: "ALL", kind: "ALL", trace: "" };
  try { return { ...fallback, ...JSON.parse(sessionStorage.getItem("agentmesh-mission-filter") || "{}") }; }
  catch { return fallback; }
}

function storedMissionBookmarks() {
  try { return JSON.parse(localStorage.getItem("agentmesh-mission-bookmarks") || "{}"); }
  catch { return {}; }
}

const state = {
  tasks: [], selectedId: null, selected: null, toolAudit: [], toolAuditError: "",
  agents: [], selectedAgentId: null, selectedAgent: null,
  artifacts: [], selectedArtifactId: null, selectedArtifact: null, artifactsError: "",
  approvals: [], selectedApprovalId: null, selectedApproval: null, approvalsError: "",
  activity: [], activityError: "", interactions: [], interactionError: "", planning: null, planningError: "",
  features: new Map(), view: "tasks", poll: null, streamAbort: null, streamCursor: "",
  streamGeneration: 0, streamConnected: false, streamRetryMs: 1000, reconnectTimer: null, refreshTimer: null,
  missionView: "map", missionSelectedId: null, missionPulses: [], missionFilter: storedMissionFilter(),
  missionReplay: { mode: "live", cursor: -1, playing: false, timer: null }, missionBookmarks: storedMissionBookmarks(),
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
    const previous = state.selected?.id === id ? state.selected : null;
    const next = await api(`/api/v1/tasks/${id}`);
    if (previous && state.missionReplay.mode === "live") deriveMissionPulses(previous, next);
    else if (!previous) { state.missionSelectedId = null; state.missionPulses = []; resetMissionReplay(); }
    state.selected = next;
    state.toolAudit = []; state.toolAuditError = "";
    state.activity = []; state.activityError = "";
    state.interactions = []; state.interactionError = "";
    state.planning = null; state.planningError = "";
    if (featureEnabled("dynamic_replanning") && state.selected.execution_mode === "COORDINATED") {
      try { state.planning = await api(`/api/v1/tasks/${id}/planning`); }
      catch (error) { state.planningError = error.message; }
    }
    if (featureEnabled("activity_timeline")) {
      try { state.activity = (await api(`/api/v1/tasks/${id}/activity?limit=100`)).items; }
      catch (error) { state.activityError = error.message; }
      try { state.interactions = (await api(`/api/v1/tasks/${id}/interactions?limit=100`)).items; }
      catch (error) { state.interactionError = error.message; }
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
  renderMissionMap(task); renderDag(task); renderRuns(task); renderPlanning(); renderActivityTimeline(); renderToolAudit(); renderTaskArtifacts();
  $("task-output").textContent = task.error ? `错误：${task.error}` : task.output ? JSON.stringify(task.output, null, 2) : "任务尚未产生输出。";
  $("result-label").textContent = task.output ? "最终输出" : task.error ? "执行异常" : "等待执行";
}

function renderPlanning() {
  const panel = $("planning-panel"); const task = state.selected;
  const enabled = featureEnabled("dynamic_replanning") && task?.execution_mode === "COORDINATED";
  panel.classList.toggle("hidden", !enabled); if (!enabled) return;
  $("planning-version").textContent = task.plan_version ? `Plan v${task.plan_version}` : "";
  $("propose-plan-patch").disabled = !["CREATED", "WAITING_APPROVAL"].includes(task.status) || !state.planning;
  if (state.planningError) { $("plan-patch-list").innerHTML = `<div class="empty-dag audit-error">无法读取计划治理信息：${escapeHtml(state.planningError)}</div>`; return; }
  const patches = state.planning?.patches || [];
  $("plan-patch-list").innerHTML = patches.length ? [...patches].reverse().map((patch) => `
    <article class="plan-patch-card">
      <div class="audit-heading"><strong>v${patch.base_plan_version} → v${patch.proposed_plan_version}</strong><span class="pill ${statusClass(patch.status)}">${escapeHtml(patch.status)}</span></div>
      <p>${escapeHtml(patch.reason)} · ${escapeHtml(patch.requested_by)} · ${age(patch.created_at)}</p>
      <div class="plan-evidence">${patch.evidence.map((finding) => `<span class="${finding.passed ? "passed" : "failed"}" title="${escapeHtml(`${finding.message} · ${JSON.stringify(finding.details || {})}`)}">${finding.passed ? "✓" : "×"} ${escapeHtml(finding.code)}</span>`).join("")}</div>
      ${patch.status === "VERIFIED" ? `<button class="button subtle apply-plan-patch" data-patch-id="${patch.id}" type="button">应用已验证方案</button>` : ""}
    </article>`).join("") : `<div class="empty-dag">尚未提出 Plan Patch；任务进入安全静止点后可替换未开始的工作。</div>`;
  document.querySelectorAll(".apply-plan-patch").forEach((node) => node.addEventListener("click", () => applyPlanPatch(node.dataset.patchId)));
}

function openPlanPatchForm() {
  const task = state.selected; if (!task || !state.planning) return;
  const plan = { max_concurrency: task.max_concurrency, subtasks: task.subtasks.map((unit) => ({
    key: unit.key, objective: unit.objective, input: unit.input || {}, required_capabilities: unit.required_capabilities,
    preferred_agent_id: unit.preferred_agent_id, depends_on: unit.depends_on
  })) };
  $("plan-patch-requester").value = "console-user"; $("plan-patch-reason").value = "";
  $("plan-patch-json").value = JSON.stringify(plan, null, 2); $("plan-patch-error").textContent = "";
  $("plan-patch-dialog").showModal(); setTimeout(() => $("plan-patch-reason").focus(), 50);
}

async function submitPlanPatch(event) {
  event.preventDefault(); $("plan-patch-submit").disabled = true; $("plan-patch-error").textContent = "";
  try {
    const proposed = JSON.parse($("plan-patch-json").value);
    await api(`/api/v1/tasks/${state.selectedId}/plan-patches`, { method: "POST", body: JSON.stringify({
      base_plan_version: state.selected.plan_version, base_plan_digest: state.selected.plan_digest,
      reason: $("plan-patch-reason").value.trim(), requested_by: $("plan-patch-requester").value.trim(),
      max_concurrency: proposed.max_concurrency, subtasks: proposed.subtasks
    }) });
    $("plan-patch-dialog").close(); await loadTask(state.selectedId); toast("Plan Patch 已通过安全验证");
  } catch (error) { $("plan-patch-error").textContent = error.message; }
  finally { $("plan-patch-submit").disabled = false; }
}

async function applyPlanPatch(patchId) {
  try { await api(`/api/v1/tasks/${state.selectedId}/plan-patches/${patchId}/apply`, { method: "POST" }); await loadTask(state.selectedId); toast("剩余计划已原子替换"); }
  catch (error) { toast(error.message, true); }
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

function stopMissionReplay() {
  clearInterval(state.missionReplay.timer); state.missionReplay.timer = null; state.missionReplay.playing = false;
}

function resetMissionReplay() {
  stopMissionReplay(); state.missionReplay.mode = "live"; state.missionReplay.cursor = -1;
}

function missionReplayReached(time, id, task) {
  return Boolean(time) && missionReplayIncludes({ time, id }, task);
}

function missionReplayTask(task) {
  if (state.missionReplay.mode === "live") return task;
  const runs = task.runs.filter((run) => missionReplayReached(run.queued_at, `${run.id}-queued`, task)).map((run) => {
    const completed = missionReplayReached(run.completed_at, `${run.id}-done`, task);
    const started = missionReplayReached(run.started_at, `${run.id}-started`, task);
    return {
      ...run,
      status: completed ? run.status : started ? "RUNNING" : "READY",
      started_at: started ? run.started_at : null,
      completed_at: completed ? run.completed_at : null
    };
  });
  const projectedRuns = new Map();
  runs.forEach((run) => { if (run.subtask_id) projectedRuns.set(run.subtask_id, run); });
  const subtasks = task.subtasks.map((unit) => {
    const run = projectedRuns.get(unit.id);
    const status = !run ? "CREATED" : run.status === "SUCCEEDED" ? "COMPLETED" : run.status;
    return { ...unit, status, current_run_id: run?.id || null };
  });
  const anyStarted = runs.some((run) => run.status !== "READY");
  const terminalReached = terminal.has(task.status) && missionReplayReached(task.updated_at, `${task.id}-terminal`, task);
  return { ...task, status: terminalReached ? task.status : anyStarted ? "RUNNING" : runs.length ? "READY" : "CREATED", subtasks, runs };
}

function missionReplayBookmarks(task) {
  const validIds = new Set(missionReplayEvents(task).map((event) => event.id));
  return (state.missionBookmarks[task.id] || []).filter((id) => validIds.has(id));
}

function saveMissionReplayBookmarks() {
  localStorage.setItem("agentmesh-mission-bookmarks", JSON.stringify(state.missionBookmarks));
}

function renderMissionReplay(task) {
  const panel = $("mission-replay"); const events = missionReplayEvents(task); panel.classList.toggle("hidden", !events.length);
  if (!events.length) return;
  const live = state.missionReplay.mode === "live";
  if (live) state.missionReplay.cursor = events.length - 1;
  else state.missionReplay.cursor = Math.max(0, Math.min(state.missionReplay.cursor, events.length - 1));
  const current = events[state.missionReplay.cursor];
  $("mission-replay-range").max = Math.max(0, events.length - 1); $("mission-replay-range").value = state.missionReplay.cursor;
  $("mission-replay-live").classList.toggle("active", live);
  $("mission-replay-back").disabled = !events.length || (!live && state.missionReplay.cursor === 0);
  $("mission-replay-forward").disabled = live || state.missionReplay.cursor >= events.length - 1;
  $("mission-replay-toggle").textContent = live ? "Pause" : state.missionReplay.playing ? "Pause" : "Play";
  $("mission-replay-label").textContent = live ? `Live · ${events.length} events` : `${state.missionReplay.cursor + 1}/${events.length} · ${new Date(current.time).toLocaleTimeString()} · ${current.title}`;
  $("mission-live-beacon").classList.toggle("replay", !live); $("mission-live-label").textContent = live ? "LIVE" : "REPLAY";
  const bookmarks = missionReplayBookmarks(task);
  $("mission-replay-bookmarks").innerHTML = `<option value="">Bookmarks (${bookmarks.length})</option>${bookmarks.map((id) => {
    const event = events.find((candidate) => candidate.id === id);
    return `<option value="${escapeHtml(id)}"${event?.id === current?.id && !live ? " selected" : ""}>${escapeHtml(`${new Date(event.time).toLocaleTimeString()} · ${event.title}`)}</option>`;
  }).join("")}`;
  $("mission-replay-bookmark").disabled = !current;
}

function setMissionReplayCursor(index) {
  const events = state.selected ? missionReplayEvents(state.selected) : [];
  if (!events.length) return;
  stopMissionReplay(); state.missionReplay.mode = "replay"; state.missionReplay.cursor = Math.max(0, Math.min(Number(index), events.length - 1));
  renderMissionMap(state.selected);
}

function stepMissionReplay(delta) {
  const events = state.selected ? missionReplayEvents(state.selected) : [];
  if (!events.length) return;
  const start = state.missionReplay.mode === "live" ? events.length - 1 : state.missionReplay.cursor;
  setMissionReplayCursor(start + delta);
}

function toggleMissionReplay() {
  if (!state.selected) return;
  const events = missionReplayEvents(state.selected); if (!events.length) return;
  if (state.missionReplay.mode === "live") { setMissionReplayCursor(events.length - 1); return; }
  if (state.missionReplay.playing) { stopMissionReplay(); renderMissionMap(state.selected); return; }
  if (state.missionReplay.cursor >= events.length - 1) state.missionReplay.cursor = 0;
  state.missionReplay.playing = true;
  state.missionReplay.timer = setInterval(() => {
    const latest = missionReplayEvents(state.selected);
    if (state.missionReplay.cursor >= latest.length - 1) { stopMissionReplay(); renderMissionMap(state.selected); return; }
    state.missionReplay.cursor += 1; renderMissionMap(state.selected);
  }, 900);
  renderMissionMap(state.selected);
}

function setMissionLive() {
  if (!state.selected) return;
  resetMissionReplay(); renderMissionMap(state.selected);
}

function bookmarkMissionReplay() {
  if (!state.selected) return;
  const events = missionReplayEvents(state.selected); const event = events[state.missionReplay.cursor];
  if (!event) return;
  const bookmarks = new Set(state.missionBookmarks[state.selected.id] || []); bookmarks.add(event.id);
  state.missionBookmarks[state.selected.id] = [...bookmarks]; saveMissionReplayBookmarks(); renderMissionMap(state.selected);
  toast("Replay bookmark saved");
}

function exportMissionReplay() {
  if (!state.selected) return;
  const task = state.selected; const events = missionReplayEvents(task);
  const payload = {
    schema: "agentmesh.mission-replay.v1", exported_at: new Date().toISOString(),
    task: { id: task.id, objective: task.objective, execution_mode: task.execution_mode, created_at: task.created_at, updated_at: task.updated_at },
    events, interactions: state.interactions, bookmark_event_ids: missionReplayBookmarks(task)
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })); const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `agentmesh-mission-${task.id}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
  toast("Sanitized mission replay exported");
}

function missionUnits(task) {
  if (task.subtasks.length) return task.subtasks;
  const run = task.runs[task.runs.length - 1];
  return [{
    id: "direct", key: "direct", objective: task.objective, input: { role: "Direct executor" },
    required_capabilities: [run?.role || "general.task"], preferred_agent_id: run?.agent_id || null,
    depends_on: [], status: task.status, current_run_id: run?.id || null
  }];
}

function missionRunsBySubtask(task) {
  const result = new Map();
  task.runs.forEach((run) => { if (run.subtask_id) result.set(run.subtask_id, run); });
  if (!task.subtasks.length && task.runs.length) result.set("direct", task.runs[task.runs.length - 1]);
  return result;
}

function missionLayout(task) {
  const units = missionUnits(task); const byKey = new Map(units.map((unit) => [unit.key, unit]));
  const depthMemo = new Map();
  function depth(unit, visiting = new Set()) {
    if (depthMemo.has(unit.key)) return depthMemo.get(unit.key);
    if (visiting.has(unit.key)) return 0;
    const nextVisiting = new Set(visiting); nextVisiting.add(unit.key);
    const value = unit.depends_on.length ? Math.max(...unit.depends_on.map((key) => byKey.has(key) ? depth(byKey.get(key), nextVisiting) + 1 : 0)) : 0;
    depthMemo.set(unit.key, value); return value;
  }
  const groups = new Map();
  units.forEach((unit) => { const level = depth(unit); if (!groups.has(level)) groups.set(level, []); groups.get(level).push(unit); });
  const maxDepth = Math.max(0, ...groups.keys()); const maxRows = Math.max(1, ...[...groups.values()].map((items) => items.length));
  const width = Math.max(780, 455 + maxDepth * 235); const stageHeight = Math.max(430, 90 + maxRows * 140);
  const externalEndpoints = missionExternalEndpoints(); const dockRows = Math.ceil(externalEndpoints.length / 3);
  const height = stageHeight + (dockRows ? 65 + dockRows * 92 : 0);
  const positions = new Map();
  groups.forEach((items, level) => {
    const gap = stageHeight / (items.length + 1);
    items.forEach((unit, index) => positions.set(unit.key, { x: 245 + level * 235, y: Math.round(gap * (index + 1) - 47) }));
  });
  const externalPositions = new Map();
  externalEndpoints.forEach((endpoint, index) => externalPositions.set(missionEndpointKey(endpoint), {
    x: 155 + (index % 3) * 215, y: stageHeight + 44 + Math.floor(index / 3) * 92, width: 170
  }));
  return { units, byKey, positions, externalEndpoints, externalPositions, width, height, stageHeight, hq: { x: 30, y: Math.round(stageHeight / 2 - 52) } };
}

function missionPath(source, target) {
  const sourceWidth = source.hq ? 160 : (source.width || 180); const sourceHeight = source.external ? 64 : (source.hq ? 104 : 94);
  const targetHeight = target.external ? 64 : (target.hq ? 104 : 94);
  const sx = source.x + sourceWidth / 2; const sy = source.y + sourceHeight / 2;
  const tx = target.x + (target.hq ? 80 : (target.width || 180) / 2); const ty = target.y + targetHeight / 2;
  const dx = tx - sx; const dy = ty - sy; const bend = Math.max(38, Math.min(120, Math.abs(dx) * .42 + Math.abs(dy) * .12));
  if (Math.abs(dy) > Math.abs(dx)) return `M ${sx} ${sy} C ${sx} ${sy + Math.sign(dy) * bend}, ${tx} ${ty - Math.sign(dy) * bend}, ${tx} ${ty}`;
  return `M ${sx} ${sy} C ${sx + Math.sign(dx || 1) * bend} ${sy}, ${tx - Math.sign(dx || 1) * bend} ${ty}, ${tx} ${ty}`;
}

function missionRouteStatus(source, target) {
  if (target.status === "FAILED" || source.status === "FAILED") return "failed";
  if (target.status === "RUNNING" || target.status === "READY") return "active";
  if (source.status === "COMPLETED" && target.status === "COMPLETED") return "completed";
  return "queued";
}

function missionRunEvents(task) {
  const units = new Map(task.subtasks.map((unit) => [unit.id, unit]));
  const items = [];
  task.runs.forEach((run) => {
    const unit = units.get(run.subtask_id); const label = unit?.input?.role || unit?.key || run.role || "task";
    if (run.queued_at) items.push({ id: `${run.id}-queued`, time: run.queued_at, status: "QUEUED", title: `${run.agent_id} dispatched`, detail: `${label} · run ${shortId(run.id)}` });
    if (run.started_at) items.push({ id: `${run.id}-started`, time: run.started_at, status: "RUNNING", title: `${run.agent_id} started`, detail: label });
    if (run.completed_at) items.push({ id: `${run.id}-done`, time: run.completed_at, status: run.status, title: `${run.agent_id} ${run.status.toLowerCase()}`, detail: label });
  });
  return items;
}

function missionInteractionEvent(event) {
  return {
    id: event.id, time: event.occurred_at, status: event.status,
    title: missionInteractionTitle(event),
    detail: `${event.transport} · ${event.source.label || event.source.type} → ${event.target.label || event.target.type}`,
    interaction: true
  };
}

function missionReplayCompare(left, right) {
  const time = new Date(left.time).getTime() - new Date(right.time).getTime();
  return time || String(left.id).localeCompare(String(right.id));
}

function missionReplayEvents(task) {
  return [...missionRunEvents(task), ...state.interactions.map(missionInteractionEvent)].sort(missionReplayCompare);
}

function missionReplayCursorEvent(task) {
  if (state.missionReplay.mode === "live") return null;
  const events = missionReplayEvents(task);
  return events[Math.max(0, Math.min(state.missionReplay.cursor, events.length - 1))] || null;
}

function missionReplayIncludes(event, task = state.selected) {
  if (!task || state.missionReplay.mode === "live") return true;
  const cursor = missionReplayCursorEvent(task);
  return cursor ? missionReplayCompare(event, cursor) <= 0 : false;
}

function missionEventItems(task) {
  const items = [
    ...(!missionFiltersActive() ? missionRunEvents(task) : []),
    ...missionVisibleInteractions().map(missionInteractionEvent)
  ];
  return items.filter((event) => missionReplayIncludes(event, task)).sort((left, right) => missionReplayCompare(right, left)).slice(0, 10);
}

function missionEndpointKey(endpoint) { return `${endpoint.type}:${endpoint.id}`; }

function missionFiltersActive() {
  const filter = state.missionFilter;
  return filter.transport !== "ALL" || filter.agent !== "ALL" || filter.status !== "ALL" || filter.kind !== "ALL" || Boolean(filter.trace);
}

function missionVisibleInteractions() {
  const filter = state.missionFilter; const trace = filter.trace.trim().toLowerCase();
  return state.interactions.filter((event) =>
    (filter.transport === "ALL" || event.transport === filter.transport) &&
    (filter.agent === "ALL" || event.source.id === filter.agent || event.target.id === filter.agent) &&
    (filter.status === "ALL" || event.status === filter.status) &&
    (filter.kind === "ALL" || event.kind === filter.kind) &&
    (!trace || (event.trace_id || "").toLowerCase().includes(trace)) &&
    missionReplayIncludes(missionInteractionEvent(event))
  );
}

function missionFilterOptions(values, selected, allLabel) {
  return [`<option value="ALL">${escapeHtml(allLabel)}</option>`, ...values.map((value) => `<option value="${escapeHtml(value.value)}"${value.value === selected ? " selected" : ""}>${escapeHtml(value.label)}</option>`)].join("");
}

function renderMissionFilters(task) {
  const panel = $("mission-filters"); panel.classList.toggle("hidden", !state.interactions.length);
  if (!state.interactions.length) return;
  const transports = [...new Set(state.interactions.map((event) => event.transport))].sort().map((value) => ({ value, label: value }));
  const statuses = [...new Set(state.interactions.map((event) => event.status))].sort().map((value) => ({ value, label: value }));
  const kinds = [...new Set(state.interactions.map((event) => event.kind))].sort().map((value) => ({ value, label: missionInteractionTitle({ kind: value }) }));
  const agents = missionUnits(task).map((unit) => ({ value: unit.id, label: unit.input?.role || unit.key }));
  const ensure = (key, values) => { if (state.missionFilter[key] !== "ALL" && !values.some((item) => item.value === state.missionFilter[key])) state.missionFilter[key] = "ALL"; };
  ensure("transport", transports); ensure("agent", agents); ensure("status", statuses); ensure("kind", kinds);
  $("mission-filter-transport").innerHTML = missionFilterOptions(transports, state.missionFilter.transport, "All transports");
  $("mission-filter-agent").innerHTML = missionFilterOptions(agents, state.missionFilter.agent, "All agents");
  $("mission-filter-status").innerHTML = missionFilterOptions(statuses, state.missionFilter.status, "All statuses");
  $("mission-filter-kind").innerHTML = missionFilterOptions(kinds, state.missionFilter.kind, "All events");
  $("mission-filter-trace").value = state.missionFilter.trace;
  $("mission-filter-reset").disabled = !missionFiltersActive();
}

function updateMissionFilter(key, value) {
  state.missionFilter[key] = value; sessionStorage.setItem("agentmesh-mission-filter", JSON.stringify(state.missionFilter));
  if (state.selected) renderMissionMap(state.selected);
}

function missionExternalEndpoints() {
  const endpoints = new Map();
  missionVisibleInteractions().forEach((event) => [event.source, event.target].forEach((endpoint) => {
    if (!["TASK", "SUBTASK"].includes(endpoint.type)) endpoints.set(missionEndpointKey(endpoint), endpoint);
  }));
  return [...endpoints.values()].slice(0, 9);
}

function missionInteractionTitle(event) {
  const titles = {
    HANDOFF_REQUESTED: "Context handoff requested", HANDOFF_ACCEPTED: "Context handoff accepted", HANDOFF_REJECTED: "Context handoff rejected",
    MCP_TOOL_STARTED: "MCP tool invoked", MCP_TOOL_COMPLETED: "MCP result returned",
    A2A_DELEGATION_PREPARED: "A2A delegation prepared", A2A_DELEGATION_STATE: "A2A remote state updated",
    APPROVAL_GATE_CREATED: "Approval gate created", APPROVAL_GATE_DECIDED: "Approval gate decided",
    PLAN_PATCH_VERIFIED: "Plan Patch verified", PLAN_PATCH_APPLIED: "Plan Patch applied"
  };
  return titles[event.kind] || event.kind.replaceAll("_", " ").toLowerCase();
}

function missionInteractionPoint(endpoint, layout) {
  if (endpoint.type === "TASK") return { ...layout.hq, hq: true };
  if (endpoint.type === "SUBTASK") {
    const unit = layout.units.find((candidate) => candidate.id === endpoint.id);
    return unit ? layout.positions.get(unit.key) : null;
  }
  const point = layout.externalPositions.get(missionEndpointKey(endpoint));
  return point ? { ...point, external: true } : null;
}

function missionInteractionRoutes(layout) {
  const unique = new Map();
  missionVisibleInteractions().forEach((event) => {
    const pair = [missionEndpointKey(event.source), missionEndpointKey(event.target)].sort().join("|");
    const key = `${event.transport}:${pair}`;
    if (!unique.has(key)) unique.set(key, event);
  });
  return [...unique.values()].map((event, index) => {
    const source = missionInteractionPoint(event.source, layout); const target = missionInteractionPoint(event.target, layout);
    if (!source || !target) return "";
    const path = missionPath(source, target); const transport = event.transport.toLowerCase().replace("_", "-");
    const packet = `<circle class="interaction-packet ${transport}" r="4"><animateMotion dur="${2.2 + index * .15}s" begin="${index * .18}s" path="${path}" repeatCount="indefinite"/></circle>`;
    return `<path class="interaction-route ${transport} ${statusClass(event.status)}" d="${path}"/>${packet}`;
  }).join("");
}

function deriveMissionPulses(previous, next) {
  const previousRunIds = new Set(previous.runs.map((run) => run.id));
  const nextById = new Map(next.subtasks.map((unit) => [unit.id, unit]));
  const previousByKey = new Map(previous.subtasks.map((unit) => [unit.key, unit]));
  const additions = [];
  next.runs.filter((run) => !previousRunIds.has(run.id) && run.subtask_id).forEach((run) => {
    const target = nextById.get(run.subtask_id); if (target) additions.push({ id: `${run.id}-dispatch`, type: "dispatch", targetKey: target.key });
  });
  next.subtasks.forEach((unit) => {
    const oldStatus = previousByKey.get(unit.key)?.status;
    if (oldStatus !== "COMPLETED" && unit.status === "COMPLETED") {
      next.subtasks.filter((candidate) => candidate.depends_on.includes(unit.key)).forEach((target) => additions.push({ id: `${unit.id}-${target.id}-output`, type: "output", sourceKey: unit.key, targetKey: target.key }));
    }
  });
  const activeIds = new Set(state.missionPulses.map((item) => item.id)); const uniqueAdditions = additions.filter((item) => !activeIds.has(item.id));
  if (!uniqueAdditions.length) return;
  state.missionPulses.push(...uniqueAdditions);
  const ids = new Set(uniqueAdditions.map((item) => item.id));
  setTimeout(() => {
    state.missionPulses = state.missionPulses.filter((item) => !ids.has(item.id));
    if (state.selected?.id === next.id) renderMissionMap(state.selected);
  }, 2200);
}

function setMissionView(view) {
  state.missionView = view;
  $("mission-view").classList.toggle("hidden", view !== "map"); $("board-view").classList.toggle("hidden", view !== "board");
  $("mission-view-button").classList.toggle("active", view === "map"); $("board-view-button").classList.toggle("active", view === "board");
}

function renderMissionInspector(task, layout, runsBySubtask) {
  const selected = layout.units.find((unit) => unit.id === state.missionSelectedId) || layout.units[0];
  state.missionSelectedId = selected?.id || null;
  if (!selected) { $("mission-inspector").innerHTML = `<div class="empty-dag">No execution unit is available.</div>`; return; }
  const run = runsBySubtask.get(selected.id); const agent = run?.agent_id || selected.preferred_agent_id || "Awaiting dispatch";
  const dependencies = selected.depends_on.length ? selected.depends_on.join(" → ") : "HQ dispatch route";
  $("mission-inspector").innerHTML = `
    <div class="inspector-head"><span class="eyebrow">ACTIVE UNIT</span><span class="pill ${statusClass(selected.status)}">${escapeHtml(selected.status)}</span></div>
    <h3>${escapeHtml(selected.input?.role || selected.key)}</h3><p>${escapeHtml(selected.objective)}</p>
    <div class="mission-inspector-grid"><div><span>Agent</span><strong>${escapeHtml(agent)}</strong></div><div><span>Station</span><strong>${escapeHtml(selected.key)}</strong></div><div><span>Route</span><strong>${escapeHtml(dependencies)}</strong></div><div><span>Capability</span><strong>${escapeHtml(selected.required_capabilities.join(", ") || "general.task")}</strong></div>${run ? `<div><span>Run</span><strong>${escapeHtml(shortId(run.id))} · ${escapeHtml(run.status)}</strong></div>` : ""}</div>`;
}

function renderMissionEvents(task) {
  const events = missionEventItems(task); $("mission-event-count").textContent = `${events.length} signals`;
  $("mission-event-list").innerHTML = state.interactionError ? `<div class="empty-dag audit-error">Governed interaction stream unavailable: ${escapeHtml(state.interactionError)}</div>` : events.length ? events.map((event) => `
    <article class="mission-event ${statusClass(event.status)}"><i></i><div><strong>${escapeHtml(event.title)}</strong><small>${escapeHtml(event.detail)} · ${new Date(event.time).toLocaleTimeString()}</small></div></article>`).join("") : `<div class="empty-dag">Run the task to see durable dispatch and completion signals.</div>`;
}

function renderMissionMap(task) {
  setMissionView(state.missionView); renderMissionFilters(task); renderMissionReplay(task);
  const projectedTask = missionReplayTask(task); const visibleInteractions = missionVisibleInteractions(); const layout = missionLayout(projectedTask); const runsBySubtask = missionRunsBySubtask(projectedTask);
  if (!layout.units.some((unit) => unit.id === state.missionSelectedId)) state.missionSelectedId = layout.units.find((unit) => unit.status === "RUNNING")?.id || layout.units[0]?.id || null;
  const routes = [];
  layout.units.forEach((unit) => {
    const target = layout.positions.get(unit.key);
    if (!unit.depends_on.length) routes.push(`<path class="mission-route ${missionRouteStatus({ status: projectedTask.status }, unit)}" d="${missionPath({ ...layout.hq, hq: true }, target)}"/>`);
    unit.depends_on.forEach((key) => { const sourceUnit = layout.byKey.get(key); const source = layout.positions.get(key); if (sourceUnit && source) routes.push(`<path class="mission-route ${missionRouteStatus(sourceUnit, unit)}" d="${missionPath(source, target)}"/>`); });
  });
  const stations = layout.units.map((unit) => {
    const point = layout.positions.get(unit.key); const run = runsBySubtask.get(unit.id); const agent = run?.agent_id || unit.preferred_agent_id || "awaiting-dispatch";
    return `<g class="mission-station ${statusClass(unit.status)}${unit.id === state.missionSelectedId ? " selected" : ""}" data-mission-node="${escapeHtml(unit.id)}" transform="translate(${point.x} ${point.y})" role="button" tabindex="0" aria-label="${escapeHtml(`${unit.input?.role || unit.key}, ${unit.status}`)}">
      <rect class="station-base" width="180" height="94" rx="15"/><circle class="station-orbit" cx="25" cy="28" r="16"/><circle class="station-avatar" cx="25" cy="28" r="12"/><text class="station-initial" x="25" y="28">${escapeHtml(agent.charAt(0).toUpperCase())}</text><text class="station-role" x="49" y="27">${escapeHtml(unit.input?.role || unit.key)}</text><text class="station-agent" x="49" y="45">${escapeHtml(agent)}</text><text class="station-state" x="16" y="75">${escapeHtml(unit.status)}</text><text class="station-agent" x="164" y="75" text-anchor="end">${escapeHtml(unit.key)}</text>
    </g>`;
  }).join("");
  const externalNodes = layout.externalEndpoints.map((endpoint) => {
    const point = layout.externalPositions.get(missionEndpointKey(endpoint));
    const event = visibleInteractions.find((item) => missionEndpointKey(item.source) === missionEndpointKey(endpoint) || missionEndpointKey(item.target) === missionEndpointKey(endpoint));
    const transport = event?.transport || endpoint.type; const symbol = endpoint.type === "TOOL" ? "T" : endpoint.type === "PEER" ? "A" : endpoint.type === "APPROVAL" ? "G" : "P";
    return `<g class="mission-external ${escapeHtml(transport.toLowerCase())} ${statusClass(event?.status || "QUEUED")}" transform="translate(${point.x} ${point.y})">
      <rect width="170" height="64" rx="13"/><circle cx="24" cy="23" r="12"/><text class="external-symbol" x="24" y="23">${symbol}</text><text class="external-kind" x="45" y="21">${escapeHtml(transport)}</text><text class="external-label" x="45" y="39">${escapeHtml(endpoint.label || shortId(endpoint.id))}</text><text class="external-status" x="14" y="54">${escapeHtml(event?.status || "AVAILABLE")}</text>
    </g>`;
  }).join("");
  const interactionRoutes = missionInteractionRoutes(layout);
  const pulses = state.missionPulses.map((pulse) => {
    const target = layout.positions.get(pulse.targetKey); const source = pulse.sourceKey ? layout.positions.get(pulse.sourceKey) : { ...layout.hq, hq: true };
    return source && target ? `<circle class="mission-pulse ${pulse.type}" r="6"><animateMotion dur="1.8s" path="${missionPath(source, target)}" fill="freeze"/></circle>` : "";
  }).join("");
  const dockDivider = layout.externalEndpoints.length ? `<path class="interaction-dock-line" d="M 28 ${layout.stageHeight + 18} H ${layout.width - 28}"/><text class="interaction-dock-title" x="36" y="${layout.stageHeight + 11}">GOVERNED INTERACTION DOCK</text>` : "";
  $("mission-canvas").innerHTML = `<svg viewBox="0 0 ${layout.width} ${layout.height}" role="img" aria-label="Agent task execution map"><defs><marker id="mission-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#59646e"/></marker><filter id="station-shadow"><feDropShadow dx="0" dy="5" stdDeviation="6" flood-opacity=".3"/></filter><filter id="pulse-glow"><feGaussianBlur stdDeviation="3" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>${routes.join("")}${interactionRoutes}${dockDivider}<g class="mission-station mission-hq ${statusClass(projectedTask.status)}" transform="translate(${layout.hq.x} ${layout.hq.y})"><rect class="station-base" width="160" height="104" rx="18"/><circle class="station-avatar" cx="28" cy="30" r="14"/><text class="station-initial" x="28" y="30">M</text><text class="station-role" x="50" y="29">AGENTMESH HQ</text><text class="station-agent" x="50" y="47">Control plane</text><text class="station-state" x="18" y="78">${escapeHtml(projectedTask.status)}</text><text class="station-agent" x="142" y="78" text-anchor="end">PLAN v${escapeHtml(projectedTask.plan_version || 1)}</text></g>${stations}${externalNodes}${pulses}</svg>`;
  const running = layout.units.filter((unit) => unit.status === "RUNNING").length; const completed = layout.units.filter((unit) => unit.status === "COMPLETED").length;
  const interactionCount = visibleInteractions.length === state.interactions.length ? `${state.interactions.length} interactions` : `${visibleInteractions.length}/${state.interactions.length} interactions`;
  const replaySummary = state.missionReplay.mode === "live" ? "" : `replay ${state.missionReplay.cursor + 1}/${missionReplayEvents(task).length} · `;
  $("mission-map-summary").textContent = `${replaySummary}${layout.units.length} agents · ${interactionCount} · ${running} running · ${completed} complete`;
  document.querySelectorAll("[data-mission-node]").forEach((node) => {
    const select = () => { state.missionSelectedId = node.dataset.missionNode; renderMissionMap(task); };
    node.addEventListener("click", select); node.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
  });
  renderMissionInspector(projectedTask, layout, runsBySubtask); renderMissionEvents(task);
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
$("propose-plan-patch").addEventListener("click", openPlanPatchForm); $("plan-patch-form").addEventListener("submit", submitPlanPatch);
$("execution-mode").addEventListener("change", (event) => { const coordinated = event.target.value === "COORDINATED"; $("team-fields").classList.toggle("hidden", !coordinated); $("max-concurrency").disabled = !coordinated; });
$("run-button").addEventListener("click", () => taskAction("runs")); $("pause-button").addEventListener("click", () => taskAction("pause")); $("resume-button").addEventListener("click", () => taskAction("resume")); $("cancel-button").addEventListener("click", () => taskAction("cancel"));
$("mission-view-button").addEventListener("click", () => setMissionView("map")); $("board-view-button").addEventListener("click", () => setMissionView("board"));
$("mission-filter-transport").addEventListener("change", (event) => updateMissionFilter("transport", event.target.value));
$("mission-filter-agent").addEventListener("change", (event) => updateMissionFilter("agent", event.target.value));
$("mission-filter-status").addEventListener("change", (event) => updateMissionFilter("status", event.target.value));
$("mission-filter-kind").addEventListener("change", (event) => updateMissionFilter("kind", event.target.value));
$("mission-filter-trace").addEventListener("input", (event) => updateMissionFilter("trace", event.target.value));
$("mission-filter-reset").addEventListener("click", () => { state.missionFilter = { transport: "ALL", agent: "ALL", status: "ALL", kind: "ALL", trace: "" }; sessionStorage.removeItem("agentmesh-mission-filter"); if (state.selected) renderMissionMap(state.selected); });
$("mission-replay-live").addEventListener("click", setMissionLive);
$("mission-replay-back").addEventListener("click", () => stepMissionReplay(-1));
$("mission-replay-toggle").addEventListener("click", toggleMissionReplay);
$("mission-replay-forward").addEventListener("click", () => stepMissionReplay(1));
$("mission-replay-range").addEventListener("input", (event) => setMissionReplayCursor(event.target.value));
$("mission-replay-bookmark").addEventListener("click", bookmarkMissionReplay);
$("mission-replay-bookmarks").addEventListener("change", (event) => {
  if (!state.selected || !event.target.value) return;
  const index = missionReplayEvents(state.selected).findIndex((item) => item.id === event.target.value);
  if (index >= 0) setMissionReplayCursor(index);
});
$("mission-replay-export").addEventListener("click", exportMissionReplay);
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
