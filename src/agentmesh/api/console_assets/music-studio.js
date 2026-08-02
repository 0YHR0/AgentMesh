const COPY = {
  en: {
    office: "Office", studio: "Music Studio", admin: "Admin", kicker: "CREATIVE COMPANY",
    title: "Make a song with your AI team.",
    subtitle: "Describe the outcome. Your employees research, write, produce, listen, and return one traceable release for your approval.",
    checking: "Checking studio", setupTitle: "Set up your studio",
    setupHint: "Demo mode uses no API key and makes no external request.", companyName: "Company name",
    defaultLanguage: "Default language", defaultGenre: "Default genre", install: "Create studio",
    briefTitle: "Describe the song", briefHint: "Keep it directional. The team will turn it into a production plan.",
    songTitle: "Working title", audience: "Audience", language: "Language", mood: "Mood", themes: "Themes",
    genre: "Genre attributes", rounds: "Maximum rounds",
    safety: "No artist imitation · no voice cloning · no publishing", start: "Start project",
    currentProject: "CURRENT PROJECT", round: "Round", phase: "Phase", provider: "Provider",
    newProject: "New project", teamWork: "Team at work", listen: "Compare and decide",
    compareHint: "Select one direction before approval", approve: "Approve release", revise: "Request revision",
    failedCriterion: "What did not meet your goal?", requestedChange: "What should the team change?",
    cancel: "Cancel", sendRevision: "Send to team", approved: "Release approved",
    approvedHint: "The selected audio and its evidence are now immutable.", download: "Download package",
    selected: "Selected", select: "Select this version", score: "review score"
  },
  "zh-CN": {
    office: "公司", studio: "音乐工作室", admin: "管理员后台", kicker: "创意公司",
    title: "和你的 AI 团队一起完成一首歌。",
    subtitle: "描述创作目标，员工会完成调研、作词、制作与试听，并把有完整记录的作品交给你审批。",
    checking: "正在检查工作室", setupTitle: "创建你的工作室",
    setupHint: "演示模式不需要 API Key，也不会访问外部服务。", companyName: "公司名称",
    defaultLanguage: "默认语言", defaultGenre: "默认曲风", install: "创建工作室",
    briefTitle: "描述你想要的歌曲", briefHint: "给出方向即可，团队会把它转化为制作方案。",
    songTitle: "作品名称", audience: "目标听众", language: "歌曲语言", mood: "情绪", themes: "主题",
    genre: "曲风特征", rounds: "最大修改轮数", safety: "不模仿艺人 · 不克隆声音 · 不自动发布",
    start: "开始创作", currentProject: "当前项目", round: "轮次", phase: "阶段", provider: "生成方式",
    newProject: "新建项目", teamWork: "团队正在协作", listen: "对比并决定",
    compareHint: "批准前请选择一个方向", approve: "批准作品", revise: "要求修改",
    failedCriterion: "哪里没有达到你的目标？", requestedChange: "希望团队怎样修改？",
    cancel: "取消", sendRevision: "发回团队", approved: "作品已批准",
    approvedHint: "选中的音频及其证据已经固定保存。", download: "下载发布包",
    selected: "已选择", select: "选择此版本", score: "评审分"
  }
};
const STEPS = [
  ["brief", "Creative Director", "创意总监"], ["trend", "Trend Researcher", "趋势研究员"],
  ["lyrics", "Lyricist", "作词人"], ["production", "Music Producer", "音乐制作人"],
  ["generation", "Generation Operator", "生成工程师"], ["listening", "Audio Critic", "试听评审"]
];
const state = {
  lang: localStorage.getItem("agentmesh-language") === "zh-CN" ? "zh-CN" : "en",
  token: sessionStorage.getItem("agentmesh-token") || "",
  taskId: localStorage.getItem("agentmesh-music-task") || "",
  timer: null, urls: new Map(), packageVersionId: ""
};
const byId = id => document.getElementById(id);
const t = key => COPY[state.lang][key] || key;

