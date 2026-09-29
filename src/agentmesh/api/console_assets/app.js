const t = (source, variables = {}) => window.AgentMeshI18n.t(source, variables);

function clientRequestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function storedMissionFilter() {
  const fallback = { transport: "ALL", agent: "ALL", status: "ALL", kind: "ALL", trace: "" };
  try { return { ...fallback, ...JSON.parse(sessionStorage.getItem("agentmesh-mission-filter") || "{}") }; }
  catch { return fallback; }
}

function storedMissionBookmarks() {
  try { return JSON.parse(localStorage.getItem("agentmesh-mission-bookmarks") || "{}"); }
  catch { return {}; }
}
function storedModelConnectionTests() {
  try { return new Map(Object.entries(JSON.parse(sessionStorage.getItem("agentmesh-model-connection-tests") || "{}"))); }
  catch { return new Map(); }
}

const state = {
  tasks: [], selectedId: null, selected: null, toolAudit: [], toolAuditError: "",
  agents: [], selectedAgentId: null, selectedAgent: null,
  tools: [], selectedToolKey: null, selectedTool: null, toolsError: "", catalogPreview: null,
  artifacts: [], selectedArtifactId: null, selectedArtifact: null, artifactsError: "",
  approvals: [], selectedApprovalId: null, selectedApproval: null, approvalsError: "",
  companyTemplate: null, companyTemplateError: "",
  companyOperations: null, companyOperationsError: "",
  companyWorkforce: null, companyWorkforceError: "",
  marketResearch: null, marketResearchError: "",
  memoryCompany: null, memoryCompanies: [], selectedMemoryCompanyId: null, memoryRecords: [], memoryPolicies: [], memoryRetrievals: [], memoryError: "",
  selectedMemoryId: null,
  activity: [], activityError: "", interactions: [], interactionError: "", planning: null, planningError: "",
  features: new Map(), featureItems: [], modelConnections: [], modelReadiness: null, modelConnectionTests: storedModelConnectionTests(), memorySetup: null, pendingTaskPayload: null, view: "tasks", poll: null, streamAbort: null, streamCursor: "",
  streamGeneration: 0, streamConnected: false, streamRetryMs: 1000, reconnectTimer: null, refreshTimer: null,
  pollInFlight: false, taskListFingerprint: "", taskListRenderedAt: 0,
  detailFingerprint: "", detailFingerprintTaskId: null,
  missionView: "board", missionSelectedId: null, missionPulses: [], missionFilter: storedMissionFilter(),
  missionReplay: { mode: "live", cursor: -1, playing: false, timer: null }, missionBookmarks: storedMissionBookmarks(),
  missionCamera: { zoom: 1, autoFit: true, layout: null, panning: null },
  token: sessionStorage.getItem("agentmesh-token") || ""
};
const $ = (id) => document.getElementById(id);
function credentialOriginSafe() { return location.protocol === "https:" || ["localhost", "127.0.0.1", "::1"].includes(location.hostname); }
function refreshCredentialSecurity() {
  const safe = credentialOriginSafe();
  $("manage-models")?.toggleAttribute("disabled", !safe);
  if ($("token")) { $("token").disabled = !safe; if (!safe) $("token").placeholder = "Use HTTPS or a local SSH tunnel to enter a token"; }
  if ($("connection-secret")) $("connection-secret").disabled = !safe;
  if ($("connection-env-name")) $("connection-env-name").disabled = !safe;
  if (!safe && state.token) { sessionStorage.removeItem("agentmesh-token"); state.token = ""; }
  const note = $("credential-security-note"); if (note) note.textContent = safe ? "Credential entry is available over HTTPS or a local SSH tunnel." : "Credential entry is blocked on this insecure origin. Use HTTPS or a local SSH tunnel.";
}
const terminal = new Set(["COMPLETED", "FAILED", "CANCELED"]);
const busy = new Set(["READY", "RUNNING", "REVIEWING", "REVISION_REQUIRED", "PAUSE_REQUESTED"]);

async function api(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.message || payload?.detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.code = payload?.code || null;
    throw error;
  }
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
  if (seconds < 60) return t("{count} 秒前", { count: seconds });
  if (seconds < 3600) return t("{count} 分钟前", { count: Math.floor(seconds / 60) });
  if (seconds < 86400) return t("{count} 小时前", { count: Math.floor(seconds / 3600) });
  return t("{count} 天前", { count: Math.floor(seconds / 86400) });
}
function shortId(value) { return value ? value.slice(0, 8) : "—"; }
function statusClass(value) { return String(value || "").toLowerCase(); }
function toast(message, error = false) { const node = $("toast"); node.textContent = message; node.className = `toast show${error ? " error" : ""}`; clearTimeout(toast.timer); toast.timer = setTimeout(() => node.className = "toast", 2800); }
function featureEnabled(name) { return state.features.get(name) === true; }
function providerLabel(policy = {}) {
  return ["openai", "deepseek"].includes(policy.provider) ? `${policy.provider} · ${policy.model}` : policy.provider === "deterministic" ? t("确定性运行") : t("继承部署默认值");
}
function csv(value) { return [...new Set(String(value || "").split(",").map((item) => item.trim()).filter(Boolean))]; }
function base64Utf8(value) {
  const bytes = new TextEncoder().encode(value); let binary = "";
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return btoa(binary);
}
function bytesLabel(value) { return value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KiB`; }
function updateConnection(online = true) {
  $("connection").classList.toggle("online", online);
  $("connection").lastChild.textContent = online ? (featureEnabled("realtime_events") ? (state.streamConnected ? t("实时连接") : t("轮询回退")) : t("已连接")) : t("连接异常");
}
function showAuthenticationNotice() {
  const secure = credentialOriginSafe();
  $("auth-notice").classList.remove("hidden");
  $("auth-guidance").textContent = t(secure
    ? "Authentication required — open Connection settings and enter an authorized Bearer token."
    : "Authentication required. This HTTP origin cannot send credentials; use HTTPS or a local SSH tunnel.");
  $("connection").classList.remove("online"); $("connection").lastChild.textContent = t("Authentication required");
  state.features = new Map(); state.featureItems = [];
  for (const id of ["agents-nav", "tools-nav", "artifacts-nav", "approvals-nav", "company-nav", "memory-nav"]) $(id).disabled = true;
  $("feature-readiness-list").innerHTML = `<p class="muted">${escapeHtml($("auth-guidance").textContent)}</p>`;
}
function clearAuthenticationNotice() { $("auth-notice").classList.add("hidden"); }

async function loadFeatures() {
  const result = await api("/api/v1/features");
  clearAuthenticationNotice();
  state.featureItems = result.features;
  state.features = new Map(result.features.map((item) => [item.name, item.enabled]));
  for (const [id, feature, label] of [
    ["agents-nav", "agent_registry_management", "Agents"], ["tools-nav", "mcp_read_tools", "Tools"],
    ["artifacts-nav", "artifact_service", "Deliverables"], ["approvals-nav", "policy_approval", "Approvals"],
    ["company-nav", "company_packs", "Company"], ["memory-nav", "organizational_memory", "Memory"]
  ]) {
    const button = $(id); const enabled = featureEnabled(feature);
    button.textContent = `${label}${enabled ? "" : ` · ${t("Setup")}`}`;
    button.disabled = !enabled;
    button.title = enabled ? `Enabled · ${feature}` : `Setup needed · enable ${feature} in server configuration`;
  }
  $("open-company-setup").disabled = !featureEnabled("company_model");
  $("open-memory-setup").disabled = !featureEnabled("organizational_memory");
  $("save-memory-setup").disabled = !featureEnabled("organizational_memory");
  $("memory-extraction-opt-in").disabled = !featureEnabled("organizational_memory");
  renderFeatureReadiness();
  updateTaskModeOptions();
  if (!featureEnabled("agent_registry_management") && state.view === "agents") switchView("tasks");
  if (!featureEnabled("mcp_read_tools") && state.view === "tools") switchView("tasks");
  if (!featureEnabled("artifact_service") && state.view === "artifacts") switchView("tasks");
  if (!featureEnabled("policy_approval") && state.view === "approvals") switchView("tasks");
  if (!featureEnabled("company_packs") && state.view === "company") switchView("tasks");
  if (!featureEnabled("organizational_memory") && state.view === "memory") switchView("tasks");
}

const featureDescriptions = {
  company_model: "Create a company workspace and use built-in organizational memory.",
  agent_registry_management: "Create and publish employees with versioned capabilities.",
  mcp_read_tools: "Connect governed read-only tools to task execution.",
  artifact_service: "Store versioned task deliverables and evidence.",
  policy_approval: "Review policy decisions and grant explicit execution permits.",
  company_packs: "Install and operate a governed business workspace.",
  organizational_memory: "Review candidate memories and manage company policies.",
  reviewed_execution: "Run an independent reviewer and bounded revisions.",
  coordinated_execution: "Plan dependency-aware work across published employees.",
  budget_admission: "Set hard task run, token, cost, and deadline limits."
};
function renderFeatureReadiness() {
  if (!$("feature-readiness-list")) return;
  const core = new Set(["company_model", "organizational_memory", "agent_registry_management", "reviewed_execution", "coordinated_execution", "budget_admission", "company_packs"]);
  const render = (items) => items.map((item) => `<div class="setup-row"><span class="status-dot ${item.enabled ? "available" : "disabled"}"></span><div><strong>${escapeHtml(featureLabel(item.name))}</strong><small>${escapeHtml(t(featureDescriptions[item.name] || item.description || "Optional workspace capability."))}</small></div><span class="pill ${item.enabled ? "good" : "muted"}">${t(item.enabled ? "Enabled" : "Setup needed")}</span></div>`).join("");
  const features = state.featureItems.filter((item) => item.name !== "office_3d");
  $("feature-readiness-list").innerHTML = render(features.filter((item) => core.has(item.name)));
  $("advanced-feature-readiness-list").innerHTML = render(features.filter((item) => !core.has(item.name)));
  $("advanced-capabilities").classList.toggle("hidden", !features.some((item) => !core.has(item.name)));
}
function featureLabel(name) {
  return t(({ agent_registry_management: "Published employees", coordinated_execution: "Coordinated tasks", reviewed_execution: "Reviewed tasks", company_model: "Company workspace", organizational_memory: "Governed memory", company_packs: "Company packs", budget_admission: "Task budgets" })[name] || name.replaceAll("_", " "));
}
function updateTaskModeOptions() {
  const select = $("execution-mode"); if (!select) return;
  for (const [mode, feature, extra] of [["REVIEWED", "reviewed_execution", []], ["COORDINATED", "coordinated_execution", ["agent_registry_management"]]]) {
    const option = [...select.options].find((item) => item.value === mode); if (!option) continue;
    const available = featureEnabled(feature) && extra.every(featureEnabled);
    option.disabled = !available;
    option.textContent = `${t(mode === "REVIEWED" ? "Reviewed" : "Coordinated")} · ${available ? t(mode === "REVIEWED" ? "deterministic review policy" : "selected published employees") : t("setup needed")}`;
  }
  $("budget-max-runs").disabled = !featureEnabled("budget_admission"); $("task-deadline").disabled = !featureEnabled("budget_admission");
}

async function loadMemory({ quiet = false } = {}) {
  if (!featureEnabled("organizational_memory")) return;
  if (state.memoryCompanies.length > 1 && !state.selectedMemoryCompanyId) {
    state.memoryError = t("Choose a company workspace before configuring memory.");
    state.memoryRecords = []; state.memoryPolicies = []; state.memoryRetrievals = [];
    if (state.view === "memory") { renderSidebarList(); renderMemory(); }
    return;
  }
  try {
    const selectedCompanyId = state.selectedMemoryCompanyId || state.memoryCompany?.company?.id;
    const company = selectedCompanyId
      ? await api(`/api/v1/companies/${encodeURIComponent(selectedCompanyId)}`)
      : await api("/api/v1/companies/active");
    const companyId = company.company.id;
    const [records, policies, retrievals] = await Promise.all([
      api(`/api/v1/companies/${companyId}/memory/records`),
      api(`/api/v1/companies/${companyId}/memory/policies`),
      api(`/api/v1/companies/${companyId}/memory/_retrievals`),
    ]);
    state.memoryCompany = company;
    state.memoryRecords = records;
    state.memoryPolicies = policies;
    state.memoryRetrievals = retrievals;
    state.memoryError = "";
  } catch (error) {
    state.memoryCompany = null;
    state.memoryRecords = [];
    state.memoryPolicies = [];
    state.memoryRetrievals = [];
    state.memoryError = error.message;
    if (!quiet) toast(error.message, true);
  }
  if (state.view === "memory") {
    renderSidebarList();
    renderMemory();
  }
}

async function loadCompanyTemplate({ quiet = false } = {}) {
  if (!featureEnabled("company_packs")) return;
  try {
    state.companyTemplate = await api("/api/v1/company-templates/market-intelligence-studio/preview");
    state.companyOperations = await api("/api/v1/company-templates/market-intelligence-studio/operations/preview");
    state.companyWorkforce = await api("/api/v1/company-templates/market-intelligence-studio/workforce/preview");
    state.marketResearch = await api("/api/v1/company-templates/market-intelligence-studio/research/preflight");
    state.companyTemplateError = "";
    if (state.view === "company") {
      renderCompanyTemplateList();
      renderCompanyTemplate();
    }
  } catch (error) {
    state.companyTemplate = null; state.companyOperations = null; state.companyWorkforce = null; state.marketResearch = null; state.companyTemplateError = error.message;
    if (state.view === "company") renderCompanyTemplateList();
    if (!quiet) toast(error.message, true);
  }
}

async function loadTools({ quiet = false } = {}) {
  if (!featureEnabled("mcp_read_tools")) return;
  try {
    state.tools = await api("/api/v1/mcp/catalog/tools"); state.toolsError = "";
    if (state.view === "tools") renderSidebarList();
    renderVersionToolOptions();
  } catch (error) {
    state.tools = []; state.toolsError = error.message;
    if (state.view === "tools") renderSidebarList();
    if (!quiet) toast(error.message, true);
  }
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
    renderDirectAgentChoices();
    if ($("version-connection")) renderVersionConnections();
    if (state.view === "agents") renderSidebarList();
    if (state.selectedAgentId && state.view === "agents") selectAgent(state.selectedAgentId, { renderList: false });
  } catch (error) { if (!quiet) toast(error.message, true); }
}

function renderVersionConnections() {
  const select = $("version-connection"); if (!select) return;
  const provider = $("version-provider")?.value;
  const previous = select.value;
  const eligible = state.modelConnections.filter((item) => item.enabled && (!provider || item.provider === provider));
  select.innerHTML = `<option value="">${t("Use provider configuration")}</option>${eligible.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.provider)} / ${escapeHtml(item.model || t("Provider default"))}${item.has_credential ? ` · ${t("credential saved")}` : ` · ${t("credential missing")}`}</option>`).join("")}`;
  if (eligible.some((item) => item.id === previous)) select.value = previous;
}
function publishedDefaultAgents({ asyncOnly = false } = {}) {
  return state.agents.filter((agent) => agent.lifecycle === "ACTIVE" && agent.versions?.some((version) => version.id === agent.default_version_id && version.status === "PUBLISHED" && (!asyncOnly || version.execution_modes?.includes("async"))));
}
function renderDirectAgentChoices() {
  const select = $("direct-agent"); if (!select) return;
  const previous = select.value; const agents = publishedDefaultAgents({ asyncOnly: true });
  select.innerHTML = `<option value="">Use deployment default employee</option>${agents.map((agent) => `<option value="${escapeHtml(agent.name)}">${escapeHtml(agent.name)}</option>`).join("")}`;
  if (agents.some((agent) => agent.name === previous)) select.value = previous;
}

async function loadProductSetup() {
  $("task-use-memory").disabled = true;
  try {
    const result = await api("/api/v1/model-connections");
    state.modelConnections = result.connections || []; state.modelReadiness = result.readiness || null;
    for (const connection of state.modelConnections) {
      if (state.modelConnectionTests.has(connection.id) && state.modelConnectionTests.get(connection.id).revision !== connection.revision) {
        state.modelConnectionTests.delete(connection.id);
      }
    }
    sessionStorage.setItem("agentmesh-model-connection-tests", JSON.stringify(Object.fromEntries(state.modelConnectionTests)));
    const enabledConnections = state.modelConnections.filter((item) => item.enabled);
    const anyFailedTest = enabledConnections.some((item) => state.modelConnectionTests.get(item.id)?.ok === false);
    const anyPassedTest = enabledConnections.some((item) => state.modelConnectionTests.get(item.id)?.ok === true);
    $("model-readiness").textContent = anyFailedTest ? t("Test failed") : anyPassedTest ? t("Provider test passed · assignment still required") : enabledConnections.length ? t("Connection configured — test required") : t("No model connection configured");
    $("model-readiness").className = `pill ${anyPassedTest && !anyFailedTest ? "good" : "muted"}`;
    $("model-connection-list").innerHTML = state.modelConnections.length ? state.modelConnections.map((item) => {
      const test = state.modelConnectionTests.get(item.id);
      const testLabel = !item.enabled ? t("Disabled") : !test ? t("Test required") : test.ok ? t("Test passed (this session)") : t("Test failed (this session)");
      return `<div class="setup-row"><span class="status-dot ${item.enabled && item.has_credential ? "available" : "disabled"}"></span><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.provider)} · ${escapeHtml(item.model || t("provider default"))} · ${item.credential_source === "api_key" ? t("API key saved") : t("environment reference")}</small><small class="connection-test-status ${test?.ok === false ? "error" : ""}">${testLabel}${test ? ` · ${escapeHtml(test.message)}` : ""}</small></div><span class="pill ${item.enabled ? "good" : "muted"}">${t(item.enabled ? "Enabled" : "Disabled")}</span>${item.enabled ? `<button class="button subtle" data-connection-test="${escapeHtml(item.id)}" type="button">${t("Test")}</button><button class="button subtle" data-connection-disable="${escapeHtml(item.id)}" type="button">${t("Disable")}</button>` : ""}</div>`;
    }).join("") : `<p class="muted">${t("No saved connections yet.")}</p>`;
    renderVersionConnections();
  } catch (error) {
    $("model-readiness").textContent = error.message.includes("403") || error.message.includes("401") ? t("Authorization required") : t("Unavailable");
    $("model-readiness").className = "pill muted";
    $("model-connection-list").innerHTML = `<p class="muted">${escapeHtml(error.message)}. Check identity/RBAC and credential-management setup.</p>`;
  }
  if (!featureEnabled("organizational_memory")) {
    $("memory-readiness").textContent = t("Setup needed"); $("memory-setup-copy").textContent = t("Enable organizational_memory in server feature configuration, then restart."); $("task-memory-status").textContent = "Memory is disabled on this server."; return;
  }
  state.memoryCompany = null;
  try {
    const companyList = await api("/api/v1/companies");
    state.memoryCompanies = companyList.filter((company) => company.status === "ACTIVE");
    const companyChoice = $("company-choice");
    $("company-choice-wrap").classList.toggle("hidden", state.memoryCompanies.length < 2);
    companyChoice.innerHTML = state.memoryCompanies.length > 1
      ? `<option value="">${t("Choose a company workspace")}</option>${state.memoryCompanies.map((company) => `<option value="${escapeHtml(company.id)}">${escapeHtml(company.name)}</option>`).join("")}`
      : state.memoryCompanies.map((company) => `<option value="${escapeHtml(company.id)}">${escapeHtml(company.name)}</option>`).join("");
    let active;
    if (state.memoryCompanies.length > 1) {
      const selected = state.memoryCompanies.some((company) => company.id === state.selectedMemoryCompanyId)
        ? state.selectedMemoryCompanyId : "";
      companyChoice.value = selected;
      if (!selected) {
        state.selectedMemoryCompanyId = null; state.memoryCompany = null; state.memorySetup = null;
        $("company-setup-form").classList.add("hidden");
        $("company-setup-status").textContent = t("Choose a company workspace before configuring memory.");
        $("memory-readiness").textContent = t("Selection required"); $("memory-readiness").className = "pill muted";
        $("memory-setup-copy").textContent = t("Several active company workspaces are available. Choose one explicitly.");
        $("memory-extraction-opt-in").disabled = true; $("save-memory-setup").disabled = true;
        $("task-use-memory").disabled = true; $("task-memory-status").textContent = t("Choose an active company workspace in Setup first.");
        return;
      }
      active = await api(`/api/v1/companies/${encodeURIComponent(selected)}`);
    } else {
      const listed = state.memoryCompanies[0];
      state.selectedMemoryCompanyId = listed?.id || null;
      active = await api("/api/v1/companies/active");
      if (listed && active.company.id !== listed.id) throw new Error("The active company changed while Setup was loading. Refresh and choose again.");
      companyChoice.value = listed?.id || "";
    }
    state.memoryCompany = active;
    $("company-setup-form")?.classList.add("hidden");
    $("company-setup-status").textContent = `${t("Active company")}: ${active.company.name}`;
    $("open-company-setup").textContent = t("Open company workspace");
    const result = await api(`/api/v1/companies/${encodeURIComponent(active.company.id)}/memory/setup`);
    state.memorySetup = result;
    $("memory-extraction-opt-in").disabled = false; $("save-memory-setup").disabled = false;
    $("memory-readiness").textContent = t(result.configured ? "Policy configured" : "Setup needed");
    $("memory-readiness").className = `pill ${result.configured ? "good" : "muted"}`;
    const external = Object.entries(result.external_backends || {}).map(([name, status]) => `${name}: ${status}`).join(" · ");
    const externalNote = external ? t("External backends {status}.", { status: external }) : "";
    $("memory-setup-copy").textContent = result.configured
      ? `${result.backend} · ${t(result.policy?.extraction_enabled ? "candidate extraction enabled; review required" : "candidate extraction off")}${externalNote ? ` · ${externalNote}` : ""}`
      : `${t("No reviewed company memory policy is configured yet.")} ${t("Save the reviewed preset below; learning stays off by default.")} ${externalNote}`.trim();
    $("memory-extraction-opt-in").checked = result.policy?.extraction_enabled === true;
    const canAttach = result.enabled && result.configured && Boolean(result.policy?.id) && result.policy?.active !== false;
    $("task-use-memory").disabled = !canAttach;
    $("task-memory-status").textContent = canAttach ? t("Optional: include this active company policy's approved memory context in the task.") : t("Configure an active company and reviewed memory policy in Setup before opting in.");
  } catch (error) {
    $("memory-readiness").textContent = "Not configured"; $("memory-readiness").className = "pill muted";
    const noCompany = !state.memoryCompany && error.status === 404 && /no active company exists/i.test(error.message);
    const canCreateCompany = featureEnabled("company_model") && noCompany;
    $("company-setup-form")?.classList.toggle("hidden", !canCreateCompany);
    $("company-setup-status").textContent = canCreateCompany
      ? t("Create a company workspace to configure built-in memory. Packs are optional.")
      : error.message;
    $("open-company-setup").textContent = canCreateCompany ? t("Create company workspace") : t("Set up a company workspace");
    $("memory-setup-copy").textContent = noCompany ? t("Create or activate a company workspace first. Then choose the reviewed memory preset; candidate learning remains off until you opt in.") : error.message;
    $("memory-extraction-opt-in").disabled = true; $("save-memory-setup").disabled = true;
    $("task-use-memory").disabled = true; $("task-memory-status").textContent = t("Configure an active company and reviewed memory policy in Setup before opting in.");
  }
}

async function createCompanyWorkspace(event) {
  event.preventDefault();
  const button = $("company-create-button");
  button.disabled = true; $("company-setup-error").textContent = "";
  try {
    await api("/api/v1/companies", { method: "POST", body: JSON.stringify({ name: $("company-name").value.trim(), mission: $("company-mission").value.trim() }) });
    $("company-setup-form").reset();
    await loadProductSetup();
    await loadMemory({ quiet: true });
    toast(t("Company workspace created."));
  } catch (error) {
    $("company-setup-error").textContent = error.message.includes("403")
      ? t("Company creation requires an authorized workspace administrator.") : error.message;
  } finally { button.disabled = false; }
}

function openModelConnectionForm() {
  if (!credentialOriginSafe()) { toast(t("Use HTTPS or a local SSH tunnel before entering credentials."), true); return; }
  $("model-connection-form").reset(); $("connection-name").value = ""; $("connection-model").value = "";
  $("connection-secret").value = ""; $("connection-env-name").value = ""; $("model-connection-error").textContent = "";
  syncConnectionCredentialFields(); $("model-connection-dialog").showModal();
}
function syncConnectionCredentialFields() {
  const apiKey = $("connection-credential-type").value === "api_key";
  $("connection-secret-label").classList.toggle("hidden", !apiKey); $("connection-secret").required = apiKey;
  $("connection-env-label").classList.toggle("hidden", apiKey); $("connection-env-name").required = !apiKey;
}
async function saveModelConnection(event) {
  event.preventDefault(); if (!credentialOriginSafe()) { $("model-connection-error").textContent = t("Use HTTPS or a local SSH tunnel before entering credentials."); return; }
  const credential = $("connection-credential-type").value === "api_key"
    ? { type: "api_key", value: $("connection-secret").value }
    : { type: "environment", name: $("connection-env-name").value.trim() };
  const payload = { name: $("connection-name").value.trim(), provider: $("connection-provider").value, ...( $("connection-model").value.trim() ? { model: $("connection-model").value.trim() } : {}), credential };
  $("connection-save-button").disabled = true; $("model-connection-error").textContent = "";
  try { await api("/api/v1/model-connections", { method: "POST", body: JSON.stringify(payload) }); $("connection-secret").value = ""; $("model-connection-dialog").close(); await loadProductSetup(); toast("Model connection saved. Test it explicitly before assigning it to an employee."); }
  catch (error) { $("model-connection-error").textContent = error.message.includes("403") ? "Credential management requires an authorized administrator and Identity/RBAC." : error.message; }
  finally { $("connection-save-button").disabled = false; }
}
async function testModelConnection(id) {
  if (!credentialOriginSafe()) return toast("Use HTTPS or a local SSH tunnel before testing credentials.", true);
  try {
    const result = await api(`/api/v1/model-connections/${encodeURIComponent(id)}/test`, { method: "POST" });
      const testedConnection = state.modelConnections.find((item) => item.id === id);
      state.modelConnectionTests.set(id, { ok: result.ok === true, message: result.message || "", testedAt: new Date().toISOString(), revision: testedConnection?.revision });
    sessionStorage.setItem("agentmesh-model-connection-tests", JSON.stringify(Object.fromEntries(state.modelConnectionTests)));
    toast(result.message || (result.ok ? "Connection test passed." : "Connection test failed."), !result.ok); await loadProductSetup();
  }
  catch (error) { toast(error.message.includes("403") ? "Testing requires an authorized administrator and Identity/RBAC." : error.message, true); }
}
async function disableModelConnection(id) {
  if (!confirm("Disable this model connection? Published employee versions may stop being runnable.")) return;
  try { await api(`/api/v1/model-connections/${encodeURIComponent(id)}/disable`, { method: "POST" }); await loadProductSetup(); toast("Model connection disabled."); }
  catch (error) { toast(error.message, true); }
}
async function saveMemorySetup() {
  const companyId = state.memoryCompany?.company?.id; if (!companyId) { toast("Create or activate a company first.", true); return; }
  if (!featureEnabled("organizational_memory")) { toast("Organizational memory is disabled in server configuration.", true); return; }
  const configured = state.memorySetup?.configured; const oldEnabled = state.memorySetup?.policy?.extraction_enabled === true; const nextEnabled = $("memory-extraction-opt-in").checked;
  if (configured && oldEnabled === nextEnabled) { toast("Memory policy is already up to date."); return; }
  const body = { preset: "reviewed_company_memory", extraction_enabled: nextEnabled };
  if (configured) body.version = Number(state.memorySetup.policy.version) + 1;
  try { await api(`/api/v1/companies/${encodeURIComponent(companyId)}/memory/setup`, { method: "POST", body: JSON.stringify(body) }); await loadProductSetup(); await loadMemory({ quiet: true }); toast("Reviewed memory policy saved."); }
  catch (error) { toast(error.message, true); }
}

async function loadTasks({ quiet = false } = {}) {
  try {
    const result = await api("/api/v1/tasks?limit=50&offset=0");
    const fingerprint = JSON.stringify(result.items);
    const changed = fingerprint !== state.taskListFingerprint;
    state.taskListFingerprint = fingerprint;
    state.tasks = result.items;
    updateConnection(true);
    if (state.view === "tasks" && (changed || Date.now() - state.taskListRenderedAt > 30000)) renderSidebarList();
    if (state.selectedId) await loadTask(state.selectedId, { quiet: true });
  } catch (error) {
    updateConnection(false);
    if (/401|403|authentication|bearer/i.test(error.message)) showAuthenticationNotice();
    if (!quiet) toast(error.message, true);
  }
}