function translate() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-text]").forEach(node => { node.textContent = t(node.dataset.text); });
  byId("language-toggle").textContent = state.lang === "en" ? "中文" : "English";
}
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(value.message || value.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}
async function artifactBlob(versionId) {
  const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const response = await fetch(`/api/v1/artifact-versions/${versionId}/content`, { headers });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.blob();
}
function show(id) {
  ["setup-view", "create-view", "project-view"].forEach(value => byId(value).classList.toggle("hidden", value !== id));
}
function system(text, kind = "ready") {
  const node = byId("system-state"); node.className = `system-state ${kind}`; node.querySelector("span").textContent = text;
}
function toast(message) {
  const node = byId("toast"); node.textContent = message; node.classList.remove("hidden");
  setTimeout(() => node.classList.add("hidden"), 3500);
}
function split(value) { return value.split(",").map(item => item.trim()).filter(Boolean); }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}
function clearUrls() { state.urls.forEach(url => URL.revokeObjectURL(url)); state.urls.clear(); }

async function initialize() {
  translate();
  try {
    const preview = await api("/api/v1/company-templates/music-studio/preview");
    system(state.lang === "en" ? "Studio ready" : "工作室已就绪");
    if (state.taskId) { show("project-view"); await refresh(); return; }
    show(preview.installable ? "setup-view" : "create-view");
  } catch (error) { system(error.message, "error"); show("setup-view"); }
}
byId("language-toggle").addEventListener("click", async () => {
  state.lang = state.lang === "en" ? "zh-CN" : "en";
  localStorage.setItem("agentmesh-language", state.lang); translate(); await refresh();
});
byId("setup-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    await api("/api/v1/company-templates/music-studio/install", { method: "POST", body: JSON.stringify({ company_name: form.get("company_name"), default_language: form.get("default_language"), default_genre: form.get("default_genre"), use_plan: "internal-demo" }) });
    show("create-view"); toast(state.lang === "en" ? "Studio created" : "工作室创建完成");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
byId("project-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    const result = await api("/api/v1/music-studio/projects", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ title: form.get("title"), audience: form.get("audience"), language: form.get("language"), mood: form.get("mood"), themes: split(form.get("themes")), genre_attributes: split(form.get("genre_attributes")), max_rounds: Number(form.get("max_rounds")) }) });
    state.taskId = result.task.id; localStorage.setItem("agentmesh-music-task", state.taskId);
    show("project-view"); renderTask(result.task); await refresh();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
function renderTask(task) {
  const brief = task.input?.brief || {};
  byId("project-title").textContent = brief.title || task.objective;
  byId("audience-value").textContent = brief.audience || "—";
  byId("mood-value").textContent = brief.mood || "—";
  const byKey = Object.fromEntries((task.subtasks || []).map(item => [item.key, item]));
  let currentPhase = STEPS.find(([key]) => byKey[key]?.status !== "COMPLETED");
  if (!currentPhase) currentPhase = STEPS[STEPS.length - 1];
  byId("phase-value").textContent = state.lang === "en" ? currentPhase[1] : currentPhase[2];
  byId("pipeline").innerHTML = STEPS.map(([key, en, zh]) => {
    const item = byKey[key] || {}; const status = item.status || "BLOCKED";
    const cls = status === "COMPLETED" ? "done" : ["READY", "QUEUED", "RUNNING"].includes(status) ? "active" : "";
    const role = state.lang === "en" ? en : zh;
    return `<article class="pipeline-item ${cls}"><i></i><strong>${escapeHtml(role)}</strong><span>${escapeHtml(status.replaceAll("_", " "))}</span></article>`;
  }).join("");
  byId("project-status").textContent = task.status === "COMPLETED" ? (state.lang === "en" ? "Preparing result" : "正在整理作品") : (state.lang === "en" ? "Team working" : "团队工作中");
}
async function renderCandidates(result) {
  clearUrls();
  const grid = byId("candidate-grid");
  grid.innerHTML = result.candidates.map(candidate => `<article class="candidate ${candidate.selected ? "selected" : ""}">
    <div class="candidate-head"><div><span class="variant">${escapeHtml(candidate.variant)}</span><strong>${state.lang === "en" ? "Direction" : "方案"} ${escapeHtml(candidate.variant)}</strong></div><span class="candidate-score">${candidate.overall_score}<small>/100 ${t("score")}</small></span></div>
    <audio data-version="${candidate.audio_version_id}" controls preload="metadata"></audio>
    <ul class="findings">${candidate.findings.map(value => `<li>${escapeHtml(value)}</li>`).join("")}</ul>
    <button class="${candidate.selected ? "selected-button" : "secondary"}" data-select="${candidate.candidate_id}" type="button" ${candidate.selected ? "disabled" : ""}>${candidate.selected ? `✓ ${t("selected")}` : t("select")}</button>
  </article>`).join("");
  await Promise.all([...grid.querySelectorAll("audio")].map(async audio => {
    const versionId = audio.dataset.version; const url = URL.createObjectURL(await artifactBlob(versionId));
    state.urls.set(versionId, url); audio.src = url;
  }));
}
async function renderResult(result) {
  byId("project-title").textContent = result.title;
  byId("round-value").textContent = `${result.current_round} / ${result.max_rounds}`;
  if (!["WAITING_APPROVAL", "APPROVED"].includes(result.status)) return;
  byId("listening-card").classList.remove("hidden"); await renderCandidates(result);
  const approved = result.status === "APPROVED"; const exhausted = result.current_round >= result.max_rounds;
  state.packageVersionId = result.package_version_id || "";
  byId("decision-actions").classList.toggle("hidden", approved);
  byId("approved-banner").classList.toggle("hidden", !approved);
  byId("download-package").classList.toggle("hidden", !result.package_version_id);
  byId("revision-button").disabled = exhausted;
  byId("revision-button").title = exhausted ? (state.lang === "en" ? "Maximum rounds reached" : "已达到最大轮次") : "";
  byId("project-status").textContent = approved ? (state.lang === "en" ? "Complete" : "已完成") : (state.lang === "en" ? "Waiting for you" : "等待你的决定");
}
async function refresh() {
  if (!state.taskId) return;
  try {
    const task = await api(`/api/v1/tasks/${state.taskId}`); renderTask(task);
    let result = await api(`/api/v1/music-studio/projects/${state.taskId}`);
    if (result.status === "READY") result = await api(`/api/v1/music-studio/projects/${state.taskId}/materialize`, { method: "POST" });
    await renderResult(result); byId("poll-time").textContent = new Date().toLocaleTimeString();
    if (!["WAITING_APPROVAL", "APPROVED"].includes(result.status)) schedule();
  } catch (error) { toast(error.message); schedule(5000); }
}
function schedule(delay = 1800) { clearTimeout(state.timer); state.timer = setTimeout(refresh, delay); }
byId("candidate-grid").addEventListener("click", async event => {
  const button = event.target.closest("[data-select]"); if (!button) return; button.disabled = true;
  try {
    const result = await api(`/api/v1/music-studio/projects/${state.taskId}/select`, { method: "POST", body: JSON.stringify({ candidate_id: button.dataset.select }) });
    await renderResult(result); toast(state.lang === "en" ? "Candidate selected" : "已选择候选版本");
  } catch (error) { toast(error.message); button.disabled = false; }
});
byId("approve-button").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  try { await renderResult(await api(`/api/v1/music-studio/projects/${state.taskId}/approve`, { method: "POST" })); }
  catch (error) { toast(error.message); event.currentTarget.disabled = false; }
});
byId("download-package").addEventListener("click", async () => {
  if (!state.packageVersionId) return;
  try {
    const url = URL.createObjectURL(await artifactBlob(state.packageVersionId));
    const link = document.createElement("a"); link.href = url; link.download = `agentmesh-music-${state.taskId}.zip`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { toast(error.message); }
});
byId("revision-button").addEventListener("click", () => byId("revision-form").classList.remove("hidden"));
byId("cancel-revision").addEventListener("click", () => byId("revision-form").classList.add("hidden"));
byId("revision-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    const result = await api(`/api/v1/music-studio/projects/${state.taskId}/revision`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ failed_criterion: form.get("failed_criterion"), requested_change: form.get("requested_change") }) });
    event.currentTarget.reset(); event.currentTarget.classList.add("hidden"); await renderResult(result);
    toast(state.lang === "en" ? "The team returned two revised candidates" : "团队已返回两个修改版本");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
byId("new-project").addEventListener("click", () => {
  clearTimeout(state.timer); state.taskId = ""; localStorage.removeItem("agentmesh-music-task");
  byId("listening-card").classList.add("hidden"); clearUrls(); show("create-view");
});
initialize();