function renderSidebarList() {
  if (state.view === "setup") { $("task-list").innerHTML = `<div class="empty-dag">Review workspace setup in the main panel.</div>`; return; }
  if (state.view === "agents") { renderAgentList(); return; }
  if (state.view === "tools") { renderToolList(); return; }
  if (state.view === "artifacts") { renderArtifactList(); return; }
  if (state.view === "approvals") { renderApprovalList(); return; }
  if (state.view === "company") { renderCompanyTemplateList(); return; }
  if (state.view === "memory") { renderMemoryList(); return; }
  const query = $("search").value.trim().toLowerCase();
  state.taskListRenderedAt = Date.now();
  const tasks = state.tasks.filter((task) => task.objective.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.length ? tasks.map((task) => `
    <button class="task-item ${task.id === state.selectedId ? "active" : ""}" data-task-id="${task.id}">
      <strong>${escapeHtml(task.objective)}</strong>
      <div><span class="status-dot ${statusClass(task.status)}">${escapeHtml(task.status)}</span><span>${age(task.updated_at)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? t("没有匹配任务") : t("还没有任务")}</div>`;
  document.querySelectorAll("[data-task-id]").forEach((node) => node.addEventListener("click", () => selectTask(node.dataset.taskId)));
}

function renderMemoryList() {
  const query = $("search").value.trim().toLowerCase();
  const values = state.memoryRecords.filter((item) => {
    const memory = item.memory;
    return `${memory.content} ${memory.memory_type} ${memory.namespace_type} ${memory.namespace_id} ${memory.status}`.toLowerCase().includes(query);
  });
  $("task-list").innerHTML = state.memoryError
    ? `<div class="empty-dag audit-error">${escapeHtml(state.memoryError)}</div>`
    : values.length
      ? values.map((item) => {
        const memory = item.memory;
        return `<button class="task-item ${memory.id === state.selectedMemoryId ? "active" : ""}" data-memory-id="${memory.id}">
          <strong>${escapeHtml(memory.content)}</strong>
          <div><span class="status-dot ${statusClass(memory.status)}">${escapeHtml(memory.status)}</span><span>${escapeHtml(memory.memory_type)}</span></div>
        </button>`;
      }).join("")
      : `<div class="empty-dag">${query ? t("没有匹配记忆") : t("还没有长期记忆")}</div>`;
  document.querySelectorAll("[data-memory-id]").forEach((node) => node.addEventListener("click", () => {
    state.selectedMemoryId = node.dataset.memoryId;
    renderMemoryList();
    document.querySelector(`[data-memory-card-id="${CSS.escape(node.dataset.memoryId)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}

function memoryCard(snapshot, { review = false } = {}) {
  const memory = snapshot.memory;
  const evidence = snapshot.evidence.map((item) => `<span>${escapeHtml(item.evidence_type)} · ${escapeHtml(shortId(item.evidence_id))}</span>`).join("");
  const actions = review
    ? `<div class="memory-card-actions"><button class="button danger" data-memory-decision="REJECT" data-memory-target="${memory.id}" type="button">${t("拒绝")}</button><button class="button primary" data-memory-decision="ACCEPT" data-memory-target="${memory.id}" type="button">${t("接受并生效")}</button></div>`
    : memory.status === "ACCEPTED"
      ? `<div class="memory-card-actions"><button class="button danger" data-memory-decision="REVOKE" data-memory-target="${memory.id}" type="button">${t("撤销未来使用")}</button></div>`
      : "";
  return `<article class="memory-card ${statusClass(memory.status)}" data-memory-card-id="${memory.id}">
    <div class="memory-card-head"><div><span class="pill">${escapeHtml(memory.memory_type)}</span><span class="pill">${escapeHtml(memory.namespace_type)}</span></div><span class="status-dot ${statusClass(memory.status)}">${escapeHtml(memory.status)}</span></div>
    <p>${escapeHtml(memory.content)}</p>
    <div class="memory-evidence">${evidence || `<span>${t("没有证据摘要")}</span>`}</div>
    <footer><code>${escapeHtml(shortId(memory.id))} · ${escapeHtml(memory.provenance_type)}</code><span>${memory.confidence_basis_points / 100}% · ${age(memory.created_at)}</span></footer>
    ${actions}
  </article>`;
}

function renderMemory() {
  const candidates = state.memoryRecords.filter((item) => item.memory.status === "CANDIDATE");
  const accepted = state.memoryRecords.filter((item) => item.memory.status === "ACCEPTED");
  const activePolicies = state.memoryPolicies.filter((item) => item.active);
  $("manual-memory-policy").innerHTML = activePolicies.length
    ? activePolicies.map((policy) => `<option value="${escapeHtml(policy.id)}">${escapeHtml(policy.key)} · v${policy.version}</option>`).join("")
    : `<option value="">${t("Configure a reviewed memory policy first.")}</option>`;
  $("manual-memory-submit").disabled = !activePolicies.length || !state.memoryCompany?.company?.id;
  $("memory-company-name").textContent = state.memoryCompany?.company?.name || t("尚未创建公司");
  $("memory-candidate-count").textContent = candidates.length;
  $("memory-accepted-count").textContent = accepted.length;
  $("memory-retrieval-count").textContent = state.memoryRetrievals.length;
  $("memory-policy-count").textContent = activePolicies.length;
  $("memory-review-count").textContent = t("{count} 条等待决定", { count: candidates.length });
  $("memory-error").classList.toggle("hidden", !state.memoryError);
  $("memory-error").textContent = state.memoryError;
  $("memory-policy-strip").innerHTML = activePolicies.length
    ? activePolicies.map((policy) => `<article class="memory-policy-card"><header><strong>${escapeHtml(policy.key)}</strong><span class="pill">v${policy.version}</span></header><small>${policy.extraction_enabled ? t("自动学习已开启") : t("仅手动沉淀")} · ${policy.maximum_retrieval_count} records / ${policy.maximum_context_tokens} tokens</small><small>${escapeHtml(policy.allowed_memory_types.join(" · "))}</small></article>`).join("")
    : `<div class="memory-empty">${t("尚未配置活动记忆策略")}</div>`;
  $("memory-candidate-list").innerHTML = candidates.length
    ? candidates.map((item) => memoryCard(item, { review: true })).join("")
    : `<div class="memory-empty">${t("没有等待审核的学习。员工完成任务后，新经验会先来到这里。")}</div>`;
  const filter = $("memory-status-filter").value;
  const records = state.memoryRecords.filter((item) => {
    if (item.memory.status === "CANDIDATE") return false;
    if (filter === "ALL") return true;
    if (filter === "ACTIVE") return ["ACCEPTED"].includes(item.memory.status);
    return item.memory.status === filter;
  });
  $("memory-record-list").innerHTML = records.length
    ? records.map((item) => memoryCard(item)).join("")
    : `<div class="memory-empty">${t("当前筛选条件下没有记忆")}</div>`;
  $("memory-retrieval-list").innerHTML = state.memoryRetrievals.length
    ? [...state.memoryRetrievals].reverse().slice(0, 24).map((item) => `<article class="memory-retrieval">
        <header><strong>${escapeHtml(item.reason)}</strong><small>${age(item.created_at)}</small></header>
        <div class="memory-route"><span>Task ${escapeHtml(shortId(item.task_id))}</span><i></i><span>Run ${escapeHtml(shortId(item.run_id))}</span><i></i><span>${item.result_memory_ids.length} Memory</span></div>
        <div class="memory-result-dots">${item.result_memory_ids.map(() => "<i></i>").join("")}</div>
        <small>${escapeHtml(item.principal_id)} · policy v${item.policy_version}</small>
      </article>`).join("")
    : `<div class="memory-empty">${t("还没有任务召回记录")}</div>`;
  document.querySelectorAll("[data-memory-decision]").forEach((node) => node.addEventListener("click", () => openMemoryReview(node.dataset.memoryTarget, node.dataset.memoryDecision)));
}

async function saveManualMemoryNote(event) {
  event.preventDefault();
  const companyId = state.memoryCompany?.company?.id;
  const policyId = $("manual-memory-policy").value;
  if (!companyId || !policyId) { $("manual-memory-error").textContent = t("Configure an active company and reviewed memory policy first."); return; }
  const button = $("manual-memory-submit"); button.disabled = true; $("manual-memory-error").textContent = "";
  try {
    const snapshot = await api(`/api/v1/companies/${encodeURIComponent(companyId)}/memory/notes`, {
      method: "POST",
      body: JSON.stringify({ policy_id: policyId, content: $("manual-memory-content").value.trim(), memory_type: $("manual-memory-type").value })
    });
    $("manual-memory-content").value = "";
    state.selectedMemoryId = snapshot.memory.id;
    await loadMemory({ quiet: true });
    toast(t("Company note saved; check its policy status in the inbox or ledger."));
  } catch (error) { $("manual-memory-error").textContent = error.message; }
  finally { button.disabled = !state.memoryPolicies.some((policy) => policy.active); }
}

function openMemoryReview(memoryId, decision) {
  const snapshot = state.memoryRecords.find((item) => item.memory.id === memoryId);
  if (!snapshot) return;
  const policies = state.memoryPolicies.filter((item) => item.active);
  $("memory-review-id").value = memoryId;
  $("memory-review-decision").value = decision;
  $("memory-review-title").textContent = decision === "ACCEPT" ? t("接受这条学习") : decision === "REJECT" ? t("拒绝这条学习") : t("撤销这条记忆");
  $("memory-review-content").textContent = snapshot.memory.content;
  $("memory-review-policy").innerHTML = policies.map((policy) => `<option value="${policy.id}">${escapeHtml(policy.key)} · v${policy.version}</option>`).join("");
  $("memory-review-policy").disabled = decision === "REVOKE";
  $("memory-review-reason").value = "";
  $("memory-review-error").textContent = "";
  $("memory-review-submit").textContent = decision === "ACCEPT" ? t("接受并生效") : decision === "REJECT" ? t("确认拒绝") : t("确认撤销");
  $("memory-review-dialog").showModal();
}

async function submitMemoryReview(event) {
  event.preventDefault();
  const memoryId = $("memory-review-id").value;
  const decision = $("memory-review-decision").value;
  const companyId = state.memoryCompany?.company?.id;
  if (!companyId) return;
  $("memory-review-submit").disabled = true;
  $("memory-review-error").textContent = "";
  try {
    if (decision === "REVOKE") {
      await api(`/api/v1/companies/${companyId}/memory/${memoryId}/revoke`, {
        method: "POST",
        body: JSON.stringify({ reason: $("memory-review-reason").value.trim() }),
      });
    } else {
      await api(`/api/v1/companies/${companyId}/memory/${memoryId}/review`, {
        method: "POST",
        body: JSON.stringify({
          policy_id: $("memory-review-policy").value,
          decision,
          reason: $("memory-review-reason").value.trim(),
        }),
      });
    }
    $("memory-review-dialog").close();
    toast(decision === "ACCEPT" ? t("记忆已接受并可用于未来任务") : decision === "REJECT" ? t("学习候选已拒绝") : t("记忆已撤销"));
    await loadMemory({ quiet: true });
  } catch (error) {
    $("memory-review-error").textContent = error.message;
  } finally {
    $("memory-review-submit").disabled = false;
  }
}

function renderCompanyTemplateList() {
  $("task-list").innerHTML = state.companyTemplateError
    ? `<div class="empty-dag audit-error">${escapeHtml(state.companyTemplateError)}</div>`
    : `<button class="task-item active" type="button"><strong>${t("市场情报工作室")}</strong><div><span class="status-dot available">${t("内置模板")}</span><span>v${escapeHtml(state.companyTemplate?.version || "1.0.0")}</span></div></button>`;
}

function renderCompanyTemplate() {
  const value = state.companyTemplate; if (!value) return;
  $("company-template-name").textContent = value.name;
  $("company-template-mission").textContent = value.mission;
  $("company-template-version").textContent = `v${value.version}`;
  $("company-template-digest").textContent = `sha256:${value.content_digest.slice(0, 12)}`;
  $("company-template-digest").title = value.content_digest;
  $("template-unit-count").textContent = value.resource_summary.organization_unit || 0;
  $("template-position-count").textContent = value.resource_summary.position || 0;
  $("template-object-count").textContent = value.resource_summary.business_object_type || 0;
  $("template-credential-count").textContent = value.required_credentials.length;
  $("template-resource-total").textContent = t("{count} 个受治理资源", { count: value.resources.length });
  const groups = [
    ["organization_unit", t("部门")],
    ["position", t("岗位")],
    ["business_object_type", t("业务对象")],
  ];
  $("template-resource-groups").innerHTML = groups.map(([kind, label]) => {
    const items = value.resources.filter((item) => item.kind === kind);
    return `<section><h4>${escapeHtml(label)} <span>${items.length}</span></h4><div>${items.map((item) => `<span title="${escapeHtml(item.key)}">${escapeHtml(item.name)}</span>`).join("")}</div></section>`;
  }).join("");
  const button = $("install-company-template");
  button.disabled = !value.installable;
  $("company-template-status").textContent = value.active_company_id
    ? t("当前租户已有活跃公司；请先归档它再安装新模板。")
    : value.missing_features.length
      ? t("缺少功能开关：{features}", { features: value.missing_features.join(", ") })
      : t("将以一个数据库事务创建全部资源；失败时不会留下半成品。");
  renderCompanyOperations();
  renderCompanyWorkforce();
  renderMarketResearch();
}

function renderMarketResearch() {
  const value = state.marketResearch;
  const panel = $("market-research-panel");
  if (!value || !value.company_id) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const badge = $("market-research-badge");
  badge.textContent = value.ready ? t("可以启动") : t("预检未通过");
  badge.className = `status-dot ${value.ready ? "completed" : "queued"}`;
  const tools = value.tools.map((tool) => `<article class="${tool.ready ? "ready" : "blocked"}">
    <span>${tool.ready ? "✓" : "!"}</span><div><strong>${escapeHtml(tool.logical_key)}</strong><small>${escapeHtml(tool.server_name || t("尚未绑定 MCP 工具"))}</small></div>
  </article>`).join("");
  const positions = value.positions.map((position) => `<article class="${position.ready ? "ready" : "blocked"}">
    <span>${position.ready ? "✓" : "!"}</span><div><strong>${escapeHtml(position.title)}</strong><small>${escapeHtml(position.agent_name || t("尚未任命 Agent"))}</small></div>
  </article>`).join("");
  const blockers = value.blockers.length
    ? `<div class="research-blockers">${value.blockers.map((item) => `<p><code>${escapeHtml(item.subject)}</code>${escapeHtml(item.message)}</p>`).join("")}</div>`
    : `<p class="research-ready">${t("预检通过：任务会创建五段可观测协作链，并立即进入执行队列。")}</p>`;
  const warnings = value.warnings.length
    ? `<div class="research-warnings">${value.warnings.map((item) => `<p><code>${escapeHtml(item.subject)}</code>${escapeHtml(item.message)}</p>`).join("")}</div>`
    : "";
  $("market-research-preflight").innerHTML = `<div><h4>MCP TOOLS</h4>${tools}</div><div><h4>APPOINTED TEAM</h4>${positions}</div>${blockers}${warnings}`;
  $("launch-market-research").disabled = !value.ready;
}

async function launchMarketResearch(event) {
  event.preventDefault();
  const button = $("launch-market-research"); button.disabled = true;
  $("market-research-error").textContent = "";
  const payload = {
    question: $("research-question").value.trim(),
    target_audience: $("research-audience").value.trim(),
    decision_supported: $("research-decision").value.trim(),
    scope: $("research-scope").value.trim(),
    max_sources: Number($("research-max-sources").value),
  };
  try {
    const result = await api("/api/v1/company-templates/market-intelligence-studio/research/launch", {
      method: "POST",
      headers: { "Idempotency-Key": clientRequestId() },
      body: JSON.stringify(payload),
    });
    toast(t("真实研究任务已启动"));
    await loadTasks({ quiet: true });
    switchView("tasks");
    await selectTask(result.task.id);
  } catch (error) {
    $("market-research-error").textContent = error.message;
    button.disabled = !state.marketResearch?.ready;
  }
}

function renderCompanyOperations() {
  const value = state.companyOperations;
  const panel = $("company-operations-panel");
  if (!value) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  if (!$("operations-starts-at").value) {
    const start = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000);
    $("operations-starts-at").value = start.toISOString().slice(0, 16);
  }
  const labels = {
    operating_cycle: t("经营周期"), objective: t("目标"), key_result: "KR",
    initiative: "Initiative", budget_allocation: t("预算"),
    memory_policy: t("记忆策略"), company_operation: t("运营流程"),
  };
  $("company-operations-summary").innerHTML = Object.entries(value.resource_summary)
    .map(([kind, count]) => `<span><strong>${count}</strong>${escapeHtml(labels[kind] || kind)}</span>`)
    .join("");
  const badge = $("company-operations-badge");
  badge.textContent = value.already_installed ? t("已启用") : t("可选");
  badge.className = `status-dot ${value.already_installed ? "completed" : "available"}`;
  const button = $("activate-company-operations");
  button.disabled = !value.installable;
  $("company-operations-status").textContent = value.already_installed
    ? t("运营系统已启用；周期流程仍处于草稿，等待你检查并激活。")
    : !value.active_company_id
      ? t("先创建基础公司，再启用运营系统。")
      : !value.base_pack_installed
        ? t("当前公司不是由市场情报模板创建，无法启用此扩展包。")
        : value.missing_features.length
          ? t("缺少功能开关：{features}", { features: value.missing_features.join(", ") })
          : t("启用动作以单个数据库事务完成，不会启动 Agent 或执行外部写入。");
}

function renderCompanyWorkforce() {
  const value = state.companyWorkforce;
  const panel = $("company-workforce-panel");
  if (!value || !value.active_company_id) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const badge = $("company-workforce-badge");
  badge.textContent = value.fully_staffed ? t("团队就绪") : t("等待配置");
  badge.className = `status-dot ${value.fully_staffed ? "completed" : "queued"}`;
  $("company-workforce-positions").innerHTML = value.positions.length
    ? value.positions.map((position) => {
      const appointed = position.appointment_id
        ? `<div class="workforce-appointment">
            <span class="workforce-appointed">${escapeHtml(position.appointed_agent_name || t("已任命 Agent 不可用"))} · ${position.ready ? t("已任命") : t("需要重新任命")}</span>
            <button class="button subtle compact" type="button" data-end-workforce-appointment="${position.appointment_id}">${t("更换")}</button>
          </div>`
        : `<select data-workforce-position="${escapeHtml(position.key)}">
            <option value="">${position.candidates.length ? t("选择 Agent") : t("没有能力匹配的 Agent")}</option>
            ${position.candidates.map((candidate) => `<option value="${candidate.agent_version_id}">${escapeHtml(candidate.agent_name)} · v${escapeHtml(candidate.semantic_version)}</option>`).join("")}
          </select>`;
      return `<article class="${position.ready ? "ready" : ""}">
        <div><strong>${escapeHtml(position.title)}</strong><code>${escapeHtml(position.key)}</code></div>
        <small>${escapeHtml(position.required_capabilities.join(" · "))}</small>
        ${appointed}
      </article>`;
    }).join("")
    : `<div class="empty-dag">${value.operations_pack_installed ? t("没有需要任命的岗位") : t("先启用运营系统，再配置团队。")}</div>`;
  $("company-workforce-operations").innerHTML = value.operations.length
    ? value.operations.map((operation) => `<label class="${operation.ready ? "ready" : "blocked"}">
        <input type="checkbox" data-workforce-operation="${escapeHtml(operation.key)}" ${operation.ready && ["DRAFT", "PAUSED"].includes(operation.status) ? "checked" : "disabled"}>
        <span><strong>${escapeHtml(operation.name)}</strong><small>${escapeHtml(operation.position_keys.join(" → "))}</small></span>
        <b>${operation.status === "ACTIVE" ? t("运行中") : operation.ready ? t("可启动") : t("缺少岗位：{positions}", { positions: operation.blockers.join(", ") })}</b>
      </label>`).join("")
    : "";
  $("appoint-company-workforce").disabled = !value.positions.some((position) => !position.appointment_id && position.candidates.length);
  $("start-company-operations").disabled = value.activatable_operation_count === 0;
  $("company-workforce-status").textContent = value.missing_features.length
    ? t("缺少功能开关：{features}", { features: value.missing_features.join(", ") })
    : value.fully_staffed
      ? t("团队预检通过。选择流程并显式启动后，周期触发会创建绑定这些员工的协作任务。")
      : t("每个岗位只显示已发布默认版本且能力完全匹配的 Agent。");
  document.querySelectorAll("[data-end-workforce-appointment]").forEach((node) => {
    node.addEventListener("click", () => endCompanyWorkforceAppointment(node.dataset.endWorkforceAppointment));
  });
}

async function installCompanyTemplate(event) {
  event.preventDefault();
  const button = $("install-company-template"); button.disabled = true;
  $("company-template-error").textContent = "";
  const payload = {
    company_name: $("template-company-name").value.trim(),
    target_market: $("template-target-market").value.trim(),
    product_type: $("template-product-type").value,
    excluded_sectors: $("template-excluded-sectors").value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    operating_timezone: $("template-timezone").value.trim(),
  };
  try {
    const result = await api("/api/v1/company-templates/market-intelligence-studio/install", { method: "POST", body: JSON.stringify(payload) });
    toast(t("公司 {name} 已创建", { name: result.company.name }));
    await loadCompanyTemplate({ quiet: true });
  } catch (error) {
    $("company-template-error").textContent = error.message;
  } finally {
    button.disabled = !state.companyTemplate?.installable;
  }
}

async function activateCompanyOperations(event) {
  event.preventDefault();
  const button = $("activate-company-operations"); button.disabled = true;
  $("company-operations-error").textContent = "";
  const payload = {
    starts_at: new Date($("operations-starts-at").value).toISOString(),
    cycle_days: Number($("operations-cycle-days").value),
    budget_limit_micros: Math.round(Number($("operations-budget").value) * 1_000_000),
    currency: $("operations-currency").value.trim().toUpperCase(),
  };
  try {
    await api("/api/v1/company-templates/market-intelligence-studio/operations/activate", {
      method: "POST", body: JSON.stringify(payload),
    });
    toast(t("公司运营系统已启用"));
    await loadCompanyTemplate({ quiet: true });
  } catch (error) {
    $("company-operations-error").textContent = error.message;
  } finally {
    button.disabled = !state.companyOperations?.installable;
  }
}

async function appointCompanyWorkforce() {
  const assignments = [...document.querySelectorAll("[data-workforce-position]")]
    .filter((node) => node.value)
    .map((node) => ({ position_key: node.dataset.workforcePosition, agent_version_id: node.value }));
  if (!assignments.length) { $("company-workforce-error").textContent = t("请至少选择一个岗位 Agent。"); return; }
  const button = $("appoint-company-workforce"); button.disabled = true;
  $("company-workforce-error").textContent = "";
  try {
    await api("/api/v1/company-templates/market-intelligence-studio/workforce/appoint", {
      method: "POST",
      body: JSON.stringify({ assignments, reason: "Configured through the AgentMesh Admin workforce wizard." }),
    });
    toast(t("岗位任命已保存"));
    await loadCompanyTemplate({ quiet: true });
  } catch (error) {
    $("company-workforce-error").textContent = error.message;
  }
}

async function endCompanyWorkforceAppointment(appointmentId) {
  if (!window.confirm(t("结束当前任命并重新选择 Agent？"))) return;
  $("company-workforce-error").textContent = "";
  try {
    await api(`/api/v1/companies/${state.companyWorkforce.active_company_id}/appointments/${appointmentId}/end`, {
      method: "POST",
    });
    toast(t("岗位已释放，可以重新任命"));
    await loadCompanyTemplate({ quiet: true });
  } catch (error) {
    $("company-workforce-error").textContent = error.message;
  }
}

async function startCompanyOperations() {
  const operationKeys = [...document.querySelectorAll("[data-workforce-operation]:checked")]
    .map((node) => node.dataset.workforceOperation);
  if (!operationKeys.length) { $("company-workforce-error").textContent = t("请选择至少一个就绪流程。"); return; }
  const button = $("start-company-operations"); button.disabled = true;
  $("company-workforce-error").textContent = "";
  try {
    await api(`/api/v1/companies/${state.companyWorkforce.active_company_id}/operations/_activate/staffed`, {
      method: "POST",
      body: JSON.stringify({ operation_keys: operationKeys, activated_at: new Date().toISOString() }),
    });
    toast(t("运营流程已启动"));
    await loadCompanyTemplate({ quiet: true });
  } catch (error) {
    $("company-workforce-error").textContent = error.message;
  }
}

function renderToolList() {
  const query = $("search").value.trim().toLowerCase();
  const tools = state.tools.filter((tool) => `${tool.logical_key} ${tool.tool_name} ${tool.description} ${tool.server_name}`.toLowerCase().includes(query));
  $("task-list").innerHTML = state.toolsError ? `<div class="empty-dag audit-error">${t("无法读取 Tool Catalog：")}${escapeHtml(state.toolsError)}</div>` : tools.length ? tools.map((tool) => `
    <button class="task-item ${tool.logical_key === state.selectedToolKey ? "active" : ""}" data-tool-key="${escapeHtml(tool.logical_key)}">
      <strong>${escapeHtml(tool.logical_key)}</strong>
      <div><span class="status-dot available">${escapeHtml(tool.side_effect)}</span><span>${escapeHtml(tool.server_name)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? t("没有匹配 Tool") : t("还没有已发布 Tool")}</div>`;
  document.querySelectorAll("[data-tool-key]").forEach((node) => node.addEventListener("click", () => selectTool(node.dataset.toolKey)));
}

function renderArtifactList() {
  const query = $("search").value.trim().toLowerCase();
  const artifacts = state.artifacts.filter((item) => `${item.display_name} ${item.kind} ${item.classification} ${item.owner_id}`.toLowerCase().includes(query));
  $("task-list").innerHTML = state.artifactsError ? `<div class="empty-dag audit-error">${t("无法读取 Artifact：")}${escapeHtml(state.artifactsError)}</div>` : artifacts.length ? artifacts.map((item) => `
    <button class="task-item artifact-item ${item.id === state.selectedArtifactId ? "active" : ""}" data-artifact-id="${item.id}">
      <strong>${escapeHtml(item.display_name)}</strong>
      <div><span class="status-dot available">${escapeHtml(item.kind)}</span><span>${t("{count} 版本", { count: item.version_count })}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? t("没有匹配 Artifact") : t("还没有 Artifact")}</div>`;
  document.querySelectorAll("[data-artifact-id]").forEach((node) => node.addEventListener("click", () => selectArtifact(node.dataset.artifactId)));
}

function renderApprovalList() {
  const query = $("search").value.trim().toLowerCase();
  const approvals = state.approvals.filter((item) => `${item.action_type} ${item.requester_id} ${item.resource_type} ${item.resource_id} ${item.approval_status}`.toLowerCase().includes(query));
  $("task-list").innerHTML = state.approvalsError ? `<div class="empty-dag audit-error">${t("无法读取审批：")}${escapeHtml(state.approvalsError)}</div>` : approvals.length ? approvals.map((item) => `
    <button class="task-item approval-item ${item.id === state.selectedApprovalId ? "active" : ""}" data-approval-action-id="${item.id}">
      <strong>${escapeHtml(item.action_type)}</strong>
      <div><span class="status-dot ${statusClass(item.approval_status)}">${escapeHtml(item.approval_status)}</span><span>${age(item.created_at)}</span></div>
    </button>`).join("") : `<div class="empty-dag">${query ? t("没有匹配审批") : t("还没有审批请求")}</div>`;
  document.querySelectorAll("[data-approval-action-id]").forEach((node) => node.addEventListener("click", () => selectApproval(node.dataset.approvalActionId)));
}

function renderAgentList() {
  const query = $("search").value.trim().toLowerCase();
  const agents = state.agents.filter((agent) => `${agent.name} ${agent.description} ${agent.tags.join(" ")}`.toLowerCase().includes(query));
  $("task-list").innerHTML = agents.length ? agents.map((agent) => {
    const published = agent.versions.filter((version) => version.status === "PUBLISHED").length;
    return `<button class="task-item agent-item ${agent.id === state.selectedAgentId ? "active" : ""}" data-agent-id="${agent.id}">
      <strong>${escapeHtml(agent.name)}</strong>
      <div><span class="status-dot ${statusClass(agent.lifecycle)}">${escapeHtml(agent.lifecycle)}</span><span>${t("{count} 已发布", { count: published })}</span></div>
    </button>`;
  }).join("") : `<div class="empty-dag">${query ? t("没有匹配 Agent") : t("还没有 Agent")}</div>`;
  document.querySelectorAll("[data-agent-id]").forEach((node) => node.addEventListener("click", () => selectAgent(node.dataset.agentId)));
}

function switchView(view) {
  state.view = view;
  $("advanced-nav").open = false;
  const agents = view === "agents";
  const tools = view === "tools";
  const artifacts = view === "artifacts";
  const approvals = view === "approvals";
  const company = view === "company";
  const memory = view === "memory";
  const setup = view === "setup";
  $("tasks-nav").classList.toggle("active", view === "tasks"); $("setup-nav").classList.toggle("active", setup); $("agents-nav").classList.toggle("active", agents); $("tools-nav").classList.toggle("active", tools); $("artifacts-nav").classList.toggle("active", artifacts); $("approvals-nav").classList.toggle("active", approvals); $("company-nav").classList.toggle("active", company); $("memory-nav").classList.toggle("active", memory);
  $("sidebar-eyebrow").textContent = memory ? "MEMORY CONTROL" : company ? "COMPANY OS" : agents ? "REGISTRY" : tools ? "CATALOG" : artifacts ? "EVIDENCE" : approvals ? "GOVERNANCE" : "WORKSPACE";
  $("sidebar-title").textContent = setup ? "Workspace setup" : memory ? t("长期记忆") : company ? t("公司模板") : agents ? t("Agent 目录") : tools ? t("Tool 目录") : artifacts ? t("Artifact 目录") : approvals ? t("审批队列") : t("任务中心");
  $("search").value = ""; $("search").placeholder = memory ? t("搜索记忆内容") : company ? t("搜索公司模板") : agents ? t("搜索 Agent") : tools ? t("搜索 Tool") : artifacts ? t("搜索 Artifact") : approvals ? t("搜索审批") : t("搜索任务");
  $("search").setAttribute("aria-label", memory ? t("搜索记忆内容") : company ? t("搜索公司模板") : agents ? t("搜索 Agent") : tools ? t("搜索 Tool") : artifacts ? t("搜索 Artifact") : approvals ? t("搜索审批") : t("搜索任务"));
  $("new-task-button").classList.toggle("hidden", approvals || tools || company || memory || setup); $("new-task-button").setAttribute("aria-label", agents ? t("创建 Agent") : artifacts ? t("创建 Artifact") : t("创建任务"));
  $("empty-state").classList.toggle("hidden", view !== "tasks" || Boolean(state.selectedId));
  $("setup-detail").classList.toggle("hidden", !setup);
  $("task-detail").classList.toggle("hidden", view !== "tasks" || !state.selectedId);
  $("agent-empty-state").classList.toggle("hidden", !agents || Boolean(state.selectedAgentId));
  $("agent-detail").classList.toggle("hidden", !agents || !state.selectedAgentId);
  $("tool-empty-state").classList.toggle("hidden", !tools || Boolean(state.selectedToolKey));
  $("tool-detail").classList.toggle("hidden", !tools || !state.selectedToolKey);
  $("artifact-empty-state").classList.toggle("hidden", !artifacts || Boolean(state.selectedArtifactId));
  $("artifact-detail").classList.toggle("hidden", !artifacts || !state.selectedArtifactId);
  $("approval-empty-state").classList.toggle("hidden", !approvals || Boolean(state.selectedApprovalId));
  $("approval-detail").classList.toggle("hidden", !approvals || !state.selectedApprovalId);
  $("company-detail").classList.toggle("hidden", !company);
  $("memory-detail").classList.toggle("hidden", !memory);
  renderSidebarList();
  if (agents) loadAgents({ quiet: true });
  if (tools) loadTools({ quiet: true });
  if (artifacts) loadArtifacts({ quiet: false });
  if (approvals) loadApprovals({ quiet: false });
  if (company) loadCompanyTemplate({ quiet: false });
  if (memory) loadMemory({ quiet: false });
}

function selectTool(logicalKey) {
  const tool = state.tools.find((item) => item.logical_key === logicalKey); if (!tool) return;
  state.selectedToolKey = logicalKey; state.selectedTool = tool; renderToolList();
  $("tool-empty-state").classList.add("hidden"); $("tool-detail").classList.remove("hidden");
  $("tool-key").textContent = tool.logical_key; $("tool-name").textContent = tool.tool_name;
  $("tool-description").textContent = tool.description || t("未填写描述");
  $("tool-side-effect").textContent = tool.side_effect; $("tool-server").textContent = tool.server_name;
  $("tool-version").textContent = `v${tool.version}`; $("tool-schema-digest").textContent = shortId(tool.schema_digest.replace("sha256:", ""));
  $("tool-schema-digest").title = tool.schema_digest; $("tool-status").textContent = tool.version_status;
  $("tool-input-schema").textContent = JSON.stringify(tool.input_schema, null, 2);
}

function openMcpCatalog() {
  $("mcp-catalog-form").reset(); $("mcp-catalog-results").innerHTML = "";
  $("mcp-import-panel").classList.add("hidden"); $("mcp-import-error").textContent = "";
  $("mcp-catalog-dialog").showModal(); setTimeout(() => $("mcp-catalog-query").focus(), 50);
}

async function searchMcpCatalog(event) {
  event.preventDefault(); const button = $("mcp-catalog-search"); button.disabled = true;
  try {
    const values = await api(`/api/v1/mcp/catalog/search?q=${encodeURIComponent($("mcp-catalog-query").value.trim())}&limit=20`);
    state.catalogCandidates = values;
    $("mcp-catalog-results").innerHTML = values.length ? values.map((item, index) => `
      <article class="catalog-card ${item.installable ? "" : "disabled"}">
        <div><strong>${escapeHtml(item.registry_name)}</strong><span class="pill">${escapeHtml(item.version)}</span></div>
        <p>${escapeHtml(item.description || t("未填写描述"))}</p>
        <small>${escapeHtml(item.compatibility_note)}</small>
        <button class="button subtle inspect-catalog-candidate" type="button" data-candidate-index="${index}" ${item.installable ? "" : "disabled"}>${t("检查候选")}</button>
      </article>`).join("") : `<div class="empty-dag">${t("没有找到兼容的 MCP Server。")}</div>`;
    document.querySelectorAll("[data-candidate-index]").forEach((node) => node.addEventListener("click", () => selectCatalogCandidate(Number(node.dataset.candidateIndex))));
  } catch (error) { $("mcp-catalog-results").innerHTML = `<div class="empty-dag audit-error">${escapeHtml(error.message)}</div>`; }
  finally { button.disabled = false; }
}

function selectCatalogCandidate(index) {
  const candidate = state.catalogCandidates?.[index]; if (!candidate) return;
  state.catalogCandidate = candidate; state.catalogPreview = null;
  $("mcp-import-name").value = candidate.runtime_name; $("mcp-import-version").value = candidate.version;
  $("mcp-import-owner").value = "local-user"; $("mcp-import-endpoint").value = candidate.endpoint || "";
  $("mcp-import-error").textContent = candidate.authentication_required ? t("该候选需要 Bearer 凭据；当前一键发现仅支持匿名 Server。") : "";
  $("mcp-preview-button").disabled = candidate.authentication_required; $("mcp-preview-tools").innerHTML = "";
  $("mcp-import-actions").classList.add("hidden"); $("mcp-import-panel").classList.remove("hidden");
  $("mcp-import-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function previewCatalogCandidate() {
  $("mcp-preview-button").disabled = true; $("mcp-import-error").textContent = "";
  try {
    const preview = await api("/api/v1/mcp/catalog/discovery-preview", { method: "POST", body: JSON.stringify({
      endpoint_reference: $("mcp-import-endpoint").value.trim(), expected_server_name: $("mcp-import-name").value.trim(), expected_protocol_version: "2025-11-25"
    }) });
    state.catalogPreview = preview; const prefix = $("mcp-import-name").value.trim().toLowerCase().replace(/[^a-z0-9]+/g, ".").replace(/^\.+|\.+$/g, "");
    $("mcp-preview-tools").innerHTML = preview.tools.length ? preview.tools.map((tool) => {
      const safe = tool.read_only_hint === true && tool.input_schema;
      const key = `${prefix}.${tool.name}`.replace(/[^a-zA-Z0-9_.-]/g, "-");
      return `<label class="tool-choice ${safe ? "" : "disabled"}"><input type="checkbox" data-preview-tool="${escapeHtml(tool.name)}" data-logical-key="${escapeHtml(key)}" ${safe ? "checked" : "disabled"}><span><strong>${escapeHtml(tool.name)}</strong><small>${escapeHtml(tool.description || (safe ? t("已声明只读") : t("缺少可信只读标记")))}</small></span></label>`;
    }).join("") : `<p class="muted">${t("Server 没有返回 Tool。")}</p>`;
    $("mcp-import-actions").classList.toggle("hidden", !preview.tools.some((tool) => tool.read_only_hint === true && tool.input_schema));
  } catch (error) { $("mcp-import-error").textContent = `${error.message} ${t("请确认 Runtime name 与 Server 初始化身份完全一致。")}`; }
  finally { $("mcp-preview-button").disabled = false; }
}

async function importCatalogTools() {
  const selected = [...document.querySelectorAll("[data-preview-tool]:checked")]; if (!selected.length || !state.catalogPreview) return;
  const button = $("mcp-import-button"); button.disabled = true; $("mcp-import-error").textContent = "";
  try {
    const server = await api("/api/v1/mcp/servers", { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify({
      owner_id: $("mcp-import-owner").value.trim(), name: $("mcp-import-name").value.trim(), description: state.catalogCandidate?.description || "",
      transport: "STREAMABLE_HTTP", endpoint_reference: $("mcp-import-endpoint").value.trim(), authentication_required: false
    }) });
    const version = await api(`/api/v1/mcp/servers/${server.id}/versions`, { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify({
      semantic_version: $("mcp-import-version").value.trim(), protocol_version: state.catalogPreview.protocol_version,
      configuration: { source: "official-mcp-registry", registry_name: state.catalogCandidate?.registry_name, endpoint: $("mcp-import-endpoint").value.trim() }
    }) });
    for (const input of selected) {
      const tool = state.catalogPreview.tools.find((item) => item.name === input.dataset.previewTool);
      await api(`/api/v1/mcp/server-versions/${version.id}/tools`, { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify({
        logical_key: input.dataset.logicalKey, tool_name: tool.name, description: tool.description || "", side_effect: "READ_ONLY", input_schema: tool.input_schema
      }) });
    }
    await api(`/api/v1/mcp/server-versions/${version.id}/publish`, { method: "POST" });
    $("mcp-catalog-dialog").close(); await loadTools({ quiet: false }); switchView("tools");
    toast(t("MCP Server 与只读 Tools 已发布"));
  } catch (error) { $("mcp-import-error").textContent = error.message; }
  finally { button.disabled = false; }
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
  $("agent-description").textContent = agent.description || t("未填写描述");
  $("agent-tags").innerHTML = agent.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  $("agent-lifecycle").textContent = agent.lifecycle; $("agent-version-count").textContent = agent.versions.length;
  $("agent-visibility").textContent = agent.visibility;
  const defaultVersion = agent.versions.find((version) => version.id === agent.default_version_id);
  $("agent-default-version").textContent = defaultVersion?.semantic_version || "—";
  const versions = [...agent.versions].sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
  $("agent-version-list").innerHTML = versions.length ? versions.map(renderAgentVersion).join("") : `<div class="empty-dag">${t("还没有 Agent Version。")}</div>`;
  bindVersionActions();
}

function renderAgentVersion(version) {
  const model = version.model_policy || {}; const tools = version.tool_profile?.allowed_tools || [];
  const modelConnection = state.modelConnections.find((item) => item.id === model.connection_id);
  const modelDetails = model.provider === "openai" ? `${escapeHtml(model.reasoning_effort || "")} · ${escapeHtml(model.max_output_tokens)} tokens` : ["deepseek"].includes(model.provider) ? `${escapeHtml(model.max_output_tokens)} tokens` : t("部署级策略");
  const connectionDetails = model.connection_id ? `${t("Connection")}: ${escapeHtml(modelConnection?.name || shortId(model.connection_id))}` : "";
  return `<article class="version-card ${version.status === "PUBLISHED" ? "published" : ""}">
    <div class="version-heading"><div><span class="version-number">v${escapeHtml(version.semantic_version)}</span><span class="pill">${escapeHtml(version.status)}</span></div><code>${escapeHtml(shortId(version.content_digest?.replace("sha256:", "")))}</code></div>
    <div class="policy-grid">
      <div><span>${t("角色")}</span><strong>${escapeHtml(version.role)}</strong></div>
      <div><span>${t("模型")}</span><strong>${escapeHtml(providerLabel(model))}</strong><small>${modelDetails}${connectionDetails ? ` · ${connectionDetails}` : ""}</small></div>
      <div><span>${t("Tool 预算")}</span><strong>${tools.length ? t("{tools} 个 / {calls} 次", { tools: tools.length, calls: escapeHtml(version.tool_profile.max_calls) }) : t("无模型 Tool")}</strong><small>${tools.map(escapeHtml).join(" · ") || t("默认关闭")}</small></div>
      <div><span>${t("已验证能力")}</span><strong>${escapeHtml(version.verified_capabilities.join(", ") || t("尚未验证"))}</strong><small>${escapeHtml(version.runtime_adapter)}</small></div>
    </div>
    <details><summary>${t("查看指令与策略 JSON")}</summary><pre>${escapeHtml(JSON.stringify({ instructions: version.instructions, model_policy: model, tool_profile: version.tool_profile }, null, 2))}</pre></details>
    ${version.status === "DRAFT" ? `<div class="version-actions"><button class="button subtle submit-version" type="button" data-version-id="${version.id}">${t("提交审核")}</button></div>` : version.status === "IN_REVIEW" ? `<div class="version-actions"><button class="button primary publish-version" type="button" data-version-id="${version.id}">${t("发布版本")}</button></div>` : ""}
  </article>`;
}

function renderArtifactDetail(artifact, { resetPreview = true } = {}) {
  const versions = [...artifact.versions].sort((left, right) => right.version_number - left.version_number); const latest = versions[0];
  $("artifact-id").textContent = shortId(artifact.id); $("artifact-name").textContent = artifact.display_name;
  $("artifact-kind").textContent = artifact.kind; $("artifact-classification").textContent = artifact.classification;
  $("artifact-updated").textContent = t("更新于 {time}", { time: age(artifact.updated_at) }); $("artifact-version-count").textContent = artifact.version_count;
  $("artifact-owner").textContent = artifact.owner_id; $("artifact-media-type").textContent = latest?.media_type || "—";
  $("artifact-size").textContent = latest ? bytesLabel(latest.size_bytes) : "—"; if (resetPreview) $("artifact-preview-panel").classList.add("hidden");
  $("artifact-version-list").innerHTML = versions.length ? versions.map((version) => `
    <article class="artifact-version-card">
      <div class="version-heading"><div><span class="version-number">v${version.version_number}</span><span class="pill">${escapeHtml(version.status)}</span></div><code title="${escapeHtml(version.sha256)}">sha256:${escapeHtml(shortId(version.sha256))}</code></div>
      <div class="artifact-version-meta"><span>${escapeHtml(version.media_type)}</span><span>${bytesLabel(version.size_bytes)}</span><span>${escapeHtml(version.storage_class)}</span><span>Scan: ${escapeHtml(version.scan_status)}</span></div>
      <div class="artifact-lineage"><span>Producer Run</span><code>${escapeHtml(version.producer_run_id || t("未绑定"))}</code><small>${new Date(version.created_at).toLocaleString()}</small></div>
      <div class="version-actions"><button class="button subtle preview-artifact-version" type="button" data-preview-version="${version.id}">${t("预览")}</button><button class="button subtle download-artifact-version" type="button" data-download-version="${version.id}" data-version-number="${version.version_number}" data-media-type="${escapeHtml(version.media_type)}">${t("下载")}</button></div>
    </article>`).join("") : `<div class="empty-dag">${t("Artifact 还没有可用版本。")}</div>`;
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
    toast(t("Artifact Version 下载已开始"));
  } catch (error) { toast(error.message, true); }
}

function taskLinkedArtifacts(task) {
  const runIds = new Set(task.runs.map((run) => run.id));
  return state.artifacts.flatMap((artifact) => artifact.versions.filter((version) => version.producer_run_id && runIds.has(version.producer_run_id)).map((version) => ({ artifact, version })));
}

function renderTaskArtifacts() {
  const panel = $("task-artifact-panel"); panel.classList.toggle("hidden", !featureEnabled("artifact_service"));
  if (!featureEnabled("artifact_service") || !state.selected) return;
  const linked = taskLinkedArtifacts(state.selected);
  $("task-artifact-count").textContent = t("{count} 个版本", { count: linked.length });
  $("task-artifact-list").innerHTML = linked.length ? linked.map(({ artifact, version }) => `
    <article class="artifact-lineage-item"><div><strong>${escapeHtml(artifact.display_name)} · v${version.version_number}</strong><small>${escapeHtml(artifact.kind)} · ${escapeHtml(version.media_type)} · ${bytesLabel(version.size_bytes)}</small></div><code>${escapeHtml(shortId(version.sha256))}</code><button class="button subtle open-linked-artifact" type="button" data-linked-artifact="${artifact.id}">${t("打开")}</button></article>`).join("") : `<div class="empty-dag">${t("当前任务的 Run 尚未绑定 Artifact Version。")}</div>`;
  document.querySelectorAll("[data-linked-artifact]").forEach((button) => button.addEventListener("click", () => { switchView("artifacts"); selectArtifact(button.dataset.linkedArtifact); }));
}

function renderApprovalDetail(action) {
  $("approval-id").textContent = shortId(action.approval_id || action.id); $("approval-action-type").textContent = action.action_type;
  $("approval-status").textContent = action.approval_status; $("approval-result").textContent = action.policy_result;
  $("approval-expiry").textContent = t("到期 {time}", { time: new Date(action.expires_at).toLocaleString() });
  $("approval-requester").textContent = action.requester_id; $("approval-resource").textContent = action.resource_type;
  $("approval-resource-id").textContent = action.resource_id; $("approval-policy-version").textContent = action.policy_version;
  $("approval-policy-bundle").textContent = action.policy_bundle; $("approval-action-hash").textContent = shortId(action.action_hash);
  $("approval-action-hash").title = action.action_hash; $("approval-arguments").textContent = JSON.stringify(action.arguments, null, 2);
  const pending = action.approval_status === "PENDING";
  $("approval-actions").classList.toggle("hidden", !pending);
  $("approval-permit-state").textContent = action.permit_id ? (action.approval_status === "CONSUMED" ? t("已消费") : t("已签发")) : t("未签发");
  $("copy-permit-button").classList.toggle("hidden", !action.permit_id || action.approval_status === "CONSUMED");
  $("copy-permit-button").dataset.permitId = action.permit_id || "";
  $("approval-decision-count").textContent = t("{count} 条记录", { count: action.decisions.length });
  $("approval-decisions").innerHTML = action.decisions.length ? [...action.decisions].reverse().map((decision) => `
    <div class="decision-item"><div><strong>${escapeHtml(decision.outcome)}</strong><small>${escapeHtml(decision.approver_id)} · ${age(decision.created_at)}</small></div><p>${escapeHtml(decision.reason)}</p></div>`).join("") : `<div class="empty-dag">${t("尚未作出决定。")}</div>`;
}

function openDecision(outcome) {
  const action = state.selectedApproval; if (!action?.approval_id) return;
  $("decision-form").reset(); $("decision-approval-id").value = action.approval_id; $("decision-outcome").value = outcome;
  $("decision-title").textContent = outcome === "approve" ? t("批准执行意图") : t("拒绝执行意图");
  $("decision-submit-button").textContent = outcome === "approve" ? t("确认批准") : t("确认拒绝");
  $("decision-submit-button").classList.toggle("primary", outcome === "approve"); $("decision-submit-button").classList.toggle("danger", outcome === "reject"); $("decision-error").textContent = "";
  $("decision-dialog").showModal(); setTimeout(() => $("decision-reason").focus(), 50);
}

async function submitDecision(event) {
  event.preventDefault(); $("decision-submit-button").disabled = true; $("decision-error").textContent = "";
  const approvalId = $("decision-approval-id").value; const outcome = $("decision-outcome").value;
  try {
    const decided = await api(`/api/v1/approvals/${approvalId}/${outcome}`, { method: "POST", body: JSON.stringify({ reason: $("decision-reason").value.trim() }) });
    $("decision-dialog").close(); await loadApprovals({ quiet: false }); selectApproval(decided.id); toast(outcome === "approve" ? t("Permit 已签发") : t("执行意图已拒绝"));
  } catch (error) { $("decision-error").textContent = error.message; }
  finally { $("decision-submit-button").disabled = false; }
}

async function copySelectedPermit() {
  const permit = $("copy-permit-button").dataset.permitId; if (!permit) return;
  try { await navigator.clipboard.writeText(permit); toast(t("Permit 已复制；仅可用于完全匹配的操作一次")); }
  catch { toast(t("浏览器无法访问剪贴板，请从 API 响应复制 Permit"), true); }
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
    const created = await api("/api/v1/artifacts", { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify(payload) });
    $("artifact-dialog").close(); await loadArtifacts({ quiet: false }); selectArtifact(created.id); toast(t("Artifact 与首个版本已创建"));
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
    const updated = await api(`/api/v1/artifacts/${state.selectedArtifactId}/versions`, { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify(payload) });
    $("artifact-version-dialog").close(); await loadArtifacts({ quiet: false }); selectArtifact(updated.id); toast(t("不可变 Artifact Version 已追加"));
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
    $("agent-dialog").close(); await loadAgents({ quiet: false }); selectAgent(created.id); toast(t("Agent Definition 已创建"));
  } catch (error) { $("agent-form-error").textContent = error.message; }
  finally { $("agent-create-button").disabled = false; }
}

function openVersionForm() {
  if (!state.selectedAgent) return;
  $("version-form").reset(); $("version-semver").value = `0.1.${state.selectedAgent.versions.length}`;
  $("version-capabilities").value = "general.task"; $("version-provider").value = "inherit";
  $("version-model").value = "gpt-5.6-terra"; $("version-effort").value = "low"; $("version-max-tokens").value = "1200"; $("version-max-calls").value = "3";
  $("version-form-error").textContent = ""; renderVersionToolOptions(); renderVersionConnections(); syncProviderFields(); $("version-dialog").showModal(); setTimeout(() => $("version-role").focus(), 50);
}

function renderVersionToolOptions() {
  const node = $("version-tool-options"); if (!node) return;
  const readOnly = state.tools.filter((tool) => tool.side_effect === "READ_ONLY");
  node.innerHTML = readOnly.length ? readOnly.map((tool) => `<label class="tool-choice"><input type="checkbox" value="${escapeHtml(tool.logical_key)}"><span><strong>${escapeHtml(tool.logical_key)}</strong><small>${escapeHtml(tool.description || tool.server_name)}</small></span></label>`).join("") : `<p class="muted">${t("当前没有可选择的已发布只读 Tool，可在下方手动填写逻辑 Key。")}</p>`;
}

function syncProviderFields() {
  const provider = $("version-provider").value; const external = provider === "openai" || provider === "deepseek";
  document.querySelectorAll("[data-openai-field]").forEach((field) => field.classList.toggle("hidden", !external));
  document.querySelectorAll("[data-connection-field]").forEach((field) => field.classList.toggle("hidden", !external));
  document.querySelectorAll("[data-reasoning-field]").forEach((field) => field.classList.toggle("hidden", provider !== "openai"));
  if (provider === "deepseek" && $("version-model").value === "gpt-5.6-terra") $("version-model").value = "deepseek-flash";
  if (provider === "openai" && $("version-model").value === "deepseek-flash") $("version-model").value = "gpt-5.6-terra";
  renderVersionConnections();
}

async function createVersion(event) {
  event.preventDefault(); if (!state.selectedAgentId) return;
  $("version-form-error").textContent = "";
  const provider = $("version-provider").value;
  if (provider === "deepseek" && !$("version-connection").value) { $("version-form-error").textContent = "DeepSeek requires a saved model connection."; return; }
  $("version-create-button").disabled = true;
  const selectedTools = [...document.querySelectorAll("#version-tool-options input:checked")].map((input) => input.value);
  const tools = [...new Set([...selectedTools, ...csv($("version-tools").value)])];
  const modelPolicy = provider === "inherit" ? {} : provider === "deterministic" ? { provider } : {
    provider, model: $("version-model").value.trim(),
    ...(provider === "openai" ? { reasoning_effort: $("version-effort").value } : {}),
    max_output_tokens: Number($("version-max-tokens").value),
    ...($("version-connection").value ? { connection_id: $("version-connection").value } : {})
  };
  const payload = {
    semantic_version: $("version-semver").value.trim(), role: $("version-role").value.trim(), instructions: $("version-instructions").value.trim(),
    declared_capabilities: csv($("version-capabilities").value), input_schema: { type: "object" }, output_schema: { type: "object" },
    model_policy: modelPolicy, tool_profile: tools.length ? { allowed_tools: tools, max_calls: Number($("version-max-calls").value) } : {},
    runtime_adapter: "local", execution_modes: ["async"]
  };
  try {
    await api(`/api/v1/agents/${state.selectedAgentId}/versions`, { method: "POST", body: JSON.stringify(payload) });
    $("version-dialog").close(); await refreshSelectedAgent(); toast(t("Agent Version 草稿已创建"));
  } catch (error) { $("version-form-error").textContent = error.message; }
  finally { $("version-create-button").disabled = false; }
}

async function submitVersion(versionId) {
  try { await api(`/api/v1/agent-versions/${versionId}/submit-review`, { method: "POST" }); await refreshSelectedAgent(); toast(t("版本已提交审核")); }
  catch (error) { toast(error.message, true); }
}

function openPublish(versionId) {
  const version = state.selectedAgent?.versions.find((item) => item.id === versionId); if (!version) return;
  $("publish-version-id").value = versionId; $("publish-capabilities").value = version.declared_capabilities.join(", ");
  $("publish-default").checked = true; $("publish-permit").value = ""; $("publish-form-error").textContent = "";
  $("publish-request-status").textContent = ""; $("publish-request-status").classList.add("hidden");
  const governed = featureEnabled("policy_approval"); $("publish-permit-field").classList.toggle("hidden", !governed); $("request-publish-approval").classList.toggle("hidden", !governed);
  $("publish-form-note").textContent = governed ? t("发布受 Policy Approval 保护，请填写与本次发布参数完全匹配的一次性 Permit。") : t("当前未启用 Policy Approval；发布仍由 Registry 状态机和 API 权限保护。");
  $("publish-dialog").showModal();
}

function publishArguments() {
  return { verified_capabilities: csv($("publish-capabilities").value), make_default: $("publish-default").checked };
}

async function requestPublishApproval() {
  const button = $("request-publish-approval"); button.disabled = true; $("publish-form-error").textContent = "";
  try {
    const action = await api("/api/v1/policy/actions", { method: "POST", headers: { "Idempotency-Key": clientRequestId() }, body: JSON.stringify({
      action_type: "agent.version.publish", resource_type: "agent_version", resource_id: $("publish-version-id").value, arguments: publishArguments()
    }) });
    if (action.permit_id) $("publish-permit").value = action.permit_id;
    const status = $("publish-request-status"); status.classList.remove("hidden");
    status.textContent = action.approval_status === "PENDING" ? t("审批请求 {id} 已创建；请由独立 APPROVER 审核。", { id: shortId(action.approval_id) }) : t("Policy 结果：{result}；{detail}", { result: action.policy_result, detail: action.permit_id ? t("Permit 已填入。") : action.reason_code });
    toast(action.approval_status === "PENDING" ? t("审批请求已创建") : t("Policy 已完成决策"));
  } catch (error) { $("publish-form-error").textContent = error.message; }
  finally { button.disabled = false; }
}

async function publishVersion(event) {
  event.preventDefault(); $("publish-button").disabled = true; $("publish-form-error").textContent = "";
  const permit = $("publish-permit").value.trim(); const headers = permit ? { "Execution-Permit-Id": permit } : {};
  const payload = publishArguments();
  try {
    await api(`/api/v1/agent-versions/${$("publish-version-id").value}/publish`, { method: "POST", headers, body: JSON.stringify(payload) });
    $("publish-dialog").close(); await refreshSelectedAgent(); toast(t("Agent Version 已发布"));
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
    if (state.selectedId !== id) return;
    if (previous && state.missionReplay.mode === "live") deriveMissionPulses(previous, next);
    else if (!previous) { state.missionSelectedId = null; state.missionPulses = []; resetMissionReplay(); resetMissionCamera(); }
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
      try {
        const shared = await api(`/api/v1/tasks/${id}/replay-bookmarks`);
        state.missionBookmarks[id] = shared.map((bookmark) => bookmark.event_id);
        saveMissionReplayBookmarks();
      }
      catch (error) { if (!quiet) toast(`Shared bookmarks unavailable: ${error.message}`, true); }
    }
    if (featureEnabled("mcp_read_tools")) {
      try { state.toolAudit = (await api(`/api/v1/tasks/${id}/tool-invocations`)).items; }
      catch (error) { state.toolAuditError = error.message; }
    }
    if (state.selectedId !== id) return;
    const fingerprint = JSON.stringify([
      next, state.planning, state.planningError, state.activity, state.activityError,
      state.interactions, state.interactionError, state.toolAudit, state.toolAuditError,
      state.missionBookmarks[id]
    ]);
    if (state.detailFingerprintTaskId === id && state.detailFingerprint === fingerprint) {
      $("task-updated").textContent = t("更新于 {time}", { time: age(next.updated_at) });
      $("poll-time").textContent = t("自动刷新 · {time}", { time: new Date().toLocaleTimeString() });
      return;
    }
    state.detailFingerprintTaskId = id;
    state.detailFingerprint = fingerprint;
    renderDetail();
  }
  catch (error) { if (!quiet) toast(error.message, true); }
}

function renderDetail() {
  const task = state.selected; if (!task) return;
  $("task-id").textContent = shortId(task.id); $("task-objective").textContent = task.objective;
  $("task-status").textContent = task.status; $("task-mode").textContent = task.execution_mode;
  $("task-updated").textContent = t("更新于 {time}", { time: age(task.updated_at) }); $("poll-time").textContent = t("自动刷新 · {time}", { time: new Date().toLocaleTimeString() });
  const units = task.subtasks.length || (task.runs.length ? 1 : 0);
  const completed = task.subtasks.length ? task.subtasks.filter((item) => item.status === "COMPLETED").length : (task.status === "COMPLETED" ? 1 : 0);
  const progress = task.status === "COMPLETED" ? 100 : units ? Math.round(completed / units * 100) : 0;
  $("progress-value").textContent = `${progress}%`; $("progress-bar").style.width = `${progress}%`;
  $("unit-count").textContent = `${completed} / ${units}`; $("concurrency").textContent = task.max_concurrency; $("run-count").textContent = task.runs.length;
  $("run-button").disabled = task.status !== "CREATED";
  $("pause-button").disabled = !busy.has(task.status);
  $("resume-button").disabled = !["PAUSED", "WAITING_APPROVAL"].includes(task.status);
  $("cancel-button").disabled = terminal.has(task.status);
  if (state.missionView === "map") renderMissionMap(task);
  renderDag(task); renderRuns(task); renderPlanning(); renderActivityTimeline(); renderToolAudit(); renderTaskArtifacts();
  renderTaskResult(task);
}

function resultSources(task) {
  const output = task.output || task.candidate_output;
  if (!output) return [];
  if (task.execution_mode === "COORDINATED" && task.output?.agent?.kind === "deterministic-demo") {
    const predecessors = new Set(task.subtasks.flatMap((unit) => unit.depends_on || []));
    const finalUnits = task.subtasks.filter((unit) => unit.output && unit.status === "COMPLETED" && !predecessors.has(unit.key));
    if (finalUnits.length) return finalUnits.map((unit) => ({
      output: unit.output, label: unit.input?.role || unit.key, agent: unit.output.agent?.id || unit.preferred_agent_id || unit.key
    }));
  }
  return [{ output, label: t(task.output ? "任务结果" : "待审核的候选结果"), agent: output.agent?.id || null }];
}

function readableResultText(output) {
  for (const key of ["summary", "report", "answer", "text", "content", "result"]) {
    if (typeof output?.[key] === "string" && output[key].trim()) return output[key].trim();
  }
  return null;
}

function renderTaskResult(task) {
  const raw = task.output || task.candidate_output;
  const details = $("task-raw-details");
  details.classList.toggle("hidden", !raw);
  $("task-output").textContent = raw ? JSON.stringify(raw, null, 2) : "";
  $("result-label").textContent = task.error ? t("执行异常") : task.output ? t("最终输出") : task.candidate_output ? t("待审核") : t("等待执行");
  if (task.error) { $("task-result-content").innerHTML = `<p class="result-alert error">${escapeHtml(t("错误：{error}", { error: task.error }))}</p>`; return; }
  if (!raw) { $("task-result-content").innerHTML = `<p class="result-empty">${t("任务尚未产生输出。")}</p>`; return; }
  const cards = resultSources(task).map(({ output, label, agent }) => {
    const text = readableResultText(output);
    const demo = output.agent?.kind === "deterministic-demo";
    return `<article class="result-deliverable"><div class="result-source"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(agent || t("未知员工"))}</span></div>${demo ? `<p class="result-alert">${t("演示结果只验证执行流程，不是模型生成的回答。")}</p>` : ""}${text ? `<div class="result-body">${escapeHtml(text)}</div>` : `<p class="result-empty">${t("此结果没有可阅读的正文，请查看原始 JSON。")}</p>`}</article>`;
  }).join("");
  const linked = taskLinkedArtifacts(task);
  const files = linked.length ? `<div class="result-files"><h4>${t("相关产物")}</h4>${linked.map(({ artifact, version }) => `<button class="result-file" type="button" data-result-artifact="${escapeHtml(artifact.id)}"><span><strong>${escapeHtml(artifact.display_name)} · v${version.version_number}</strong><small>${escapeHtml(version.media_type)} · ${bytesLabel(version.size_bytes)}</small></span><span aria-hidden="true">↗</span></button>`).join("")}</div>` : "";
  $("task-result-content").innerHTML = `${!task.output ? `<p class="result-alert">${t("候选结果尚未成为最终交付，请先完成审核。")}</p>` : ""}${cards}${files}`;
  document.querySelectorAll("[data-result-artifact]").forEach((button) => button.addEventListener("click", () => { switchView("artifacts"); selectArtifact(button.dataset.resultArtifact); }));
}

function renderPlanning() {
  const panel = $("planning-panel"); const task = state.selected;
  const enabled = featureEnabled("dynamic_replanning") && task?.execution_mode === "COORDINATED";
  panel.classList.toggle("hidden", !enabled); if (!enabled) return;
  $("planning-version").textContent = task.plan_version ? `Plan v${task.plan_version}` : "";
  $("propose-plan-patch").disabled = !["CREATED", "WAITING_APPROVAL"].includes(task.status) || !state.planning;
  if (state.planningError) { $("plan-patch-list").innerHTML = `<div class="empty-dag audit-error">${t("无法读取计划治理信息：")}${escapeHtml(state.planningError)}</div>`; return; }
  const patches = state.planning?.patches || [];
  $("plan-patch-list").innerHTML = patches.length ? [...patches].reverse().map((patch) => `
    <article class="plan-patch-card">
      <div class="audit-heading"><strong>v${patch.base_plan_version} → v${patch.proposed_plan_version}</strong><span class="pill ${statusClass(patch.status)}">${escapeHtml(patch.status)}</span></div>
      <p>${escapeHtml(patch.reason)} · ${escapeHtml(patch.requested_by)} · ${age(patch.created_at)}</p>
      <div class="plan-evidence">${patch.evidence.map((finding) => `<span class="${finding.passed ? "passed" : "failed"}" title="${escapeHtml(`${finding.message} · ${JSON.stringify(finding.details || {})}`)}">${finding.passed ? "✓" : "×"} ${escapeHtml(finding.code)}</span>`).join("")}</div>
      ${patch.status === "VERIFIED" ? `<button class="button subtle apply-plan-patch" data-patch-id="${patch.id}" type="button">${t("应用已验证方案")}</button>` : ""}
    </article>`).join("") : `<div class="empty-dag">${t("尚未提出 Plan Patch；任务进入安全静止点后可替换未开始的工作。")}</div>`;
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
    $("plan-patch-dialog").close(); await loadTask(state.selectedId); toast(t("Plan Patch 已通过安全验证"));
  } catch (error) { $("plan-patch-error").textContent = error.message; }
  finally { $("plan-patch-submit").disabled = false; }
}

async function applyPlanPatch(patchId) {
  try { await api(`/api/v1/tasks/${state.selectedId}/plan-patches/${patchId}/apply`, { method: "POST" }); await loadTask(state.selectedId); toast(t("剩余计划已原子替换")); }
  catch (error) { toast(error.message, true); }
}

function renderActivityTimeline() {
  const panel = $("activity-panel");
  panel.classList.toggle("hidden", !featureEnabled("activity_timeline"));
  if (!featureEnabled("activity_timeline")) return;
  $("activity-count").textContent = state.activityError ? t("不可用") : t("{count} 条事件", { count: state.activity.length });
  $("activity-list").innerHTML = state.activityError ? `<div class="empty-dag audit-error">${t("无法读取活动时间线：")}${escapeHtml(state.activityError)}</div>` : state.activity.length ? state.activity.map((item) => {
    const details = Object.entries(item.details || {}).slice(0, 4).map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(String(value))}`).join(" · ");
    return `<article class="audit-item activity-item">
      <span class="audit-marker ${statusClass(item.status)}"></span>
      <div><div class="audit-heading"><strong>${escapeHtml(item.title)}</strong><span class="pill">${escapeHtml(item.status)}</span></div>
      <p>${escapeHtml(item.category.toUpperCase())} · ${new Date(item.occurred_at).toLocaleString()}${item.actor ? ` · ${escapeHtml(item.actor)}` : ""}</p>
      ${details ? `<small class="activity-details">${details}</small>` : ""}
      <code>${escapeHtml(item.entity_type)} ${escapeHtml(shortId(item.entity_id))}${item.trace_id ? ` · trace ${escapeHtml(shortId(item.trace_id))}` : ""}</code></div>
    </article>`;
  }).join("") : `<div class="empty-dag">${t("当前任务还没有活动记录。")}</div>`;
}

function renderToolAudit() {
  const panel = $("tool-audit-panel");
  panel.classList.toggle("hidden", !featureEnabled("mcp_read_tools"));
  if (!featureEnabled("mcp_read_tools")) return;
  $("tool-audit-count").textContent = state.toolAuditError ? t("不可用") : t("{count} 次调用", { count: state.toolAudit.length });
  $("tool-audit-list").innerHTML = state.toolAuditError ? `<div class="empty-dag audit-error">${t("无法读取 Tool 审计：")}${escapeHtml(state.toolAuditError)}</div>` : state.toolAudit.length ? [...state.toolAudit].reverse().map((item) => `
    <article class="audit-item">
      <span class="audit-marker ${statusClass(item.status)}"></span>
      <div><div class="audit-heading"><strong>${escapeHtml(item.tool_key)}</strong><span class="pill">${escapeHtml(item.status)}</span></div>
      <p>${escapeHtml(item.server_name)} · ${escapeHtml(item.side_effect)} · ${age(item.started_at)}</p>
      <code>invocation ${escapeHtml(shortId(item.id))} · schema ${escapeHtml(shortId(item.schema_digest?.replace("sha256:", "")))}</code>
      ${item.error ? `<small class="audit-error">${escapeHtml(item.error)}</small>` : ""}</div>
    </article>`).join("") : `<div class="empty-dag">${t("这个任务还没有调用 MCP Tool。")}</div>`;
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

async function bookmarkMissionReplay() {
  if (!state.selected) return;
  const events = missionReplayEvents(state.selected); const event = events[state.missionReplay.cursor];
  if (!event) return;
  try {
    await api(`/api/v1/tasks/${state.selected.id}/replay-bookmarks`, {
      method: "POST",
      body: JSON.stringify({ event_id: event.id, label: event.title.slice(0, 120) })
    });
    const bookmarks = new Set(state.missionBookmarks[state.selected.id] || []); bookmarks.add(event.id);
    state.missionBookmarks[state.selected.id] = [...bookmarks]; saveMissionReplayBookmarks(); renderMissionMap(state.selected);
    toast("Shared replay bookmark saved");
  }
  catch (error) { toast(error.message, true); }
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

function resetMissionCamera() {
  state.missionCamera = { zoom: 1, autoFit: true, layout: null, panning: null };
}

function missionCameraClamp(value) {
  return Math.max(.25, Math.min(2.5, value));
}

function updateMissionCameraSurface({ center = null } = {}) {
  const canvas = $("mission-canvas"); const svg = canvas.querySelector("svg"); const layout = state.missionCamera.layout;
  if (!svg || !layout) return;
  const zoom = state.missionCamera.zoom;
  svg.setAttribute("width", Math.round(layout.width * zoom)); svg.setAttribute("height", Math.round(layout.height * zoom));
  $("mission-zoom-label").textContent = `${Math.round(zoom * 100)}%`;
  if (center) {
    canvas.scrollLeft = center.x * zoom - canvas.clientWidth / 2;
    canvas.scrollTop = center.y * zoom - canvas.clientHeight / 2;
  }
  renderMissionMinimap();
}

function fitMissionCamera() {
  const canvas = $("mission-canvas"); const layout = state.missionCamera.layout;
  if (!layout || !canvas.clientWidth || !canvas.clientHeight) return;
  state.missionCamera.autoFit = false;
  state.missionCamera.zoom = missionCameraClamp(Math.min((canvas.clientWidth - 18) / layout.width, (canvas.clientHeight - 18) / layout.height));
  updateMissionCameraSurface({ center: { x: layout.width / 2, y: layout.height / 2 } });
}

function setMissionCameraZoom(value) {
  const canvas = $("mission-canvas"); const oldZoom = state.missionCamera.zoom;
  const center = { x: (canvas.scrollLeft + canvas.clientWidth / 2) / oldZoom, y: (canvas.scrollTop + canvas.clientHeight / 2) / oldZoom };
  state.missionCamera.autoFit = false; state.missionCamera.zoom = missionCameraClamp(value);
  updateMissionCameraSurface({ center });
}

function resetMissionCameraZoom() {
  state.missionCamera.autoFit = false; state.missionCamera.zoom = 1;
  updateMissionCameraSurface(); $("mission-canvas").scrollTo({ left: 0, top: 0 });
}

function focusMissionCamera() {
  const layout = state.missionCamera.layout; if (!layout) return;
  const unit = layout.units.find((candidate) => candidate.id === state.missionSelectedId);
  const point = unit ? layout.positions.get(unit.key) : layout.hq;
  if (!point) return;
  state.missionCamera.autoFit = false; state.missionCamera.zoom = Math.max(.85, state.missionCamera.zoom);
  updateMissionCameraSurface({ center: { x: point.x + (unit ? 90 : 80), y: point.y + (unit ? 47 : 52) } });
}

function renderMissionMinimap() {
  const panel = $("mission-minimap"); const layout = state.missionCamera.layout; const canvas = $("mission-canvas");
  if (!layout || !canvas.querySelector("svg")) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const zoom = state.missionCamera.zoom; const x = canvas.scrollLeft / zoom; const y = canvas.scrollTop / zoom;
  const width = Math.min(layout.width, canvas.clientWidth / zoom); const height = Math.min(layout.height, canvas.clientHeight / zoom);
  const units = layout.units.map((unit) => {
    const point = layout.positions.get(unit.key);
    return `<rect class="minimap-node ${statusClass(unit.status)}" x="${point.x}" y="${point.y}" width="180" height="94" rx="8"/>`;
  }).join("");
  const external = layout.externalEndpoints.map((endpoint) => {
    const point = layout.externalPositions.get(missionEndpointKey(endpoint));
    return `<rect class="minimap-external" x="${point.x}" y="${point.y}" width="170" height="64" rx="7"/>`;
  }).join("");
  panel.innerHTML = `<svg viewBox="0 0 ${layout.width} ${layout.height}" preserveAspectRatio="none" role="img" aria-label="Map overview"><rect class="minimap-hq" x="${layout.hq.x}" y="${layout.hq.y}" width="160" height="104" rx="9"/>${units}${external}<rect class="minimap-viewport" x="${x}" y="${y}" width="${width}" height="${height}" rx="6"/></svg>`;
  panel.onclick = (event) => {
    const rect = panel.getBoundingClientRect();
    const center = { x: (event.clientX - rect.left) / rect.width * layout.width, y: (event.clientY - rect.top) / rect.height * layout.height };
    updateMissionCameraSurface({ center });
  };
}

function bindMissionCamera(layout) {
  const canvas = $("mission-canvas"); state.missionCamera.layout = layout;
  if (state.missionCamera.autoFit) fitMissionCamera(); else updateMissionCameraSurface();
  canvas.onscroll = renderMissionMinimap;
  canvas.onpointerdown = (event) => {
    if (event.button !== 0 || event.target.closest("[data-mission-node]")) return;
    state.missionCamera.panning = { x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
    canvas.classList.add("panning"); canvas.setPointerCapture(event.pointerId);
  };
  canvas.onpointermove = (event) => {
    const pan = state.missionCamera.panning; if (!pan) return;
    canvas.scrollLeft = pan.left - (event.clientX - pan.x); canvas.scrollTop = pan.top - (event.clientY - pan.y);
  };
  const stop = (event) => {
    if (!state.missionCamera.panning) return;
    state.missionCamera.panning = null; canvas.classList.remove("panning");
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  };
  canvas.onpointerup = stop; canvas.onpointercancel = stop;
  canvas.onwheel = (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault(); setMissionCameraZoom(state.missionCamera.zoom + (event.deltaY < 0 ? .12 : -.12));
  };
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
  const changed = state.missionView !== view;
  state.missionView = view;
  $("mission-view").classList.toggle("hidden", view !== "map"); $("board-view").classList.toggle("hidden", view !== "board");
  $("mission-view-button").classList.toggle("active", view === "map"); $("board-view-button").classList.toggle("active", view === "board");
  if (changed && view === "map" && state.selected) renderMissionMap(state.selected);
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
  if (state.missionView !== "map") return;
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
  bindMissionCamera(layout);
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
    $("dag").innerHTML = `<article class="work-card ${statusClass(task.status)}"><div class="card-top"><span class="card-key">direct</span><span class="pill">${escapeHtml(task.status)}</span></div><h4>${t("直接执行")}</h4><p>${escapeHtml(task.objective)}</p><div class="agent-line"><span class="avatar">A</span><div><strong>${escapeHtml(run?.agent_id || t("等待分配"))}</strong><small>general.task</small></div></div></article>`;
    return;
  }
  $("dag").innerHTML = task.subtasks.map((unit) => {
    const run = runsBySubtask.get(unit.id); const agent = run?.agent_id || unit.preferred_agent_id || t("等待调度");
    return `<article class="work-card ${statusClass(unit.status)}">
      <div class="card-top"><span class="card-key">${escapeHtml(unit.key)}</span><span class="pill">${escapeHtml(unit.status)}</span></div>
      <h4>${escapeHtml(unit.input?.role || unit.key)}</h4><p>${escapeHtml(unit.objective)}</p>
      ${unit.depends_on.length ? `<div class="dependency">${t("依赖")} → ${unit.depends_on.map(escapeHtml).join(" · ")}</div>` : `<div class="dependency">${t("起始节点 · 可立即调度")}</div>`}
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
  }).join("") : `<div class="empty-dag">${t("开始执行后，Run 会出现在这里。")}</div>`;
}

async function taskAction(action) {
  if (!state.selectedId) return;
  try { await api(`/api/v1/tasks/${state.selectedId}/${action}`, { method: "POST", headers: action === "runs" ? { "Idempotency-Key": clientRequestId() } : {} }); await loadTasks({ quiet: true }); toast(action === "runs" ? t("任务已进入执行队列") : t("操作已提交")); }
  catch (error) { toast(error.message, true); }
}

const roleDefaults = [
  { key: "research", role: t("Research"), objective: t("Collect facts, constraints, and relevant context") },
  { key: "analysis", role: t("Analysis"), objective: t("Analyze the materials and develop candidate findings"), depends: ["research"] },
  { key: "synthesis", role: t("Synthesis"), objective: t("Combine upstream work into the final deliverable"), depends: ["research", "analysis"] }
];
function addRole(value = {}) {
  const row = document.createElement("div"); row.className = "role-row";
  const key = value.key || `work-${clientRequestId().slice(0, 8)}`;
  const published = publishedDefaultAgents({ asyncOnly: true });
  const agentOptions = published.map((agent) => `<option value="${escapeHtml(agent.name)}" ${agent.name === value.agent ? "selected" : ""}>${escapeHtml(agent.name)}${agent.description ? ` — ${escapeHtml(agent.description)}` : ""}</option>`).join("");
  row.innerHTML = `<label>${t("Work item")}<input class="role-name" required maxlength="40" value="${escapeHtml(value.role || t("New work item"))}"></label><label>${t("Published employee")}<select class="role-agent" required><option value="">${published.length ? t("Choose an employee") : t("No published employees available")}</option>${agentOptions}</select></label><label>${t("Deliverable")}<input class="role-objective" required maxlength="20000" value="${escapeHtml(value.objective || t("Describe this work item's output"))}"></label><label>${t("Depends on")}<select class="role-depends" multiple aria-label="${t("Depends on")}"></select></label><button class="icon-button remove-role" type="button" aria-label="${t("Remove work item")}">×</button><input class="role-key" type="hidden" value="${escapeHtml(key)}"><input class="role-capability" type="hidden" value="general.task">`;
  row.querySelector(".remove-role").addEventListener("click", () => { row.remove(); updateRoleDependencies(); });
  row.querySelector(".role-name").addEventListener("input", updateRoleDependencies);
  row.dataset.key = key; row.dataset.dependencies = JSON.stringify(value.depends || []);
  $("role-list").appendChild(row); updateRoleDependencies();
}
function updateRoleDependencies() {
  const rows = [...document.querySelectorAll(".role-row")];
  for (const row of rows) {
    const select = row.querySelector(".role-depends");
    const previous = new Set([...select.selectedOptions].map((option) => option.value));
    for (const key of JSON.parse(row.dataset.dependencies || "[]")) previous.add(key);
    select.innerHTML = rows.filter((candidate) => candidate !== row).map((candidate) => `<option value="${escapeHtml(candidate.dataset.key)}">${escapeHtml(candidate.querySelector(".role-name").value.trim() || t("Untitled work item"))}</option>`).join("");
    [...select.options].forEach((option) => { option.selected = previous.has(option.value); });
    row.dataset.dependencies = "[]";
  }
}
function hasDependencyCycle(subtasks) {
  const byKey = new Map(subtasks.map((item) => [item.key, item])); const active = new Set(); const done = new Set();
  const visit = (key) => { if (active.has(key)) return true; if (done.has(key)) return false; active.add(key); for (const dependency of byKey.get(key)?.depends_on || []) if (byKey.has(dependency) && visit(dependency)) return true; active.delete(key); done.add(key); return false; };
  return subtasks.some((item) => visit(item.key));
}
function syncExecutionMode() {
  const coordinated = $("execution-mode").value === "COORDINATED";
  $("direct-agent-field").classList.toggle("hidden", $("execution-mode").value !== "DIRECT");
  $("execution-row").classList.toggle("single-column", !coordinated);
  $("team-fields").classList.toggle("hidden", !coordinated);
  $("team-fields").disabled = !coordinated;
  $("concurrency-field").classList.toggle("hidden", !coordinated);
  $("max-concurrency").disabled = !coordinated;
  $("execution-guidance").textContent = coordinated
    ? t("Split the goal into deliverables. Each item is pinned to a published employee; prerequisite work must finish first.")
    : $("execution-mode").value === "REVIEWED"
      ? t("A deterministic reviewer checks the result and can request bounded revisions. This is a review policy, not a second employee.")
      : t("Runs with the deployment default, or select a published employee for this task. Reviewed mode uses the deployment default.");
}
function openCreate(mode = "DIRECT") {
  const option = [...$("execution-mode").options].find((item) => item.value === mode);
  if (option?.disabled) { switchView("setup"); toast("This execution mode needs server setup. See Workspace setup.", true); return; }
  $("create-form").reset(); $("execution-mode").value = mode; $("role-list").innerHTML = "";
  state.pendingTaskPayload = null; $("task-edit-step").classList.remove("hidden"); $("task-review-step").classList.add("hidden");
  roleDefaults.forEach(addRole); $("form-error").textContent = ""; $("task-review-error").textContent = ""; updateTaskModeOptions(); syncExecutionMode();
  $("create-dialog").showModal(); setTimeout(() => $("objective").focus(), 50);
}

async function createTask(event) {
  event.preventDefault(); const mode = $("execution-mode").value; const objective = $("objective").value.trim();
  const materials = $("task-materials").value.trim(); const expected = $("task-expected-output").value.trim();
  const successCriteria = $("task-success-criteria").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const rows = [...document.querySelectorAll(".role-row")];
  const subtasks = mode === "COORDINATED" ? rows.map((row, index) => ({
    key: row.querySelector(".role-key").value.replace(/[^a-zA-Z0-9_-]/g, "-").toLowerCase() || `work-${index + 1}`,
    objective: `${row.querySelector(".role-name").value.trim()}: ${row.querySelector(".role-objective").value.trim()}`, input: {
      role: row.querySelector(".role-name").value.trim(), goal: objective, materials, expected_output: expected, success_criteria: successCriteria
    },
    required_capabilities: [row.querySelector(".role-capability").value],
    preferred_agent_id: row.querySelector(".role-agent").value.trim(),
    depends_on: [...row.querySelector(".role-depends").selectedOptions].map((option) => option.value)
  })) : [];
  if (!objective) { $("form-error").textContent = "Add a goal before creating the task."; return; }
  if (successCriteria.length > 20) { $("form-error").textContent = "Use no more than 20 success conditions."; return; }
  if (mode !== "DIRECT" && (!featureEnabled(mode === "REVIEWED" ? "reviewed_execution" : "coordinated_execution") || (mode === "COORDINATED" && !featureEnabled("agent_registry_management")))) { $("form-error").textContent = "This execution mode is disabled by the server. Review Setup for details."; return; }
  if (mode === "COORDINATED" && subtasks.length < 2) { $("form-error").textContent = "Coordinated work requires at least two work items."; return; }
  if (mode === "COORDINATED" && subtasks.some((item) => !item.preferred_agent_id)) { $("form-error").textContent = "Choose a published employee for every work item."; return; }
  if (mode === "COORDINATED" && hasDependencyCycle(subtasks)) { $("form-error").textContent = "The dependency selections contain a cycle. Remove a prerequisite link and try again."; return; }
  const input = {}; if (materials) input.materials = materials; if (expected) input.expected_output = expected;
  if (successCriteria.length) input.success_criteria = successCriteria;
  if ($("task-use-memory").checked) {
    const policy = state.memorySetup?.policy; const companyId = state.memoryCompany?.company?.id;
    if (!featureEnabled("organizational_memory") || !state.memorySetup?.enabled || !state.memorySetup?.configured || !policy?.id || !companyId) { $("form-error").textContent = "An active company memory policy is required. Review Setup and try again."; return; }
    input.company_context = { company_id: companyId, memory_policy_id: policy.id };
  }
  const payload = { objective, input, execution_mode: mode, ...(mode === "REVIEWED" ? { max_revisions: 1 } : {}), ...(mode === "DIRECT" && $("direct-agent").value ? { preferred_agent_id: $("direct-agent").value } : {}), ...(mode === "COORDINATED" ? { subtasks, max_concurrency: Number($("max-concurrency").value), ...(successCriteria.length ? { goal: { success_criteria: successCriteria } } : {}) } : {}) };
  const maxRuns = $("budget-max-runs").value.trim(); const deadline = $("task-deadline").value;
  if (maxRuns || deadline) {
    if (!featureEnabled("budget_admission")) { $("form-error").textContent = "Budget admission is disabled. Remove the optional limits or enable the feature on the server."; return; }
    const budget = {}; if (maxRuns) budget.max_runs = Number(maxRuns); if (deadline) budget.deadline = new Date(deadline).toISOString(); payload.budget = budget;
  }
  state.pendingTaskPayload = payload;
  renderTaskReview(payload, { mode, objective, expected, successCriteria, subtasks });
  $("form-error").textContent = ""; $("task-review-error").textContent = "";
  $("task-edit-step").classList.add("hidden"); $("task-review-step").classList.remove("hidden");
}

function renderTaskReview(payload, { mode, objective, expected, successCriteria, subtasks }) {
  const modeLabel = t(mode === "DIRECT" ? "Direct" : mode === "REVIEWED" ? "Reviewed" : "Coordinated");
  const assignment = mode === "DIRECT" ? $("direct-agent").selectedOptions[0]?.textContent || t("Use deployment default employee")
    : mode === "REVIEWED" ? t("Deployment default employee + deterministic reviewer")
      : subtasks.map((item) => `${item.input.role}: ${item.preferred_agent_id}${item.depends_on.length ? ` (${t("Depends on")}: ${item.depends_on.join(", ")})` : ""}`).join("; ");
  const success = successCriteria.length ? successCriteria.map((item) => `<li>${escapeHtml(item)}</li>`).join("") : `<li>${t("No additional success conditions specified.")}</li>`;
  const coordinatedNote = mode === "COORDINATED" ? t("Saved on the coordinated goal contract and included as task input context.") : t("Included as task input guidance; not a separate verified gate in this execution mode.");
  const memoryPolicy = state.memorySetup?.policy;
  const memorySummary = payload.input.company_context
    ? `${escapeHtml(state.memoryCompany?.company?.name || t("Active company"))} · ${escapeHtml(memoryPolicy?.key || t("Reviewed memory policy"))} · ${t("This task only")}`
    : t("Not included");
  const budgetParts = [];
  if (payload.budget?.max_runs) budgetParts.push(t("Maximum runs: {count}", { count: payload.budget.max_runs }));
  if (payload.budget?.deadline) budgetParts.push(t("Deadline: {time}", { time: new Date(payload.budget.deadline).toLocaleString() }));
  const budgetSummary = budgetParts.length ? budgetParts.join(" · ") : t("No extra run or deadline limit");
  $("task-review-summary").innerHTML = `<div><dt>${t("Goal")}</dt><dd>${escapeHtml(objective)}</dd></div><div><dt>${t("Expected output")}</dt><dd>${escapeHtml(expected || t("Not specified"))}</dd></div><div><dt>${t("Success conditions")}</dt><dd><ul>${success}</ul><small>${coordinatedNote}</small></dd></div><div><dt>${t("Execution")}</dt><dd>${escapeHtml(modeLabel)} · ${escapeHtml(assignment)}</dd></div><div><dt>${t("Materials")}</dt><dd>${escapeHtml(payload.input.materials ? t("Materials included") : t("No additional materials"))}</dd></div><div><dt>${t("Company memory")}</dt><dd>${memorySummary}</dd></div><div><dt>${t("Optional limits")}</dt><dd>${escapeHtml(budgetSummary)}</dd></div>`;
}

async function submitReviewedTask(runAfterCreate) {
  if (!state.pendingTaskPayload) return;
  const buttons = [$("create-task-only"), $("create-and-run")]; buttons.forEach((button) => { button.disabled = true; });
  $("task-review-error").textContent = "";
  try {
    const task = await api("/api/v1/tasks", { method: "POST", body: JSON.stringify(state.pendingTaskPayload) });
    $("create-dialog").close(); state.pendingTaskPayload = null;
    await loadTasks({ quiet: true }); await selectTask(task.id);
    if (runAfterCreate) {
      try {
        await api(`/api/v1/tasks/${encodeURIComponent(task.id)}/runs`, { method: "POST", headers: { "Idempotency-Key": clientRequestId() } });
        await loadTasks({ quiet: true }); await selectTask(task.id); toast(t("Task created and queued to run."));
      } catch (error) { toast(t("Task created, but could not start the run: {error}", { error: error.message }), true); }
    } else toast(t("Task created. It has not been started."));
  } catch (error) { $("task-review-error").textContent = error.message; }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

$("new-task-button").addEventListener("click", () => state.view === "agents" ? openAgentForm() : state.view === "artifacts" ? openArtifactForm() : openCreate("DIRECT"));
$("empty-direct-task").addEventListener("click", () => openCreate("DIRECT")); $("empty-new-task").addEventListener("click", () => openCreate("COORDINATED"));
$("tasks-nav").addEventListener("click", () => switchView("tasks")); $("agents-nav").addEventListener("click", () => switchView("agents")); $("tools-nav").addEventListener("click", () => switchView("tools")); $("artifacts-nav").addEventListener("click", () => switchView("artifacts")); $("approvals-nav").addEventListener("click", () => switchView("approvals")); $("company-nav").addEventListener("click", () => switchView("company")); $("memory-nav").addEventListener("click", () => switchView("memory"));
$("setup-nav").addEventListener("click", () => { switchView("setup"); loadProductSetup(); });
$("advanced-nav").addEventListener("click", (event) => { if (event.target.closest(".advanced-nav-menu .nav-button")) $("advanced-nav").open = false; });
$("back-to-task-edit").addEventListener("click", () => { $("task-review-step").classList.add("hidden"); $("task-edit-step").classList.remove("hidden"); });
$("create-task-only").addEventListener("click", () => submitReviewedTask(false));
$("create-and-run").addEventListener("click", () => submitReviewedTask(true));
$("company-choice").addEventListener("change", () => {
  state.selectedMemoryCompanyId = $("company-choice").value || null;
  loadProductSetup().then(() => loadMemory({ quiet: true }));
});
$("auth-open-settings").addEventListener("click", () => { if (!credentialOriginSafe()) { toast(t("Use HTTPS or a local SSH tunnel before entering an access token."), true); return; } $("token").disabled = false; $("token").value = ""; $("token-dialog").showModal(); });
$("browse-mcp-catalog").addEventListener("click", openMcpCatalog); $("browse-mcp-catalog-detail").addEventListener("click", openMcpCatalog);
$("mcp-catalog-form").addEventListener("submit", searchMcpCatalog); $("mcp-preview-button").addEventListener("click", previewCatalogCandidate); $("mcp-import-button").addEventListener("click", importCatalogTools);
$("new-version-button").addEventListener("click", openVersionForm); $("agent-form").addEventListener("submit", createAgent); $("version-form").addEventListener("submit", createVersion); $("publish-form").addEventListener("submit", publishVersion); $("request-publish-approval").addEventListener("click", requestPublishApproval); $("version-provider").addEventListener("change", syncProviderFields);
$("artifact-form").addEventListener("submit", createArtifact); $("new-artifact-version-button").addEventListener("click", openArtifactVersionForm); $("artifact-version-form").addEventListener("submit", createArtifactVersion); $("close-artifact-preview").addEventListener("click", () => $("artifact-preview-panel").classList.add("hidden"));
$("approve-approval-button").addEventListener("click", () => openDecision("approve")); $("reject-approval-button").addEventListener("click", () => openDecision("reject")); $("decision-form").addEventListener("submit", submitDecision); $("copy-permit-button").addEventListener("click", copySelectedPermit);
$("add-role").addEventListener("click", () => addRole()); $("create-form").addEventListener("submit", createTask);
$("company-template-form").addEventListener("submit", installCompanyTemplate);
$("company-operations-form").addEventListener("submit", activateCompanyOperations);
$("appoint-company-workforce").addEventListener("click", appointCompanyWorkforce);
$("start-company-operations").addEventListener("click", startCompanyOperations);
$("market-research-form").addEventListener("submit", launchMarketResearch);
$("open-agent-registry").addEventListener("click", () => switchView("agents"));
$("propose-plan-patch").addEventListener("click", openPlanPatchForm); $("plan-patch-form").addEventListener("submit", submitPlanPatch);
$("execution-mode").addEventListener("change", syncExecutionMode);
$("manage-models").addEventListener("click", openModelConnectionForm);
$("company-setup-form").addEventListener("submit", createCompanyWorkspace);
$("open-memory-setup").addEventListener("click", () => switchView("memory"));
$("open-company-setup").addEventListener("click", () => {
  if (!featureEnabled("company_model")) { toast("Enable company_model in server setup first.", true); return; }
  if (state.memoryCompany) {
    if (featureEnabled("company_packs")) switchView("company");
    else toast("A company workspace is active. Company packs are optional.");
    return;
  }
  $("company-setup-form").classList.remove("hidden");
  $("company-name").focus();
});
$("save-memory-setup").addEventListener("click", saveMemorySetup);
$("model-connection-form").addEventListener("submit", saveModelConnection);
$("connection-credential-type").addEventListener("change", syncConnectionCredentialFields);
$("connection-provider").addEventListener("change", () => { $("connection-model").value = ""; });
$("version-provider").addEventListener("change", syncProviderFields);
$("version-connection").addEventListener("change", () => {
  const connection = state.modelConnections.find((item) => item.id === $("version-connection").value);
  if (connection?.model) $("version-model").value = connection.model;
});
$("model-connection-list").addEventListener("click", (event) => {
  const test = event.target.closest("[data-connection-test]"); const disable = event.target.closest("[data-connection-disable]");
  if (test) testModelConnection(test.dataset.connectionTest); if (disable) disableModelConnection(disable.dataset.connectionDisable);
});
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
$("mission-zoom-out").addEventListener("click", () => setMissionCameraZoom(state.missionCamera.zoom - .15));
$("mission-zoom-in").addEventListener("click", () => setMissionCameraZoom(state.missionCamera.zoom + .15));
$("mission-camera-fit").addEventListener("click", fitMissionCamera);
$("mission-camera-focus").addEventListener("click", focusMissionCamera);
$("mission-camera-reset").addEventListener("click", resetMissionCameraZoom);
$("search").addEventListener("input", renderSidebarList); $("token-button").addEventListener("click", () => { $("token").value = state.token; $("token-dialog").showModal(); });
$("refresh-memory").addEventListener("click", () => loadMemory({ quiet: false }));
$("memory-status-filter").addEventListener("change", renderMemory);
$("memory-review-form").addEventListener("submit", submitMemoryReview);
$("manual-memory-form").addEventListener("submit", saveManualMemoryNote);
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
$("token-form").addEventListener("submit", async (event) => { event.preventDefault(); if (!credentialOriginSafe() && $("token").value.trim()) { toast("Use HTTPS or a local SSH tunnel before entering an access token.", true); return; } state.token = $("token").value.trim(); state.token ? sessionStorage.setItem("agentmesh-token", state.token) : sessionStorage.removeItem("agentmesh-token"); $("token-dialog").close(); await loadConsole(); });

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
  refreshCredentialSecurity();
  try { await loadFeatures(); await Promise.all([loadTasks(), loadAgents({ quiet: true }), loadTools({ quiet: true }), loadArtifacts({ quiet: true }), loadApprovals({ quiet: true }), loadCompanyTemplate({ quiet: true }), loadMemory({ quiet: true })]); configureUpdates();
    await loadProductSetup();
    const canBrowseCatalog = featureEnabled("governed_mcp"); $("browse-mcp-catalog").classList.toggle("hidden", !canBrowseCatalog); $("browse-mcp-catalog-detail").classList.toggle("hidden", !canBrowseCatalog);
  }
  catch (error) { $("connection").classList.remove("online"); if (/401|403|authentication|bearer/i.test(error.message)) showAuthenticationNotice(); else { $("connection").lastChild.textContent = t("连接异常"); toast(error.message, true); } }
}
async function pollConsole() {
  if (document.hidden || state.pollInFlight) return;
  state.pollInFlight = true;
  try {
    if (state.view === "agents") await loadAgents({ quiet: true });
    else if (state.view === "tools") await loadTools({ quiet: true });
    else if (state.view === "artifacts") await loadArtifacts({ quiet: true });
    else if (state.view === "approvals") await loadApprovals({ quiet: true });
    else if (state.view === "company") await loadCompanyTemplate({ quiet: true });
    else if (state.view === "memory") await loadMemory({ quiet: true });
    else await loadTasks({ quiet: true });
  } finally { state.pollInFlight = false; }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleActiveRefresh(); });
loadConsole();
