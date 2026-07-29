const $ = (id) => document.getElementById(id);
const STORAGE_LANGUAGE = "agentmesh-language";
const STORAGE_SPACES = "agentmesh-office-custom-spaces-v1";
const ACTIVE_RUNS = new Set(["READY", "RUNNING", "PAUSE_REQUESTED", "PAUSED"]);
const TERMINAL_TASKS = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const COLORS = ["#5aa9b8", "#857caf", "#b8945e", "#609d84", "#b87886", "#668fab"];
const SKIN_TONES = ["#f0c6a4", "#d9a783", "#bb7f5f", "#8e5d46", "#654335"];
const HAIR_TONES = ["#26343c", "#513a32", "#6a5036", "#d0c3a5", "#38485c"];
const DEFAULT_OFFICE_GRID = { cell_size: 2, origin_x: -35, origin_z: -12, columns: 35, rows: 12 };
const DEPARTMENTS = {
  product: { grid_x: 0, grid_z: 0, width: 8, depth: 5, x: -27, z: -7, color: "#c7838c", accent: "#e5b6bc", floor: "#6f5158", style: "product" },
  research: { grid_x: 9, grid_z: 0, width: 8, depth: 5, x: -9, z: -7, color: "#67a8b8", accent: "#add3db", floor: "#456e79", style: "research" },
  analysis: { grid_x: 18, grid_z: 0, width: 8, depth: 5, x: 9, z: -7, color: "#8b82ae", accent: "#c4beda", floor: "#5e5877", style: "analysis" },
  security: { grid_x: 27, grid_z: 0, width: 8, depth: 5, x: 27, z: -7, color: "#688eaa", accent: "#b2c8d8", floor: "#455f74", style: "security" },
  design: { grid_x: 0, grid_z: 7, width: 8, depth: 5, x: -27, z: 7, color: "#b47f9f", accent: "#d9b4ca", floor: "#76566b", style: "design" },
  engineering: { grid_x: 9, grid_z: 7, width: 8, depth: 5, x: -9, z: 7, color: "#65a18a", accent: "#b4d6c9", floor: "#476f61", style: "engineering" },
  operations: { grid_x: 18, grid_z: 7, width: 8, depth: 5, x: 9, z: 7, color: "#b69a68", accent: "#ddcda9", floor: "#756647", style: "operations" },
  commons: { grid_x: 27, grid_z: 7, width: 8, depth: 5, x: 27, z: 7, color: "#7e9f78", accent: "#c0d3bc", floor: "#566d53", style: "commons" }
};
const LEGACY_CUSTOM_SPACES = loadCustomSpaces();
const CUSTOM_SPACES = [];
const COPY = {
  en: {
    connecting: "Connecting…", online: "Company systems online", offline: "Company data unavailable",
    lightMode: "Lightweight Office", console: "Admin Console", missions: "Company missions",
    employees: "Employees", working: "Working", blocked: "Blocked", search: "Search missions",
    truth: "Authoritative runtime projection", company: "Your AI company", focusEmployee: "Focus employee",
    campus: "ROYAL TECH CAMPUS", home: "Center campus", focus: "Focus selected employee",
    pan: "pan", zoom: "zoom", minimap: "CAMPUS MAP", noGpu: "3D renderer unavailable",
    noGpuHint: "Use the lightweight Office on this device.", selectedMission: "Selected mission",
    runtime: "Runtime", handoffs: "Handoffs", inspect: "Inspect", selectEmployee: "Select an employee",
    selectHint: "Click an Agent in the world or use the roster.", currentWork: "Current work",
    configuration: "Real configuration", version: "Version", lifecycle: "Lifecycle",
    capabilities: "Capabilities", tools: "Tools", noWork: "No assigned work", idle: "IDLE · AT DESK",
    complete: "COMPLETE · AT DESK", language: "中文", low: "ECO · 60%", high: "AUTO · HD",
    research: "Research Lab", analysis: "Analysis Studio", engineering: "Engineering Bay",
    operations: "Review Court", product: "Product Arena", design: "Design Atelier",
    security: "Security Center", commons: "People Commons", hub: "Central Nexus",
    campusPlanner: "Campus planner", createTask: "Create company mission", objective: "Objective",
    objectiveHint: "Describe the outcome the company should deliver", executionMode: "Execution mode",
    coordinated: "Multi-Agent collaboration", direct: "Direct execution", maxConcurrency: "Max concurrency",
    teamWorkflow: "Roles and workflow", addRole: "+ Add role", startNow: "Start execution after creation",
    cancel: "Cancel", createMission: "Create mission", role: "Role", agent: "Agent",
    roleObjective: "Work objective", dependencies: "Depends on", remove: "Remove",
    expandCampus: "Expand your company", campusHint: "Add a new space. The campus boundary and navigation map expand automatically.",
    spaceName: "Space name", spaceStyle: "Style", spaceColor: "Accent", resetCampus: "Reset custom spaces",
    addSpace: "Add space", taskCreated: "Mission created", taskStarted: "Mission created and started",
    customSpaces: "Custom spaces", noCustomSpaces: "No custom spaces yet",
    moveEmployee: "move employee", needsIntervention: "Needs intervention",
    awaitingApproval: "Awaiting approval", workingAtStation: "Working at station",
    executionPaused: "Execution paused", availableAfterDelivery: "Available after delivery",
    available: "Available", toolActivity: "MCP Tool", remoteActivity: "A2A peer",
    approvalActivity: "Approval gate"
  },
  "zh-CN": {
    moveEmployee: "移动员工",
    connecting: "正在连接…", online: "公司系统在线", offline: "公司数据暂时不可用",
    lightMode: "轻量办公室", console: "控制台", missions: "公司任务", employees: "员工",
    working: "工作中", blocked: "阻塞", search: "搜索任务", truth: "权威运行状态投影",
    company: "你的 AI 公司", focusEmployee: "聚焦员工", campus: "皇家科技园区",
    home: "回到园区中心", focus: "聚焦选中员工", pan: "移动", zoom: "缩放",
    minimap: "园区地图", noGpu: "3D 渲染器不可用", noGpuHint: "请在此设备上使用轻量办公室。",
    selectedMission: "当前任务", runtime: "运行状态", handoffs: "交接", inspect: "查看",
    selectEmployee: "选择一名员工", selectHint: "点击场景中的 Agent，或使用员工列表。",
    currentWork: "当前工作", configuration: "真实配置", version: "版本", lifecycle: "生命周期",
    capabilities: "能力", tools: "工具", noWork: "暂无分配工作", idle: "空闲 · 在工位",
    complete: "已完成 · 在工位", language: "EN", low: "节能 · 60%", high: "自动 · 高清",
    research: "研究实验室", analysis: "分析工作室", engineering: "工程工坊",
    operations: "评审大厅", product: "产品作战室", design: "设计工坊",
    security: "安全中心", commons: "员工共享区", hub: "中央枢纽",
    campusPlanner: "园区规划", createTask: "创建公司任务", objective: "总体目标",
    objectiveHint: "描述希望公司最终交付的结果", executionMode: "执行方式",
    coordinated: "多 Agent 协作", direct: "单 Agent 直接执行", maxConcurrency: "最大并发",
    teamWorkflow: "角色与工作流", addRole: "+ 添加角色", startNow: "创建后立即执行",
    cancel: "取消", createMission: "创建任务", role: "角色", agent: "Agent",
    roleObjective: "工作目标", dependencies: "依赖 Key", remove: "删除",
    expandCampus: "扩展你的公司", campusHint: "新增一个空间，园区边界和导航地图会自动扩展。",
    spaceName: "空间名称", spaceStyle: "风格", spaceColor: "强调色", resetCampus: "重置自定义空间",
    addSpace: "新增空间", taskCreated: "任务已创建", taskStarted: "任务已创建并开始执行",
    customSpaces: "自定义空间", noCustomSpaces: "还没有自定义空间",
    needsIntervention: "需要人工介入", awaitingApproval: "等待审批",
    workingAtStation: "正在工位工作", executionPaused: "执行已暂停",
    availableAfterDelivery: "交付后可用", available: "可用",
    toolActivity: "MCP 工具", remoteActivity: "A2A 节点",
    approvalActivity: "审批关卡"
  }
};

const state = {
  language: localStorage.getItem(STORAGE_LANGUAGE) === "zh-CN" ? "zh-CN" : "en",
  token: sessionStorage.getItem("agentmesh-token") || "",
  features: new Map(),
  tasks: [],
  agents: [],
  company: null,
  employees: [],
  selectedTaskId: null,
  selectedEmployeeId: null,
  scene: null,
  engine: null,
  shadowGenerator: null,
  camera: null,
  cameraTarget: null,
  orthoSize: 20,
  campusBounds: null,
  employeeNodes: new Map(),
  departmentLabels: new Map(),
  movement: null,
  animatedHandoffs: new Set(),
  quality: "auto",
  frameSamples: [],
  keys: new Set(),
  loadInFlight: false,
  labelsDirty: true,
  pointerInteraction: null,
  officeLayout: { grid: DEFAULT_OFFICE_GRID, rooms: [], obstacles: [], spaces: [], placements: [] },
  placementByAgent: new Map(),
  dropIndicator: null,
  ambientMeshes: [],
  navigationMarkers: [],
  legacyLayoutMigrationAttempted: false,
  interactions: [],
  animatedInteractions: new Set(),
  interactionEffects: []
};

function t(key) { return COPY[state.language][key] || COPY.en[key] || key; }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}
function shadeHex(value, factor) {
  const hex = String(value || "#4fb8ff").replace("#", "");
  const channel = (offset) => Math.max(0, Math.min(255, Math.round(parseInt(hex.slice(offset, offset + 2), 16) * factor)));
  return `#${[channel(0), channel(2), channel(4)].map((item) => item.toString(16).padStart(2, "0")).join("")}`;
}
function loadCustomSpaces() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_SPACES) || "[]");
    return Array.isArray(parsed) ? parsed.slice(0, 8).filter((item) => item?.key && item?.name && item?.style && item?.color) : [];
  } catch {
    return [];
  }
}
function configureCustomSpaces(spaces) {
  for (const [key, zone] of Object.entries(DEPARTMENTS)) {
    if (zone.custom) delete DEPARTMENTS[key];
  }
  CUSTOM_SPACES.splice(0, CUSTOM_SPACES.length, ...spaces);
  CUSTOM_SPACES.forEach((space, index) => {
    DEPARTMENTS[space.key] = {
      x: -27 + (index % 4) * 18,
      z: 21 + Math.floor(index / 4) * 14,
      color: space.color,
      accent: space.color,
      floor: shadeHex(space.color, .58),
      style: space.style,
      label: space.name,
      custom: true
    };
  });
}
function departmentName(key) { return DEPARTMENTS[key]?.label || t(key); }
function hash(value) {
  let result = 2166136261;
  for (const character of String(value)) result = Math.imul(result ^ character.charCodeAt(0), 16777619);
  return result >>> 0;
}
function shortId(value) { return String(value || "—").slice(0, 8); }
function hexColor(value) { return BABYLON.Color3.FromHexString(value); }
function featureEnabled(name) { return state.features.get(name) === true; }
function selectedTask() { return state.tasks.find((task) => task.id === state.selectedTaskId) || null; }
function employeeByName(name) { return state.employees.find((employee) => employee.name === name) || null; }

async function loadCompanySnapshot() {
  if (!featureEnabled("company_model")) return null;
  try { return await api("/api/v1/companies/active"); }
  catch (error) {
    if (String(error.message).includes("No active Company exists")) return null;
    throw error;
  }
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.message || payload?.detail || `${response.status} ${response.statusText}`);
  return payload;
}

async function loadCompany({ quiet = false } = {}) {
  if (state.loadInFlight) return;
  state.loadInFlight = true;
  try {
    const featurePayload = await api("/api/v1/features");
    state.features = new Map(featurePayload.features.map((feature) => [feature.name, feature.enabled]));
    const [taskPayload, loadedOfficeLayout] = await Promise.all([
      api("/api/v1/tasks?limit=50&offset=0"),
      api("/api/v1/office-layout")
    ]);
    let officeLayout = loadedOfficeLayout;
    if (!officeLayout.spaces?.length && LEGACY_CUSTOM_SPACES.length
      && !state.legacyLayoutMigrationAttempted) {
      state.legacyLayoutMigrationAttempted = true;
      try {
        for (const space of LEGACY_CUSTOM_SPACES) {
          await api("/api/v1/office-layout/spaces", {
            method: "POST",
            body: JSON.stringify({
              name: space.name,
              style: space.style,
              color: space.color
            })
          });
        }
        localStorage.removeItem(STORAGE_SPACES);
        officeLayout = await api("/api/v1/office-layout");
      } catch {
        officeLayout = { ...officeLayout, spaces: LEGACY_CUSTOM_SPACES };
      }
    }
    configureCustomSpaces(officeLayout.spaces || []);
    state.tasks = taskPayload.items;
    state.officeLayout = officeLayout;
    state.placementByAgent = new Map(officeLayout.placements.map((placement) => [placement.agent_id, placement]));
    const [agents, company] = await Promise.all([
      featureEnabled("agent_registry_management")
        ? api("/api/v1/agents?limit=100&offset=0")
        : Promise.resolve({ items: [] }),
      loadCompanySnapshot()
    ]);
    state.agents = agents.items;
    state.company = company;
    state.employees = buildEmployees();
    if (!state.selectedTaskId || !state.tasks.some((task) => task.id === state.selectedTaskId)) {
      state.selectedTaskId = state.tasks.find((task) => !TERMINAL_TASKS.has(task.status))?.id || state.tasks[0]?.id || null;
    }
    await loadTaskInteractions();
    render();
    syncScene();
    animateLatestHandoff();
    animateLatestInteraction();
    setOnline(true);
  } catch (error) {
    setOnline(false);
    if (!quiet) toast(error.message);
  } finally {
    state.loadInFlight = false;
  }
}

async function loadTaskInteractions() {
  if (!state.selectedTaskId || !featureEnabled("activity_timeline")) {
    state.interactions = [];
    return;
  }
  const taskId = state.selectedTaskId;
  try {
    const payload = await api(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/interactions?limit=20`
    );
    if (state.selectedTaskId === taskId) state.interactions = payload.items || [];
  } catch {
    if (state.selectedTaskId === taskId) state.interactions = [];
  }
}

function buildEmployees() {
  const definitions = new Map(state.agents.map((agent) => [agent.name, agent]));
  const positions = new Map((state.company?.positions || []).map((item) => [item.id, item]));
  const units = new Map((state.company?.units || []).map((item) => [item.id, item]));
  const appointments = new Map((state.company?.appointments || [])
    .filter((item) => item.status === "ACTIVE")
    .map((item) => [item.agent_definition_id, item]));
  for (const task of state.tasks) {
    for (const run of task.runs) {
      if (!definitions.has(run.agent_id)) definitions.set(run.agent_id, syntheticAgent(run.agent_id));
    }
  }
  const occupied = new Set(state.officeLayout.placements.map((item) => cellKey(item.grid_x, item.grid_z)));
  return [...definitions.values()].map((agent, index) => {
    const assignment = findAssignment(agent.name);
    const id = agent.id || `runtime:${agent.name}`;
    const placement = state.placementByAgent.get(id);
    const appointment = appointments.get(id);
    const position = positions.get(appointment?.position_id);
    const organizationUnit = units.get(position?.primary_unit_id);
    const organizationZone = organizationUnit && DEPARTMENTS[organizationUnit.key]
      ? organizationUnit.key
      : null;
    const department = placement?.department || organizationZone || departmentFor(agent);
    const cell = placement
      ? { gridX: placement.grid_x, gridZ: placement.grid_z }
      : availableHomeCell(department, index, agent.name, occupied);
    occupied.add(cellKey(cell.gridX, cell.gridZ));
    return {
      id,
      name: agent.name,
      description: agent.description || "",
      tags: agent.tags || [],
      lifecycle: agent.lifecycle || "RUNTIME",
      defaultVersionId: agent.default_version_id || null,
      versions: agent.versions || [],
      appointmentId: appointment?.id || null,
      positionTitle: position?.title || "",
      organizationUnitName: organizationUnit?.name || "",
      department,
      color: COLORS[hash(agent.name) % COLORS.length],
      grid: cell,
      position: cellToWorld(cell.gridX, cell.gridZ),
      persisted: Boolean(placement),
      assignment,
      status: employeeStatus(assignment)
    };
  }).sort((left, right) => left.name.localeCompare(right.name));
}

function syntheticAgent(name) {
  return { id: `runtime:${name}`, name, description: "", lifecycle: "RUNTIME", versions: [], tags: [] };
}

function findAssignment(agentName) {
  let latest = null;
  for (const task of state.tasks) {
    for (const run of task.runs) {
      if (run.agent_id !== agentName) continue;
      const candidate = { task, run, subtask: task.subtasks.find((item) => item.id === run.subtask_id) || null };
      if (!latest || new Date(run.queued_at) > new Date(latest.run.queued_at)) latest = candidate;
    }
  }
  return latest;
}

function departmentFor(agent) {
  const version = agent.versions?.find((item) => item.id === agent.default_version_id)
    || agent.versions?.find((item) => item.status === "PUBLISHED") || agent.versions?.[0];
  const words = `${agent.name} ${agent.description} ${(agent.tags || []).join(" ")} ${version?.role || ""} ${(version?.declared_capabilities || []).join(" ")}`.toLowerCase();
  if (/research|investigat|source|knowledge|search/.test(words)) return "research";
  if (/analy|data|finance|metric|insight/.test(words)) return "analysis";
  if (/engineer|develop|code|tool|system|build/.test(words)) return "engineering";
  if (/review|supervis|operat|synth|approv|manager/.test(words)) return "operations";
  if (/product|strategy|roadmap|market|growth/.test(words)) return "product";
  if (/design|ux|ui|creative|brand|content/.test(words)) return "design";
  if (/security|audit|risk|compliance|privacy/.test(words)) return "security";
  for (const space of CUSTOM_SPACES) {
    const keywords = space.name.toLowerCase().split(/[^a-z0-9\u4e00-\u9fff]+/).filter((value) => value.length > 2);
    if (keywords.some((keyword) => words.includes(keyword))) return space.key;
  }
  if (/people|support|success|community|hr/.test(words)) return "commons";
  const core = ["research", "analysis", "engineering", "operations", "product", "design", "security", "commons"];
  return core[hash(agent.name) % core.length];
}

function cellKey(gridX, gridZ) { return `${gridX}:${gridZ}`; }

function officeGrid() { return state.officeLayout?.grid || DEFAULT_OFFICE_GRID; }

function roomForCell(gridX, gridZ) {
  return (state.officeLayout?.rooms || []).find((room) => (
    gridX >= room.grid_x && gridX < room.grid_x + room.width
    && gridZ >= room.grid_z && gridZ < room.grid_z + room.depth
  )) || null;
}

function cellToWorld(gridX, gridZ) {
  const grid = officeGrid();
  return {
    x: grid.origin_x + (gridX + .5) * grid.cell_size,
    z: grid.origin_z + (gridZ + .5) * grid.cell_size
  };
}

function worldToCell(x, z) {
  const grid = officeGrid();
  return {
    gridX: Math.floor((x - grid.origin_x) / grid.cell_size),
    gridZ: Math.floor((z - grid.origin_z) / grid.cell_size)
  };
}

function walkableOfficeCell(gridX, gridZ) {
  const grid = officeGrid();
  if (gridX < 0 || gridX >= grid.columns || gridZ < 0 || gridZ >= grid.rows) return false;
  if (officeObstacleCell(gridX, gridZ)) return false;
  return Boolean(roomForCell(gridX, gridZ))
    || gridZ === 5 || gridZ === 6
    || gridX === 8 || gridX === 17 || gridX === 26;
}

function officeObstacleCell(gridX, gridZ) {
  return (state.officeLayout?.obstacles || []).find((obstacle) => (
    obstacle.grid_x === gridX && obstacle.grid_z === gridZ
  )) || null;
}

function occupiedOfficeCells(excludedIds = new Set()) {
  return new Set(state.employees
    .filter((employee) => !excludedIds.has(employee.id) && employee.grid)
    .map((employee) => cellKey(employee.grid.gridX, employee.grid.gridZ)));
}

function findGridPath(start, goal, excludedIds = new Set()) {
  const grid = officeGrid();
  if (!start || !goal
    || start.gridX < 0 || start.gridX >= grid.columns
    || start.gridZ < 0 || start.gridZ >= grid.rows
    || goal.gridX < 0 || goal.gridX >= grid.columns
    || goal.gridZ < 0 || goal.gridZ >= grid.rows) return [];
  const blocked = occupiedOfficeCells(excludedIds);
  blocked.delete(cellKey(start.gridX, start.gridZ));
  blocked.delete(cellKey(goal.gridX, goal.gridZ));
  const startKey = cellKey(start.gridX, start.gridZ);
  const goalKey = cellKey(goal.gridX, goal.gridZ);
  const frontier = [{ cell: { ...start }, score: 0 }];
  const cameFrom = new Map();
  const cost = new Map([[startKey, 0]]);
  while (frontier.length) {
    frontier.sort((left, right) => left.score - right.score);
    const current = frontier.shift().cell;
    const currentKey = cellKey(current.gridX, current.gridZ);
    if (currentKey === goalKey) {
      const path = [current];
      let cursor = currentKey;
      while (cameFrom.has(cursor)) {
        const previous = cameFrom.get(cursor);
        path.push(previous);
        cursor = cellKey(previous.gridX, previous.gridZ);
      }
      return path.reverse();
    }
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const next = { gridX: current.gridX + dx, gridZ: current.gridZ + dz };
      const nextKey = cellKey(next.gridX, next.gridZ);
      if ((!walkableOfficeCell(next.gridX, next.gridZ) && nextKey !== goalKey)
        || blocked.has(nextKey)) continue;
      const nextCost = cost.get(currentKey) + 1;
      if (nextCost >= (cost.get(nextKey) ?? Infinity)) continue;
      cost.set(nextKey, nextCost);
      cameFrom.set(nextKey, current);
      const heuristic = Math.abs(goal.gridX - next.gridX) + Math.abs(goal.gridZ - next.gridZ);
      frontier.push({ cell: next, score: nextCost + heuristic });
    }
  }
  return [];
}

function routeWorldPoints(path) {
  return path.map((cell) => {
    const point = cellToWorld(cell.gridX, cell.gridZ);
    return new BABYLON.Vector3(point.x, .35, point.z);
  });
}

function showNavigationRoute(points, purpose = "handoff") {
  clearNavigationRoute();
  if (!state.scene || points.length < 2) return;
  const color = purpose === "handoff" ? "#53e8ff" : "#e5c978";
  const routeMaterial = material(
    state.scene,
    `navigation-route:${performance.now()}`,
    color,
    { emissive: .42, alpha: .8 }
  );
  for (const [index, point] of points.slice(1).entries()) {
    const marker = meshCylinder(state.scene, `navigation-step:${index}`, {
      diameter: .26,
      height: .045,
      tessellation: 12
    }, [point.x, .29, point.z], routeMaterial);
    marker.isPickable = false;
    state.navigationMarkers.push(marker);
  }
  state.navigationRouteMaterial = routeMaterial;
}

function clearNavigationRoute() {
  state.navigationMarkers.forEach((marker) => marker.dispose());
  state.navigationMarkers = [];
  state.navigationRouteMaterial?.dispose();
  state.navigationRouteMaterial = null;
}

function interactionRoute(source, target) {
  const excluded = new Set([source.id, target.id]);
  const candidates = [
    { gridX: target.grid.gridX + 1, gridZ: target.grid.gridZ },
    { gridX: target.grid.gridX - 1, gridZ: target.grid.gridZ },
    { gridX: target.grid.gridX, gridZ: target.grid.gridZ + 1 },
    { gridX: target.grid.gridX, gridZ: target.grid.gridZ - 1 }
  ];
  let best = [];
  for (const candidate of candidates) {
    const route = findGridPath(source.grid, candidate, excluded);
    if (route.length && (!best.length || route.length < best.length)) best = route;
  }
  return best;
}

function availableHomeCell(department, index, name, occupied) {
  const zone = DEPARTMENTS[department];
  if (!Number.isInteger(zone?.grid_x)) {
    const fallback = worldToCell(zone?.x || 0, zone?.z || 0);
    return fallback;
  }
  const total = zone.width * zone.depth;
  const start = (index + hash(name)) % total;
  for (let offset = 0; offset < total; offset += 1) {
    const slot = (start + offset) % total;
    const cell = {
      gridX: zone.grid_x + slot % zone.width,
      gridZ: zone.grid_z + Math.floor(slot / zone.width)
    };
    if (walkableOfficeCell(cell.gridX, cell.gridZ)
      && !occupied.has(cellKey(cell.gridX, cell.gridZ))) return cell;
  }
  return { gridX: zone.grid_x, gridZ: zone.grid_z };
}

function employeeStatus(assignment) {
  if (!assignment) return { key: "idle", label: t("idle") };
  const { task, run, subtask } = assignment;
  const detail = subtask?.key || shortId(task.id);
  if (["FAILED", "CANCELLED"].includes(run.status) || ["FAILED", "CANCELLED"].includes(task.status)) {
    return { key: "blocked", label: `BLOCKED · ${detail}` };
  }
  if (["PAUSED", "PAUSE_REQUESTED"].includes(run.status) || ["PAUSED", "WAITING_APPROVAL"].includes(task.status)) {
    return { key: "waiting", label: `WAITING · ${detail}` };
  }
  if (ACTIVE_RUNS.has(run.status)) return { key: "working", label: `WORKING · ${detail}` };
  if (run.status === "SUCCEEDED") return { key: "complete", label: t("complete") };
  return { key: "idle", label: t("idle") };
}

function employeeBehavior(employee) {
  if (employee.status.key === "blocked") {
    return { key: "blocked", label: t("needsIntervention"), destination: "home" };
  }
  if (employee.status.key === "waiting") {
    const approval = employee.assignment?.task.status === "WAITING_APPROVAL";
    return approval
      ? { key: "review", label: t("awaitingApproval"), destination: "operations" }
      : { key: "paused", label: t("executionPaused"), destination: "home" };
  }
  if (employee.status.key === "working") {
    return { key: "focused", label: t("workingAtStation"), destination: "home" };
  }
  if (employee.status.key === "complete") {
    return { key: "available", label: t("availableAfterDelivery"), destination: "department" };
  }
  return { key: "available", label: t("available"), destination: "department" };
}

function createScene() {
  const canvas = $("world-canvas");
  try {
    const engine = new BABYLON.Engine(canvas, true, {
      preserveDrawingBuffer: false,
      stencil: true,
      disableWebGL2Support: false,
      powerPreference: "high-performance"
    }, true);
    state.engine = engine;
    setHighDpi();
    const scene = new BABYLON.Scene(engine);
    state.scene = scene;
    scene.clearColor = new BABYLON.Color4(0.64, 0.75, 0.8, 1);
    scene.imageProcessingConfiguration.contrast = 1.08;
    scene.imageProcessingConfiguration.saturation = .9;
    const cameraTarget = new BABYLON.Vector3(0, 0, 0);
    state.cameraTarget = cameraTarget;
    const camera = new BABYLON.ArcRotateCamera(
      "office-camera", -Math.PI / 4, Math.PI / 3.1, 82, cameraTarget, scene
    );
    camera.mode = BABYLON.Camera.ORTHOGRAPHIC_CAMERA;
    camera.minZ = .1;
    camera.maxZ = 240;
    camera.inputs.clear();
    state.camera = camera;
    applyOrthographicCamera();
    const ambient = new BABYLON.HemisphericLight("sky", new BABYLON.Vector3(0.2, 1, -0.2), scene);
    ambient.intensity = .92;
    ambient.diffuse = new BABYLON.Color3(0.9, 0.94, 0.96);
    ambient.groundColor = new BABYLON.Color3(0.28, 0.34, 0.37);
    const sun = new BABYLON.DirectionalLight("sun", new BABYLON.Vector3(-0.55, -1, 0.4), scene);
    sun.position = new BABYLON.Vector3(12, 24, -16);
    sun.intensity = 1.05;
    createCampus(scene);
    const shadows = new BABYLON.ShadowGenerator(1024, sun);
    shadows.useBlurExponentialShadowMap = true;
    shadows.blurKernel = 12;
    shadows.bias = .0008;
    state.shadowGenerator = shadows;
    scene.meshes.forEach((mesh) => {
      mesh.receiveShadows = true;
      if (mesh.position.y > .18 && !mesh.name.includes("floor") && !mesh.name.includes("path")) {
        shadows.addShadowCaster(mesh);
      }
    });
    configureInput(canvas);
    scene.onPointerObservable.add((event) => {
      if (event.type !== BABYLON.PointerEventTypes.POINTERPICK) return;
      const employeeId = event.pickInfo?.pickedMesh?.metadata?.employeeId;
      if (employeeId) selectEmployee(employeeId, false);
    });
    engine.runRenderLoop(() => {
      updateCamera();
      updateMovement();
      updateOfficeActivity();
      updateInteractionEffects();
      scene.render();
      if (state.labelsDirty) {
        updateLabels();
        state.labelsDirty = false;
      }
      samplePerformance();
    });
    window.addEventListener("resize", () => {
      engine.resize();
      applyOrthographicCamera();
      state.labelsDirty = true;
    });
  } catch (error) {
    console.error("AgentMesh Office 2.5D failed:", error);
    $("world-fallback").classList.remove("hidden");
  }
}

function setHighDpi() {
  if (!state.engine) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 1.75);
  state.engine.setHardwareScalingLevel(state.quality === "eco" ? 1.5 : 1 / ratio);
}

function applyOrthographicCamera() {
  if (!state.camera || !state.engine) return;
  const aspect = Math.max(1, state.engine.getRenderWidth() / Math.max(1, state.engine.getRenderHeight()));
  state.camera.orthoTop = state.orthoSize;
  state.camera.orthoBottom = -state.orthoSize;
  state.camera.orthoLeft = -state.orthoSize * aspect;
  state.camera.orthoRight = state.orthoSize * aspect;
  $("zoom-value").textContent = `${Math.round(1200 / state.orthoSize)}%`;
}

function material(scene, name, color, { emissive = 0, alpha = 1 } = {}) {
  const result = new BABYLON.StandardMaterial(name, scene);
  result.diffuseColor = hexColor(color);
  result.specularColor = new BABYLON.Color3(0.055, 0.065, 0.075);
  result.specularPower = 48;
  result.alpha = alpha;
  if (emissive) result.emissiveColor = hexColor(color).scale(emissive);
  return result;
}

function meshBox(scene, name, size, position, meshMaterial, rotation = null) {
  const mesh = BABYLON.MeshBuilder.CreateBox(name, size, scene);
  mesh.position.set(...position);
  if (rotation) mesh.rotation.set(...rotation);
  mesh.material = meshMaterial;
  return mesh;
}

function meshCylinder(scene, name, size, position, meshMaterial, rotation = null) {
  const mesh = BABYLON.MeshBuilder.CreateCylinder(name, size, scene);
  mesh.position.set(...position);
  if (rotation) mesh.rotation.set(...rotation);
  mesh.material = meshMaterial;
  return mesh;
}

function animateDetail(scene, callback) {
  scene.onBeforeRenderObservable.add(() => {
    if (state.quality !== "eco") callback(scene.getEngine().getDeltaTime(), performance.now());
  });
}

function campusBounds() {
  const zones = Object.values(DEPARTMENTS);
  const minX = Math.min(...zones.map((zone) => zone.x)) - 8.8;
  const maxX = Math.max(...zones.map((zone) => zone.x)) + 8.8;
  const minZ = Math.min(...zones.map((zone) => zone.z)) - 6.2;
  const maxZ = Math.max(...zones.map((zone) => zone.z)) + 6.2;
  return {
    minX, maxX, minZ, maxZ,
    width: maxX - minX,
    depth: maxZ - minZ,
    centerX: (minX + maxX) / 2,
    centerZ: (minZ + maxZ) / 2
  };
}

function createCampus(scene) {
  const bounds = campusBounds();
  state.campusBounds = bounds;
  const grass = material(scene, "campus-grass", "#7f998b");
  const base = BABYLON.MeshBuilder.CreateBox("campus-base", { width: bounds.width, depth: bounds.depth, height: 0.7 }, scene);
  base.position.set(bounds.centerX, -.42, bounds.centerZ);
  base.material = grass;
  const lowerBase = meshBox(
    scene, "campus-foundation", { width: bounds.width + 1.2, depth: bounds.depth + 1.2, height: .55 },
    [bounds.centerX, -.9, bounds.centerZ], material(scene, "campus-foundation-material", "#2d4654")
  );
  lowerBase.receiveShadows = true;
  const border = material(scene, "campus-border", "#405966");
  for (const [x, z, width, depth] of [
    [bounds.centerX, bounds.minZ, bounds.width, .55], [bounds.centerX, bounds.maxZ, bounds.width, .55],
    [bounds.minX, bounds.centerZ, .55, bounds.depth], [bounds.maxX, bounds.centerZ, .55, bounds.depth]
  ]) {
    const wall = BABYLON.MeshBuilder.CreateBox("campus-wall", { width, depth, height: 1.2 }, scene);
    wall.position.set(x, 0.15, z);
    wall.material = border;
  }
  createPaths(scene);
  for (const [department, zone] of Object.entries(DEPARTMENTS)) createDepartment(scene, department, zone);
  createHub(scene);
  createCampusAmenities(scene);
  createDropIndicator(scene);
}

function createDropIndicator(scene) {
  const valid = material(scene, "office-drop-valid", "#77d7a2", { emissive: .32, alpha: .72 });
  const invalid = material(scene, "office-drop-invalid", "#d87575", { emissive: .28, alpha: .72 });
  const mesh = BABYLON.MeshBuilder.CreateBox("office-drop-cell", {
    width: officeGrid().cell_size * .9,
    depth: officeGrid().cell_size * .9,
    height: .12
  }, scene);
  mesh.position.y = .31;
  mesh.material = valid;
  mesh.isPickable = false;
  mesh.setEnabled(false);
  state.dropIndicator = { mesh, valid, invalid };
}

function createDepartment(scene, department, zone) {
  const plateMaterial = material(scene, `${department}-floor`, zone.floor);
  const plate = BABYLON.MeshBuilder.CreateBox(`${department}-plate`, {
    width: 15.6, depth: 10.2, height: 0.38
  }, scene);
  plate.position.set(zone.x, -0.06, zone.z);
  plate.material = plateMaterial;
  plate.metadata = { zone: department };
  const trim = material(scene, `${department}-trim`, zone.color, { emissive: 0.08 });
  for (const [dx, dz, width, depth] of [[0, -4.95, 15.6, .22], [0, 4.95, 15.6, .22], [-7.7, 0, .22, 10], [7.7, 0, .22, 10]]) {
    const line = BABYLON.MeshBuilder.CreateBox(`${department}-trim`, { width, depth, height: .16 }, scene);
    line.position.set(zone.x + dx, .2, zone.z + dz);
    line.material = trim;
  }
  createDepartmentArchitecture(scene, department, zone, trim);
  const creators = {
    research: createResearchLab,
    analysis: createAnalysisStudio,
    engineering: createEngineeringBay,
    operations: createReviewCourt,
    product: createProductArena,
    design: createDesignAtelier,
    security: createSecurityCenter,
    commons: createPeopleCommons
  };
  (creators[zone.style] || createFlexibleSpace)(scene, zone, trim);
  const label = document.createElement("div");
  label.className = `department-label ${department}`;
  label.style.setProperty("--department-color", zone.color);
  label.innerHTML = `<span>${department.slice(0, 3).toUpperCase()}</span><strong>${escapeHtml(departmentName(department))}</strong><small>DEPARTMENT</small>`;
  $("agent-labels").append(label);
  state.departmentLabels.set(department, {
    element: label,
    point: new BABYLON.Vector3(zone.x, 1.05, zone.z - 4.3)
  });
}

function createDepartmentArchitecture(scene, department, zone, accent) {
  const wallMaterial = material(scene, `${department}-wall-material`, "#d5d7d3");
  const frameMaterial = material(scene, `${department}-frame-material`, "#344956");
  const glassMaterial = material(scene, `${department}-window-material`, "#a9c4cc", { emissive: .04, alpha: .78 });
  const insetMaterial = material(scene, `${department}-inset-material`, shadeHex(zone.color, .78));
  meshBox(scene, `${department}-back-wall`, { width: 14.5, depth: .3, height: 1.25 }, [zone.x, .78, zone.z + 4.55], wallMaterial);
  for (const side of [-1, 1]) {
    meshBox(scene, `${department}-side-wall`, { width: .3, depth: 3.2, height: .85 }, [zone.x + side * 7.25, .58, zone.z + 3], wallMaterial);
  }
  for (let index = 0; index < 5; index += 1) {
    const x = zone.x - 5.4 + index * 2.7;
    meshBox(scene, `${department}-window-frame`, { width: 2.15, depth: .12, height: .82 }, [x, 1.25, zone.z + 4.35], frameMaterial);
    meshBox(scene, `${department}-window-glass`, { width: 1.82, depth: .14, height: .58 }, [x, 1.28, zone.z + 4.26], glassMaterial);
  }
  for (const x of [-6, -4, -2, 0, 2, 4, 6]) {
    meshBox(scene, `${department}-floor-seam`, { width: .04, depth: 8.9, height: .025 }, [zone.x + x, .155, zone.z], insetMaterial);
  }
  for (const z of [-3, -1, 1, 3]) {
    meshBox(scene, `${department}-floor-seam`, { width: 14.4, depth: .04, height: .025 }, [zone.x, .155, zone.z + z], insetMaterial);
  }
  const threshold = meshBox(scene, `${department}-entry-threshold`, { width: 3.2, depth: .55, height: .08 }, [zone.x, .25, zone.z - 4.65], accent);
  threshold.metadata = { zone: department };
  for (const side of [-1, 1]) {
    meshBox(scene, `${department}-entry-post`, { width: .32, depth: .32, height: 1.65 }, [zone.x + side * 1.55, 1, zone.z - 4.55], frameMaterial);
    const light = BABYLON.MeshBuilder.CreateSphere(`${department}-entry-light`, { diameter: .28, segments: 8 }, scene);
    light.position.set(zone.x + side * 1.55, 1.9, zone.z - 4.55);
    light.material = accent;
  }
}

function createWorkstation(scene, name, x, z, accent, angle = 0) {
  const shell = material(scene, `${name}-shell-material`, "#c9cec9");
  const legs = material(scene, `${name}-leg-material`, "#44545a");
  const desk = meshBox(scene, `${name}-desk`, { width: 2.25, depth: 1.02, height: .68 }, [x, .55, z], shell);
  desk.rotation.y = angle;
  for (const side of [-1, 1]) {
    const leg = meshBox(scene, `${name}-leg`, { width: .16, depth: .72, height: .58 }, [x + side * .85, .31, z], legs);
    leg.rotation.y = angle;
  }
  const screen = meshBox(scene, `${name}-screen`, { width: .92, depth: .12, height: .62 }, [x, 1.25, z - .28], accent, [-.12, angle, 0]);
  screen.rotation.y = angle;
  const stand = meshBox(scene, `${name}-screen-stand`, { width: .12, depth: .12, height: .35 }, [x, .98, z - .22], legs);
  stand.rotation.y = angle;
  meshBox(scene, `${name}-keyboard`, { width: .72, depth: .32, height: .06 }, [x, .93, z - .05], legs, [0, angle, 0]);
  const chairSeat = meshBox(scene, `${name}-chair-seat`, { width: .68, depth: .68, height: .15 }, [x, .52, z + .82], legs, [0, angle, 0]);
  chairSeat.rotation.y = angle;
  const chairBack = meshBox(scene, `${name}-chair-back`, { width: .68, depth: .14, height: .78 }, [x, .9, z + 1.1], legs, [-.08, angle, 0]);
  chairBack.rotation.y = angle;
  meshCylinder(scene, `${name}-chair-post`, { diameter: .12, height: .48, tessellation: 8 }, [x, .27, z + .82], legs);
  return desk;
}

function createResearchLab(scene, zone, accent) {
  const dark = material(scene, "research-structure", "#173f5c");
  const glass = material(scene, "research-glass", zone.accent, { emissive: .07, alpha: .76 });
  const observatory = meshCylinder(
    scene, "research-observatory", { diameter: 4.6, height: 1.25, tessellation: 24 },
    [zone.x - 4.65, .82, zone.z - 2.35], dark
  );
  const dome = BABYLON.MeshBuilder.CreateSphere("research-dome", { diameter: 3.65, segments: 16 }, scene);
  dome.position.set(observatory.position.x, 2.4, observatory.position.z);
  dome.scaling.y = .58;
  dome.material = glass;
  const telescope = meshCylinder(
    scene, "research-telescope", { diameter: .62, height: 3.1, tessellation: 12 },
    [zone.x - 4.35, 3.05, zone.z - 2.15], accent, [0, 0, Math.PI / 2.7]
  );
  const scanner = BABYLON.MeshBuilder.CreateTorus("research-scanner", {
    diameter: 4.25, thickness: .11, tessellation: 48
  }, scene);
  scanner.position.set(zone.x - 4.65, 2.48, zone.z - 2.35);
  scanner.rotation.x = Math.PI / 2;
  scanner.material = accent;
  for (const [index, dz] of [-1.7, 0, 1.7].entries()) {
    const pod = meshCylinder(
      scene, `research-sample-pod-${index}`, { diameter: 1.15, height: 1.55, tessellation: 16 },
      [zone.x + 5.2, 1, zone.z + dz], glass
    );
    meshCylinder(
      scene, `research-pod-cap-${index}`, { diameter: 1.35, height: .22, tessellation: 16 },
      [pod.position.x, 1.82, pod.position.z], dark
    );
  }
  createWorkstation(scene, "research-station-a", zone.x - .8, zone.z + 2.8, accent);
  createWorkstation(scene, "research-station-b", zone.x + 2.1, zone.z + 2.8, accent);
  animateDetail(scene, (delta) => {
    scanner.rotation.z += delta * .00045;
    telescope.rotation.y += delta * .00008;
  });
}

function createAnalysisStudio(scene, zone, accent) {
  const dark = material(scene, "analysis-structure", "#29234f");
  const glass = material(scene, "analysis-glass", zone.accent, { emissive: .08, alpha: .8 });
  const tower = meshBox(
    scene, "analysis-data-tower", { width: 3.7, depth: 3.5, height: 2.7 },
    [zone.x + 4.85, 1.55, zone.z - 2.5], dark, [0, -.12, 0]
  );
  for (let level = 0; level < 3; level += 1) {
    const band = BABYLON.MeshBuilder.CreateTorus(`analysis-tower-band-${level}`, {
      diameter: 3.25 - level * .35, thickness: .12, tessellation: 4
    }, scene);
    band.position.set(tower.position.x, 1.05 + level * .82, tower.position.z);
    band.rotation.x = Math.PI / 2;
    band.rotation.y = Math.PI / 4;
    band.material = accent;
  }
  const bars = [];
  [1.15, 2.1, 3.25, 1.7, 2.65].forEach((height, index) => {
    const bar = meshBox(
      scene, `analysis-bar-${index}`, { width: .72, depth: .72, height },
      [zone.x - 5.5 + index * 1.15, .32 + height / 2, zone.z - 2.7], glass
    );
    bars.push({ bar, height, phase: index * .7 });
  });
  const table = meshCylinder(
    scene, "analysis-roundtable", { diameter: 4.8, height: .58, tessellation: 24 },
    [zone.x + .7, .52, zone.z + 2.45], dark
  );
  const tableDisplay = BABYLON.MeshBuilder.CreatePolyhedron("analysis-table-projection", { type: 1, size: .88 }, scene);
  tableDisplay.position.set(table.position.x, 1.65, table.position.z);
  tableDisplay.material = glass;
  createWorkstation(scene, "analysis-station-a", zone.x - 3.4, zone.z + 2.7, accent);
  animateDetail(scene, (_delta, now) => {
    tableDisplay.rotation.y = now * .0007;
    bars.forEach(({ bar, height, phase }) => {
      const scale = .88 + Math.sin(now * .0015 + phase) * .12;
      bar.scaling.y = scale;
      bar.position.y = .32 + height * scale / 2;
    });
  });
}

function createEngineeringBay(scene, zone, accent) {
  const dark = material(scene, "engineering-structure", "#1f4850");
  const metal = material(scene, "engineering-metal", "#9fb9b7");
  const hazard = material(scene, "engineering-hazard", "#c4a96c", { emissive: .03 });
  const workshop = meshBox(
    scene, "engineering-workshop", { width: 6.2, depth: 3, height: 2.4 },
    [zone.x - 3.5, 1.38, zone.z + 3.05], dark
  );
  for (const dx of [-2.1, 0, 2.1]) {
    const roof = meshCylinder(
      scene, "engineering-saw-roof", { diameter: 1.8, height: 2.05, tessellation: 3 },
      [workshop.position.x + dx, 2.95, workshop.position.z], metal, [Math.PI / 2, 0, Math.PI / 2]
    );
    roof.scaling.z = 1.55;
  }
  const conveyor = meshBox(
    scene, "engineering-conveyor", { width: 7.4, depth: 1.3, height: .42 },
    [zone.x + 2.8, .48, zone.z - 3.15], metal
  );
  for (let index = 0; index < 8; index += 1) {
    meshCylinder(
      scene, `engineering-roller-${index}`, { diameter: .34, height: 1.18, tessellation: 12 },
      [conveyor.position.x - 3.25 + index * .92, .74, conveyor.position.z], dark,
      [Math.PI / 2, 0, 0]
    );
  }
  const robotRoots = [];
  for (const [index, x] of [zone.x + .25, zone.x + 5.4].entries()) {
    const root = new BABYLON.TransformNode(`engineering-robot-${index}`, scene);
    root.position.set(x, .3, zone.z + .4);
    meshCylinder(scene, `engineering-robot-base-${index}`, { diameter: 1.35, height: .5, tessellation: 12 }, [0, .25, 0], dark).parent = root;
    const lower = meshBox(scene, `engineering-robot-lower-${index}`, { width: .55, depth: .55, height: 2.25 }, [0, 1.35, 0], hazard, [0, 0, -.42]);
    lower.parent = root;
    const upper = meshBox(scene, `engineering-robot-upper-${index}`, { width: 1.9, depth: .48, height: .48 }, [.65, 2.35, 0], accent, [0, 0, .28]);
    upper.parent = root;
    robotRoots.push({ root, phase: index * Math.PI });
  }
  createWorkstation(scene, "engineering-station-a", zone.x - 1.2, zone.z - 1.55, accent);
  animateDetail(scene, (_delta, now) => {
    robotRoots.forEach(({ root, phase }) => { root.rotation.y = Math.sin(now * .001 + phase) * .45; });
  });
}

function createReviewCourt(scene, zone, accent) {
  const dark = material(scene, "operations-structure", "#4b3b2c");
  const marble = material(scene, "operations-marble", "#f5e8c6");
  const display = material(scene, "operations-display", zone.accent, { emissive: .08 });
  for (let tier = 0; tier < 3; tier += 1) {
    meshBox(
      scene, `operations-court-tier-${tier}`,
      { width: 8.4 - tier * 1.4, depth: 1.45, height: .35 + tier * .25 },
      [zone.x - 1.4, .38 + tier * .12, zone.z + 3.5 - tier * 1.2], tier === 2 ? marble : dark
    );
    const seatCount = 4 - tier;
    for (let seat = 0; seat < seatCount; seat += 1) {
      meshBox(
        scene, `operations-seat-${tier}-${seat}`, { width: .75, depth: .72, height: .82 },
        [zone.x - 1.4 + (seat - (seatCount - 1) / 2) * 1.45, .88 + tier * .23, zone.z + 3.35 - tier * 1.2],
        display
      );
    }
  }
  const dais = meshBox(
    scene, "operations-review-dais", { width: 5.4, depth: 2.3, height: 1.15 },
    [zone.x + 4.5, .78, zone.z - 2.8], marble
  );
  for (const dx of [-2.25, 2.25]) {
    meshCylinder(
      scene, "operations-column", { diameter: .62, height: 3.2, tessellation: 12 },
      [dais.position.x + dx, 1.85, dais.position.z], dark
    );
  }
  const verdict = BABYLON.MeshBuilder.CreatePolyhedron("operations-verdict", { type: 1, size: .95 }, scene);
  verdict.position.set(dais.position.x, 2.45, dais.position.z);
  verdict.material = display;
  const board = meshBox(
    scene, "operations-command-board", { width: 4.2, depth: .24, height: 2.2 },
    [zone.x + 5.25, 1.72, zone.z + .55], dark, [0, Math.PI / 2, 0]
  );
  for (let index = 0; index < 3; index += 1) {
    meshBox(
      scene, `operations-board-line-${index}`, { width: 1.6 - index * .22, depth: .27, height: .17 },
      [board.position.x - .14, 2.2 - index * .48, board.position.z], display, [0, Math.PI / 2, 0]
    );
  }
  animateDetail(scene, (delta, now) => {
    verdict.rotation.y += delta * .00045;
    verdict.position.y = 2.45 + Math.sin(now * .0022) * .1;
  });
}

function createProductArena(scene, zone, accent) {
  const dark = material(scene, "product-structure", "#542c3a");
  const light = material(scene, "product-display", zone.accent, { emissive: .08 });
  const roadmap = [];
  for (let index = 0; index < 5; index += 1) {
    const step = meshBox(
      scene, `product-roadmap-${index}`, { width: 2.1, depth: 1.55, height: .35 + index * .22 },
      [zone.x - 4.8 + index * 2.35, .35 + index * .11, zone.z + 2.9], index % 2 ? accent : dark
    );
    roadmap.push(step);
  }
  const warTable = meshCylinder(
    scene, "product-war-table", { diameter: 5.2, height: .62, tessellation: 8 },
    [zone.x, .55, zone.z - .5], dark
  );
  const beacon = BABYLON.MeshBuilder.CreatePolyhedron("product-beacon", { type: 1, size: 1 }, scene);
  beacon.position.set(warTable.position.x, 1.85, warTable.position.z);
  beacon.material = light;
  meshBox(scene, "product-roadmap-wall", { width: 5.5, depth: .35, height: 2.7 }, [zone.x - 4.5, 1.55, zone.z - 3.4], dark);
  animateDetail(scene, (delta, now) => {
    beacon.rotation.y += delta * .0006;
    beacon.scaling.setAll(.92 + Math.sin(now * .002) * .08);
  });
}

function createDesignAtelier(scene, zone, accent) {
  const dark = material(scene, "design-structure", "#512e50");
  const light = material(scene, "design-light", zone.accent, { emissive: .07, alpha: .86 });
  for (const [index, x] of [-4.8, -1.6, 1.6, 4.8].entries()) {
    const arch = BABYLON.MeshBuilder.CreateTorus(`design-gallery-arch-${index}`, {
      diameter: 2.5, thickness: .22, tessellation: 28
    }, scene);
    arch.position.set(zone.x + x, 1.65, zone.z - 3.1);
    arch.rotation.x = Math.PI / 2;
    arch.material = index % 2 ? accent : light;
  }
  for (const [index, x, z] of [[0, -3.4, 1.8], [1, 0, 2.5], [2, 3.4, 1.8]]) {
    meshBox(scene, `design-table-${index}`, { width: 2.7, depth: 1.6, height: .72 }, [zone.x + x, .58, zone.z + z], dark, [0, index === 1 ? 0 : x * .03, 0]);
    const model = BABYLON.MeshBuilder.CreatePolyhedron(`design-model-${index}`, { type: index % 2, size: .65 }, scene);
    model.position.set(zone.x + x, 1.45, zone.z + z);
    model.material = light;
  }
  const palette = BABYLON.MeshBuilder.CreateTorus("design-palette", { diameter: 4.5, thickness: .24, tessellation: 32 }, scene);
  palette.position.set(zone.x + 4.8, .45, zone.z - .2);
  palette.rotation.x = Math.PI / 2;
  palette.material = accent;
  animateDetail(scene, (delta) => { palette.rotation.z += delta * .00022; });
}

function createSecurityCenter(scene, zone, accent) {
  const dark = material(scene, "security-structure", "#18354f");
  const scan = material(scene, "security-scan", zone.accent, { emissive: .12, alpha: .8 });
  const vault = meshBox(scene, "security-vault", { width: 6.4, depth: 4.2, height: 3.3 }, [zone.x + 2.7, 1.75, zone.z - 1.8], dark);
  for (const dx of [-2.7, 2.7]) {
    meshCylinder(scene, "security-vault-tower", { diameter: 1.4, height: 4.1, tessellation: 8 }, [vault.position.x + dx, 2.1, vault.position.z], dark);
  }
  for (let index = 0; index < 3; index += 1) {
    const gate = BABYLON.MeshBuilder.CreateTorus(`security-scan-gate-${index}`, { diameter: 2.8, thickness: .16, tessellation: 28 }, scene);
    gate.position.set(zone.x - 4.6 + index * 2.25, 1.6, zone.z + 2.3);
    gate.rotation.x = Math.PI / 2;
    gate.material = scan;
  }
  const radar = BABYLON.MeshBuilder.CreateTorus("security-radar", { diameter: 4.1, thickness: .12, tessellation: 40 }, scene);
  radar.position.set(zone.x - 3.9, .48, zone.z - 2.3);
  radar.rotation.x = Math.PI / 2;
  radar.material = scan;
  animateDetail(scene, (delta) => { radar.rotation.z += delta * .0008; });
}

function createPeopleCommons(scene, zone, accent) {
  const wood = material(scene, "commons-wood", "#805d3c");
  const leaf = material(scene, "commons-leaf", zone.accent, { emissive: .08 });
  const pavilion = meshCylinder(scene, "commons-pavilion", { diameter: 5.8, height: .65, tessellation: 12 }, [zone.x, .55, zone.z - .6], wood);
  const roof = meshCylinder(scene, "commons-pavilion-roof", { diameterTop: .8, diameterBottom: 6.5, height: 1.5, tessellation: 12 }, [zone.x, 2.4, zone.z - .6], accent);
  for (const angle of [0, Math.PI / 2, Math.PI, Math.PI * 1.5]) {
    meshCylinder(scene, "commons-column", { diameter: .32, height: 2.25, tessellation: 8 }, [zone.x + Math.cos(angle) * 2.2, 1.45, zone.z - .6 + Math.sin(angle) * 2.2], wood);
  }
  for (const [index, x, z] of [[0, -4.8, 2.7], [1, -2.6, 3.4], [2, 2.8, 3.3], [3, 5, 2.5]]) {
    meshCylinder(scene, `commons-planter-${index}`, { diameter: 1.15, height: .55, tessellation: 12 }, [zone.x + x, .47, zone.z + z], wood);
    const plant = BABYLON.MeshBuilder.CreatePolyhedron(`commons-plant-${index}`, { type: 1, size: .85 }, scene);
    plant.position.set(zone.x + x, 1.25, zone.z + z);
    plant.material = leaf;
  }
  pavilion.metadata = { sharedSpace: true };
  roof.metadata = { sharedSpace: true };
}

function createFlexibleSpace(scene, zone, accent) {
  createPeopleCommons(scene, zone, accent);
}

function createHub(scene) {
  const hubMaterial = material(scene, "hub", "#557887");
  const hub = BABYLON.MeshBuilder.CreateCylinder("handoff-hub", {
    diameter: 5.2, height: .5, tessellation: 32
  }, scene);
  hub.position.y = .05;
  hub.material = hubMaterial;
  const ringMaterial = material(scene, "hub-ring", "#8dbdc5", { emissive: 0.22 });
  for (const diameter of [2.2, 3.7, 5]) {
    const ring = BABYLON.MeshBuilder.CreateTorus("hub-ring", {
      diameter, thickness: .1, tessellation: 48
    }, scene);
    ring.position.y = .36;
    ring.material = ringMaterial;
  }
  const core = BABYLON.MeshBuilder.CreatePolyhedron("hub-core", { type: 1, size: 1.05 }, scene);
  core.position.y = 1.5;
  core.material = material(scene, "hub-core-material", "#9387ad", { emissive: .18 });
  scene.onBeforeRenderObservable.add(() => {
    if (state.quality === "eco") return;
    core.rotation.y += scene.getEngine().getDeltaTime() * .0007;
    core.position.y = 1.5 + Math.sin(performance.now() * .002) * .12;
  });
}

function createPaths(scene) {
  const bounds = state.campusBounds || campusBounds();
  const pathMaterial = material(scene, "path", "#d3d8d2");
  const pathEdge = material(scene, "path-edge", "#8eb2b6", { emissive: .04 });
  const rows = [...new Set(Object.values(DEPARTMENTS).map((zone) => zone.z))];
  const columns = [...new Set(Object.values(DEPARTMENTS).map((zone) => zone.x))];
  for (const z of rows) {
    meshBox(scene, "campus-path-row", { width: bounds.width - 4, depth: 1.5, height: .15 }, [bounds.centerX, .18, z], pathMaterial);
  }
  for (const x of columns) {
    meshBox(scene, "campus-path-column", { width: 1.5, depth: bounds.depth - 3, height: .15 }, [x, .18, bounds.centerZ], pathMaterial);
  }
  meshBox(scene, "campus-main-boulevard", { width: bounds.width - 3, depth: 2.25, height: .16 }, [bounds.centerX, .2, 0], pathMaterial);
  for (const [x, z, width, depth] of [
    [bounds.centerX, -1.15, bounds.width - 3, .12], [bounds.centerX, 1.15, bounds.width - 3, .12]
  ]) {
    meshBox(scene, "campus-path-edge", { width, depth, height: .08 }, [x, .29, z], pathEdge);
  }
  for (const zone of Object.values(DEPARTMENTS)) {
    for (let step = 0; step < 4; step += 1) {
      const x = zone.x * (.35 + step * .12);
      const z = zone.z * (.35 + step * .12);
      meshBox(scene, "campus-route-light", { width: .32, depth: .32, height: .12 }, [x, .34, z], pathEdge);
    }
  }
}

function createCampusAmenities(scene) {
  const trunk = material(scene, "tree-trunk", "#6f5034");
  const foliage = material(scene, "tree-foliage", "#6d967b");
  const lamp = material(scene, "campus-lamp", "#425760");
  const light = material(scene, "campus-lamp-light", "#d9e8e7", { emissive: .35 });
  for (const [index, x, z] of [
    [0, -17.8, -11.5], [1, -17.8, 11.4], [2, 17.8, -11.5], [3, 17.8, 11.4],
    [4, -2.8, -11.6], [5, 2.8, -11.6], [6, -2.8, 11.6], [7, 2.8, 11.6]
  ]) {
    meshCylinder(scene, `campus-tree-trunk-${index}`, { diameter: .42, height: 1.65, tessellation: 8 }, [x, .92, z], trunk);
    const crown = BABYLON.MeshBuilder.CreatePolyhedron(`campus-tree-${index}`, { type: 1, size: 1.25 }, scene);
    crown.position.set(x, 2.15, z);
    crown.material = foliage;
    state.ambientMeshes.push({ mesh: crown, phase: index * .83, kind: "foliage" });
  }
  for (const [index, x, z] of [[0, -5.4, -1.8], [1, 5.4, -1.8], [2, -5.4, 1.8], [3, 5.4, 1.8]]) {
    meshCylinder(scene, `campus-lamp-${index}`, { diameter: .22, height: 2.1, tessellation: 8 }, [x, 1.28, z], lamp);
    const bulb = BABYLON.MeshBuilder.CreateSphere(`campus-lamp-bulb-${index}`, { diameter: .48, segments: 8 }, scene);
    bulb.position.set(x, 2.42, z);
    bulb.material = light;
    state.ambientMeshes.push({ mesh: bulb, phase: index * 1.37, kind: "light" });
  }
}

function createEmployeeNode(employee) {
  const scene = state.scene;
  const root = new BABYLON.TransformNode(`employee:${employee.id}`, scene);
  root.position.set(employee.position.x, .35, employee.position.z);
  const identityHash = hash(employee.id);
  const shirt = material(scene, `shirt:${employee.id}`, employee.color);
  const skin = material(scene, `skin:${employee.id}`, SKIN_TONES[identityHash % SKIN_TONES.length]);
  const dark = material(scene, `dark:${employee.id}`, HAIR_TONES[(identityHash >>> 3) % HAIR_TONES.length]);
  const sole = material(scene, `sole:${employee.id}`, "#202b31");
  const white = material(scene, `white:${employee.id}`, "#dfe2dd");
  const departmentAccent = material(
    scene, `department:${employee.id}`, DEPARTMENTS[employee.department]?.accent || "#a8bdc2", { emissive: .03 }
  );
  const body = BABYLON.MeshBuilder.CreateCylinder(`body:${employee.id}`, {
    diameterTop: .62, diameterBottom: .78, height: .98, tessellation: 8
  }, scene);
  body.parent = root; body.position.y = 1.18; body.material = shirt;
  body.scaling.x = .94 + (identityHash % 7) * .018;
  const collar = meshBox(scene, `collar:${employee.id}`, { width: .34, depth: .08, height: .18 }, [0, 1.55, -.34], white);
  collar.parent = root;
  for (const side of [-1, 1]) {
    const lapel = meshBox(scene, `lapel:${employee.id}`, { width: .17, depth: .07, height: .42 }, [side * .13, 1.28, -.37], departmentAccent, [0, 0, side * .28]);
    lapel.parent = root;
  }
  const badge = meshBox(scene, `badge:${employee.id}`, { width: .18, depth: .05, height: .13 }, [.22, 1.28, -.42], departmentAccent);
  badge.parent = root;
  const neck = meshCylinder(scene, `neck:${employee.id}`, { diameter: .28, height: .2, tessellation: 8 }, [0, 1.72, 0], skin);
  neck.parent = root;
  const headPivot = new BABYLON.TransformNode(`head-pivot:${employee.id}`, scene);
  headPivot.parent = root; headPivot.position.y = 2.02;
  const head = BABYLON.MeshBuilder.CreateSphere(`head:${employee.id}`, { diameter: .68, segments: 12 }, scene);
  head.parent = headPivot; head.material = skin;
  const hair = BABYLON.MeshBuilder.CreateSphere(`hair:${employee.id}`, { diameter: .7, segments: 10 }, scene);
  hair.parent = headPivot; hair.position.set(0, .22, .01); hair.scaling.set(1, .45, 1); hair.material = dark;
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = BABYLON.MeshBuilder.CreateSphere(`eye:${employee.id}`, { diameter: .065, segments: 6 }, scene);
    eye.parent = headPivot; eye.position.set(side * .13, .02, -.325); eye.material = sole;
    eyes.push(eye);
  }
  const nose = BABYLON.MeshBuilder.CreateSphere(`nose:${employee.id}`, { diameter: .07, segments: 6 }, scene);
  nose.parent = headPivot; nose.position.set(0, -.06, -.355); nose.material = skin;
  const legs = [];
  const arms = [];
  for (const side of [-1, 1]) {
    const legPivot = new BABYLON.TransformNode(`leg-pivot:${employee.id}:${side}`, scene);
    legPivot.parent = root; legPivot.position.set(side * .2, .73, 0);
    const leg = meshCylinder(scene, `leg:${employee.id}`, { diameter: .24, height: .62, tessellation: 8 }, [0, -.31, 0], dark);
    leg.parent = legPivot;
    const shoe = meshBox(scene, `shoe:${employee.id}`, { width: .28, depth: .42, height: .16 }, [0, -.63, -.08], sole);
    shoe.parent = legPivot;
    legs.push(legPivot);
    const armPivot = new BABYLON.TransformNode(`arm-pivot:${employee.id}:${side}`, scene);
    armPivot.parent = root; armPivot.position.set(side * .48, 1.48, 0); armPivot.rotation.z = side * .12;
    const arm = meshCylinder(scene, `arm:${employee.id}`, { diameter: .2, height: .72, tessellation: 8 }, [0, -.36, 0], shirt);
    arm.parent = armPivot;
    const hand = BABYLON.MeshBuilder.CreateSphere(`hand:${employee.id}`, { diameter: .22, segments: 8 }, scene);
    hand.parent = armPivot; hand.position.set(0, -.69, 0); hand.material = skin;
    arms.push(armPivot);
  }
  const tablet = meshBox(scene, `tablet:${employee.id}`, { width: .58, depth: .08, height: .4 }, [0, 1.12, -.48], dark, [-.12, 0, 0]);
  tablet.parent = root;
  const tabletScreen = meshBox(scene, `tablet-screen:${employee.id}`, { width: .44, depth: .04, height: .28 }, [0, 1.13, -.535], departmentAccent, [-.12, 0, 0]);
  tabletScreen.parent = root;
  const base = BABYLON.MeshBuilder.CreateTorus(`base:${employee.id}`, { diameter: 1.25, thickness: .08, tessellation: 28 }, scene);
  base.parent = root; base.position.y = .08;
  base.material = material(scene, `base-mat:${employee.id}`, employee.color, { emissive: .12 });
  const shadow = meshCylinder(scene, `shadow:${employee.id}`, { diameter: .9, height: .025, tessellation: 20 }, [0, .02, .08], material(scene, `shadow-mat:${employee.id}`, "#17252b", { alpha: .22 }));
  shadow.parent = root;
  const preset = createCharacterPreset(
    scene, root, headPivot, employee, { shirt, dark, white, departmentAccent }
  );
  root.getChildMeshes().forEach((mesh) => { mesh.metadata = { employeeId: employee.id }; });
  if (state.shadowGenerator) root.getChildMeshes().forEach((mesh) => {
    if (!mesh.name.startsWith("shadow:") && !mesh.name.startsWith("base:")) state.shadowGenerator.addShadowCaster(mesh);
  });
  const label = document.createElement("button");
  label.type = "button";
  label.className = `agent-label ${employee.status.key}`;
  label.dataset.employeeId = employee.id;
  label.dataset.behavior = employeeBehavior(employee).key;
  label.title = employeeBehavior(employee).label;
  label.innerHTML = `<strong><i aria-hidden="true"></i>${escapeHtml(employee.name)}</strong><span>${escapeHtml(employee.status.label)}</span>`;
  label.addEventListener("click", () => selectEmployee(employee.id, false));
  $("agent-labels").append(label);
  const value = {
    root, label, base, body, headPivot, eyes, legs, arms, tablet, departmentAccent,
    preset, posePhase: (identityHash % 1000) / 100,
    behavior: employeeBehavior(employee),
    employee,
    walking: false, dragging: false, ambient: null,
    semanticLocation: null, semanticTarget: null,
    nextAmbientAt: performance.now() + 2500 + hash(employee.id) % 7000,
    labelPoint: new BABYLON.Vector3(), screenX: null, screenY: null, labelVisible: null
  };
  label.addEventListener("pointerdown", (event) => beginEmployeeDrag(value, event));
  label.addEventListener("pointermove", updatePointerInteraction);
  label.addEventListener("pointerup", endPointerInteraction);
  label.addEventListener("pointercancel", endPointerInteraction);
  return value;
}

function createCharacterPreset(scene, root, headPivot, employee, materials) {
  const supported = new Set(["signal", "oracle", "mech", "guardian", "spark", "companion"]);
  const requested = (employee.tags || [])
    .map((tag) => String(tag).toLowerCase())
    .find((tag) => tag.startsWith("avatar:") && supported.has(tag.slice(7)));
  const presetByDepartment = {
    research: "signal", analysis: "oracle", engineering: "mech",
    operations: "guardian", security: "guardian", product: "spark",
    design: "spark", commons: "companion"
  };
  const name = requested?.slice(7)
    || presetByDepartment[employee.department]
    || ["signal", "oracle", "mech", "spark"][hash(employee.id) % 4];
  const animated = [];
  if (name === "signal") {
    const antenna = meshCylinder(scene, `preset-signal:${employee.id}`, {
      diameter: .07, height: .48, tessellation: 8
    }, [0, .57, 0], materials.dark);
    antenna.parent = headPivot;
    const beacon = BABYLON.MeshBuilder.CreateSphere(`preset-beacon:${employee.id}`, { diameter: .17, segments: 8 }, scene);
    beacon.parent = headPivot; beacon.position.y = .84; beacon.material = materials.departmentAccent;
    animated.push({ mesh: beacon, kind: "pulse", baseY: .84 });
  } else if (name === "oracle") {
    const halo = BABYLON.MeshBuilder.CreateTorus(`preset-halo:${employee.id}`, {
      diameter: .82, thickness: .055, tessellation: 28
    }, scene);
    halo.parent = headPivot; halo.position.y = .58; halo.rotation.x = Math.PI / 2;
    halo.material = materials.departmentAccent;
    animated.push({ mesh: halo, kind: "orbit", baseY: .58 });
  } else if (name === "mech") {
    for (const side of [-1, 1]) {
      const ear = meshCylinder(scene, `preset-mech:${employee.id}`, {
        diameter: .2, height: .12, tessellation: 12
      }, [side * .38, .02, 0], materials.departmentAccent, [0, 0, Math.PI / 2]);
      ear.parent = headPivot;
    }
    const visor = meshBox(scene, `preset-visor:${employee.id}`, {
      width: .48, depth: .055, height: .1
    }, [0, .04, -.35], materials.departmentAccent);
    visor.parent = headPivot;
  } else if (name === "guardian") {
    for (const side of [-1, 1]) {
      const shoulder = BABYLON.MeshBuilder.CreateSphere(`preset-guard:${employee.id}`, {
        diameter: .42, segments: 8
      }, scene);
      shoulder.parent = root; shoulder.position.set(side * .53, 1.47, .02);
      shoulder.scaling.set(1.25, .48, .9); shoulder.material = materials.departmentAccent;
    }
  } else if (name === "companion") {
    for (const side of [-1, 1]) {
      const ear = BABYLON.MeshBuilder.CreateCylinder(`preset-ear:${employee.id}`, {
        diameterTop: 0, diameterBottom: .23, height: .38, tessellation: 4
      }, scene);
      ear.parent = headPivot; ear.position.set(side * .22, .47, 0);
      ear.rotation.z = side * -.2; ear.material = materials.dark;
    }
  } else {
    const spark = BABYLON.MeshBuilder.CreatePolyhedron(`preset-spark:${employee.id}`, {
      type: 1, size: .18
    }, scene);
    spark.parent = root; spark.position.set(.56, 2.15, 0);
    spark.material = materials.departmentAccent;
    animated.push({ mesh: spark, kind: "spark", baseY: 2.15 });
  }
  return { name, animated };
}

function syncScene() {
  if (!state.scene) return;
  const ids = new Set(state.employees.slice(0, 50).map((employee) => employee.id));
  for (const [id, value] of state.employeeNodes) {
    if (!ids.has(id)) {
      value.root.dispose();
      value.label.remove();
      state.employeeNodes.delete(id);
    }
  }
  for (const employee of state.employees.slice(0, 50)) {
    let value = state.employeeNodes.get(employee.id);
    if (!value) {
      value = createEmployeeNode(employee);
      state.employeeNodes.set(employee.id, value);
    }
    value.employee = employee;
    value.behavior = employeeBehavior(employee);
    value.label.dataset.behavior = value.behavior.key;
    value.label.title = value.behavior.label;
    value.label.className = `agent-label ${employee.status.key}${employee.id === state.selectedEmployeeId ? " selected" : ""}`;
    value.label.innerHTML = `<strong><i aria-hidden="true"></i>${escapeHtml(employee.name)}</strong><span>${escapeHtml(employee.status.label)}</span>`;
    value.root.setEnabled(true);
    if (!value.walking && !value.dragging && !value.ambient && !value.semanticLocation) {
      value.root.position.set(employee.position.x, .35, employee.position.z);
    }
  }
  state.labelsDirty = true;
}

function updateLabels() {
  if (!state.scene || !state.camera || !state.engine) return;
  const width = state.engine.getRenderWidth();
  const height = state.engine.getRenderHeight();
  const cssWidth = $("world-canvas").clientWidth;
  const cssHeight = $("world-canvas").clientHeight;
  for (const value of state.employeeNodes.values()) {
    value.labelPoint.copyFrom(value.root.position);
    value.labelPoint.y += 2.75;
    positionOverlay(value, value.labelPoint, width, height, cssWidth, cssHeight, 60, 50);
  }
  for (const value of state.departmentLabels.values()) {
    positionOverlay(value, value.point, width, height, cssWidth, cssHeight, 80, 40);
  }
}

function positionOverlay(value, point, width, height, cssWidth, cssHeight, marginX, marginY) {
  const projected = BABYLON.Vector3.Project(
    point,
    BABYLON.Matrix.Identity(),
    state.scene.getTransformMatrix(),
    state.camera.viewport.toGlobal(width, height)
  );
  const visible = projected.z > 0 && projected.z < 1
    && projected.x >= -marginX && projected.x <= width + marginX
    && projected.y >= -marginY && projected.y <= height + marginY;
  const element = value.element || value.label;
  if (value.labelVisible !== visible) {
    element.hidden = !visible;
    value.labelVisible = visible;
  }
  if (!visible) return;
  const x = projected.x / width * cssWidth;
  const y = projected.y / height * cssHeight;
  if (!Number.isFinite(value.screenX) || Math.abs(value.screenX - x) >= .2 || Math.abs(value.screenY - y) >= .2) {
    element.style.setProperty("--label-x", `${x.toFixed(2)}px`);
    element.style.setProperty("--label-y", `${y.toFixed(2)}px`);
    value.screenX = x;
    value.screenY = y;
  }
}

function configureInput(canvas) {
  canvas.addEventListener("pointerdown", (event) => {
    const employee = employeeAtPointer(event);
    if (employee) {
      beginEmployeeDrag(employee, event);
      return;
    }
    state.pointerInteraction = {
      type: "camera", pointerId: event.pointerId,
      x: event.clientX, y: event.clientY, moved: false
    };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", updatePointerInteraction);
  canvas.addEventListener("pointerup", endPointerInteraction);
  canvas.addEventListener("pointercancel", endPointerInteraction);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    changeZoom(event.deltaY > 0 ? 1.1 : .9);
  }, { passive: false });
  window.addEventListener("keydown", (event) => state.keys.add(event.code));
  window.addEventListener("keyup", (event) => state.keys.delete(event.code));
}

function canvasRenderPoint(event) {
  const canvas = $("world-canvas");
  const bounds = canvas.getBoundingClientRect();
  return {
    x: event.clientX - bounds.left,
    y: event.clientY - bounds.top
  };
}

function employeeAtPointer(event) {
  if (!state.scene || !state.engine) return null;
  const point = canvasRenderPoint(event);
  const pick = state.scene.pick(point.x, point.y, (mesh) => Boolean(mesh.metadata?.employeeId), false, state.camera);
  return pick?.hit ? state.employeeNodes.get(pick.pickedMesh.metadata.employeeId) || null : null;
}

function campusPointAtPointer(event) {
  if (!state.scene || !state.camera || !state.engine) return null;
  const point = canvasRenderPoint(event);
  const ray = state.scene.createPickingRay(point.x, point.y, BABYLON.Matrix.Identity(), state.camera);
  const distance = ray.intersectsPlane(new BABYLON.Plane(0, 1, 0, -.35));
  if (distance === null || distance < 0) return null;
  return ray.origin.add(ray.direction.scale(distance));
}

function beginEmployeeDrag(value, event) {
  event.preventDefault();
  event.stopPropagation();
  if (state.movement?.value === value) {
    state.movement = null;
    clearNavigationRoute();
  }
  value.walking = false;
  value.ambient = null;
  value.dragging = true;
  value.label.classList.add("dragging");
  const ground = campusPointAtPointer(event);
  state.pointerInteraction = {
    type: "employee", pointerId: event.pointerId, value,
    x: event.clientX, y: event.clientY, moved: false,
    original: {
      grid: { ...value.employee.grid },
      position: { ...value.employee.position },
      department: value.employee.department
    },
    candidate: { ...value.employee.grid },
    valid: true,
    offsetX: ground ? value.root.position.x - ground.x : 0,
    offsetZ: ground ? value.root.position.z - ground.z : 0
  };
  selectEmployee(value.employee.id, false);
  $("world-stage").classList.add("employee-dragging");
  event.currentTarget?.setPointerCapture?.(event.pointerId);
}

function updatePointerInteraction(event) {
  const interaction = state.pointerInteraction;
  if (!interaction || interaction.pointerId !== event.pointerId) return;
  const dx = event.clientX - interaction.x;
  const dy = event.clientY - interaction.y;
  if (Math.abs(dx) + Math.abs(dy) > 2) interaction.moved = true;
  if (interaction.type === "employee") {
    const point = campusPointAtPointer(event);
    if (point) {
      const candidate = worldToCell(point.x + interaction.offsetX, point.z + interaction.offsetZ);
      const valid = validOfficeCell(candidate.gridX, candidate.gridZ, interaction.value.employee.id);
      const world = cellToWorld(candidate.gridX, candidate.gridZ);
      interaction.candidate = candidate;
      interaction.valid = valid;
      interaction.value.root.position.set(world.x, .35, world.z);
      if (state.dropIndicator) {
        state.dropIndicator.mesh.position.set(world.x, .31, world.z);
        state.dropIndicator.mesh.material = valid ? state.dropIndicator.valid : state.dropIndicator.invalid;
        state.dropIndicator.mesh.setEnabled(true);
      }
      state.labelsDirty = true;
    }
  } else {
    panCameraFromScreen(dx, dy);
  }
  interaction.x = event.clientX;
  interaction.y = event.clientY;
}

async function endPointerInteraction(event) {
  const interaction = state.pointerInteraction;
  if (!interaction || interaction.pointerId !== event.pointerId) return;
  state.pointerInteraction = null;
  if (interaction.type === "employee") {
    const { value } = interaction;
    value.dragging = false;
    value.label.classList.remove("dragging");
    if (state.dropIndicator) state.dropIndicator.mesh.setEnabled(false);
    $("world-stage").classList.remove("employee-dragging");
    if (!interaction.valid) {
      restoreEmployeePlacement(value, interaction.original);
      toast("That grid cell is unavailable");
      return;
    }
    const world = cellToWorld(interaction.candidate.gridX, interaction.candidate.gridZ);
    value.employee.grid = { ...interaction.candidate };
    value.employee.position = world;
    try {
      const placement = await api(`/api/v1/office-layout/placements/${encodeURIComponent(value.employee.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          grid_x: interaction.candidate.gridX,
          grid_z: interaction.candidate.gridZ
        })
      });
      value.employee.department = placement.department;
      value.employee.persisted = true;
      state.placementByAgent.set(value.employee.id, placement);
      const accent = DEPARTMENTS[placement.department]?.accent || "#a8bdc2";
      value.departmentAccent.diffuseColor = hexColor(accent);
      value.departmentAccent.emissiveColor = hexColor(accent).scale(.03);
      render();
      renderInspector();
      syncScene();
      toast(`Moved to ${departmentName(placement.department)}`);
    } catch (error) {
      restoreEmployeePlacement(value, interaction.original);
      toast(error.message);
    }
  }
}

function validOfficeCell(gridX, gridZ, employeeId) {
  if (!roomForCell(gridX, gridZ) || officeObstacleCell(gridX, gridZ)) return false;
  return !state.employees.some((employee) => (
    employee.id !== employeeId
    && employee.grid?.gridX === gridX
    && employee.grid?.gridZ === gridZ
  ));
}

function restoreEmployeePlacement(value, original) {
  value.employee.grid = { ...original.grid };
  value.employee.position = { ...original.position };
  value.employee.department = original.department;
  value.root.position.set(original.position.x, .35, original.position.z);
  state.labelsDirty = true;
  render();
  renderInspector();
}

function panCameraFromScreen(dx, dy) {
  if (!state.camera || !state.cameraTarget) return;
  const scale = state.orthoSize / 430;
  const up = state.cameraTarget.subtract(state.camera.position);
  up.y = 0;
  if (up.lengthSquared() < .0001) return;
  up.normalize();
  const right = new BABYLON.Vector3(up.z, 0, -up.x);
  panCamera(
    (-dx * right.x + dy * up.x) * scale,
    (-dx * right.z + dy * up.z) * scale
  );
}

function panCamera(dx, dz) {
  if (!state.cameraTarget) return;
  const bounds = state.campusBounds || campusBounds();
  const xMargin = Math.min(bounds.width / 2, Math.max(5, state.orthoSize * .55));
  const zMargin = Math.min(bounds.depth / 2, Math.max(4, state.orthoSize * .38));
  state.cameraTarget.x = BABYLON.Scalar.Clamp(state.cameraTarget.x + dx, bounds.minX + xMargin, bounds.maxX - xMargin);
  state.cameraTarget.z = BABYLON.Scalar.Clamp(state.cameraTarget.z + dz, bounds.minZ + zMargin, bounds.maxZ - zMargin);
  state.camera.setTarget(state.cameraTarget);
  state.labelsDirty = true;
  updateLocation();
}

function updateCamera() {
  const speed = state.orthoSize * .012;
  const horizontal = (
    (state.keys.has("KeyD") || state.keys.has("ArrowRight") ? 1 : 0)
    - (state.keys.has("KeyA") || state.keys.has("ArrowLeft") ? 1 : 0)
  );
  const vertical = (
    (state.keys.has("KeyS") || state.keys.has("ArrowDown") ? 1 : 0)
    - (state.keys.has("KeyW") || state.keys.has("ArrowUp") ? 1 : 0)
  );
  if (horizontal || vertical) {
    const diagonalScale = horizontal && vertical ? Math.SQRT1_2 : 1;
    panCameraFromScreen(
      horizontal * speed * 430 / state.orthoSize * diagonalScale,
      vertical * speed * 430 / state.orthoSize * diagonalScale
    );
  }
}

function changeZoom(multiplier) {
  state.orthoSize = BABYLON.Scalar.Clamp(state.orthoSize * multiplier, 4.5, 30);
  applyOrthographicCamera();
  panCamera(0, 0);
}

function focusPoint(x, z, size = 7.5) {
  state.cameraTarget.set(x, 0, z);
  state.camera.setTarget(state.cameraTarget);
  state.orthoSize = size;
  applyOrthographicCamera();
  state.labelsDirty = true;
  updateLocation();
}

function focusZone(department) {
  const zone = DEPARTMENTS[department];
  if (zone) focusPoint(zone.x, zone.z, 6.7);
}

function focusCampus() {
  const bounds = state.campusBounds || campusBounds();
  focusPoint(bounds.centerX, bounds.centerZ, Math.min(30, Math.max(14, bounds.depth / 2 + 3)));
}

function updateLocation() {
  const target = state.cameraTarget;
  if (!target) {
    $("camera-location").textContent = t("hub").toUpperCase();
    return;
  }
  let location = "hub";
  let distance = Infinity;
  for (const [department, zone] of Object.entries(DEPARTMENTS)) {
    const candidate = Math.hypot(target.x - zone.x, target.z - zone.z);
    if (candidate < distance) { location = department; distance = candidate; }
  }
  if (Math.hypot(target.x, target.z) < 4) location = "hub";
  $("camera-location").textContent = (location === "hub" ? t("hub") : departmentName(location)).toUpperCase();
}

function selectEmployee(employeeId, focus = true) {
  state.selectedEmployeeId = employeeId;
  const value = state.employeeNodes.get(employeeId);
  if (focus && value) focusPoint(value.root.position.x, value.root.position.z, 5.8);
  renderInspector();
  syncScene();
  $("employee-select").value = employeeId;
  $("inspector").classList.add("open");
}

function animateLatestHandoff() {
  const task = selectedTask();
  const handoff = task?.handoffs?.[task.handoffs.length - 1];
  if (!handoff || state.animatedHandoffs.has(handoff.id)) return;
  const source = employeeByName(handoff.source_agent_id);
  const target = employeeByName(handoff.target_agent_id);
  const sourceNode = source && state.employeeNodes.get(source.id);
  const targetNode = target && state.employeeNodes.get(target.id);
  if (!sourceNode || !targetNode) return;
  state.animatedHandoffs.add(handoff.id);
  sourceNode.ambient = null;
  const startCell = worldToCell(sourceNode.root.position.x, sourceNode.root.position.z);
  const route = interactionRoute({ ...source, grid: startCell }, target);
  const points = route.length
    ? routeWorldPoints(route)
    : [
      sourceNode.root.position.clone(),
      new BABYLON.Vector3(0, .35, 0),
      targetNode.root.position.add(new BABYLON.Vector3(-1.1, 0, .25))
    ];
  sourceNode.walking = true;
  showNavigationRoute(points, "handoff");
  state.movement = {
    value: sourceNode,
    points,
    index: Math.min(1, points.length),
    holdUntil: 0,
    returning: false,
    homeCell: { ...source.grid },
    purpose: "handoff"
  };
}

function renderInteractionFeed() {
  const supported = new Set(["MCP", "A2A", "POLICY"]);
  const events = state.interactions.filter((event) => supported.has(event.transport)).slice(0, 4);
  $("interaction-feed").innerHTML = events.map((event) => {
    const source = event.source?.label || event.source?.type || "AgentMesh";
    const target = event.target?.label || event.target?.type || "AgentMesh";
    return `<article class="interaction-event" data-transport="${escapeHtml(event.transport)}"><strong title="${escapeHtml(`${source} → ${target}`)}">${escapeHtml(source)} → ${escapeHtml(target)}</strong><span>${escapeHtml(event.transport)} · ${escapeHtml(event.status)}</span></article>`;
  }).join("");
}

function employeeForInteractionEndpoint(endpoint) {
  if (!endpoint) return null;
  const task = selectedTask();
  if (endpoint.type === "SUBTASK") {
    const subtask = task?.subtasks?.find((item) => item.id === endpoint.id || item.key === endpoint.label);
    const run = task?.runs?.find((item) => item.subtask_id === subtask?.id);
    if (run) return employeeByName(run.agent_id);
  }
  if (endpoint.label) {
    const direct = employeeByName(endpoint.label);
    if (direct) return direct;
  }
  return null;
}

function interactionStation(transport) {
  const department = transport === "MCP" ? "engineering" : transport === "A2A" ? "security" : "operations";
  const zone = DEPARTMENTS[department];
  return new BABYLON.Vector3(zone.x, 1.45, zone.z);
}

function interactionAgentPoint(event) {
  const employee = employeeForInteractionEndpoint(event.source)
    || employeeForInteractionEndpoint(event.target)
    || state.employees.find((item) => item.assignment?.task?.id === state.selectedTaskId)
    || state.employees[0];
  const node = employee && state.employeeNodes.get(employee.id);
  return node
    ? node.root.position.add(new BABYLON.Vector3(0, 1.45, 0))
    : new BABYLON.Vector3(0, 1.45, 0);
}

function animateLatestInteraction() {
  const supported = new Set(["MCP", "A2A", "POLICY"]);
  const event = state.interactions.find((item) => supported.has(item.transport));
  if (!event || state.animatedInteractions.has(event.id) || !state.scene) return;
  state.animatedInteractions.add(event.id);
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const agentPoint = interactionAgentPoint(event);
  const stationPoint = interactionStation(event.transport);
  const stationType = event.transport === "MCP" ? "TOOL" : event.transport === "A2A" ? "PEER" : "APPROVAL";
  const returning = event.source?.type === stationType;
  const start = returning ? stationPoint : agentPoint;
  const end = returning ? agentPoint : stationPoint;
  const color = event.transport === "MCP" ? "#55dbbb" : event.transport === "A2A" ? "#62b8ff" : "#e5c978";
  const effectMaterial = material(
    state.scene,
    `interaction-material:${event.id}`,
    color,
    { emissive: .8, alpha: .95 }
  );
  const packet = BABYLON.MeshBuilder.CreateSphere(
    `interaction-packet:${event.id}`,
    { diameter: .34, segments: 10 },
    state.scene
  );
  packet.position.copyFrom(start);
  packet.material = effectMaterial;
  packet.isPickable = false;
  state.interactionEffects.push({
    mesh: packet,
    material: effectMaterial,
    start,
    end,
    startedAt: performance.now(),
    duration: 1600
  });
}

function updateInteractionEffects() {
  if (!state.interactionEffects.length) return;
  const now = performance.now();
  state.interactionEffects = state.interactionEffects.filter((effect) => {
    const progress = Math.min(1, (now - effect.startedAt) / effect.duration);
    effect.mesh.position.copyFrom(BABYLON.Vector3.Lerp(effect.start, effect.end, progress));
    effect.mesh.position.y += Math.sin(progress * Math.PI) * 2.1;
    const pulse = 1 + Math.sin(progress * Math.PI * 6) * .18;
    effect.mesh.scaling.setAll(pulse);
    if (progress < 1) return true;
    effect.mesh.dispose();
    effect.material.dispose();
    return false;
  });
}

function updateMovement() {
  const movement = state.movement;
  if (!movement || !state.scene) return;
  state.labelsDirty = true;
  if (movement.holdUntil) {
    if (performance.now() < movement.holdUntil) return;
    movement.holdUntil = 0;
  }
  const target = movement.points[movement.index];
  if (!target) {
    if (!movement.returning) {
      movement.returning = true;
      const currentCell = worldToCell(
        movement.value.root.position.x,
        movement.value.root.position.z
      );
      const route = findGridPath(
        currentCell,
        movement.homeCell,
        new Set([movement.value.employee.id])
      );
      movement.points = route.length
        ? routeWorldPoints(route)
        : [
          movement.value.root.position.clone(),
          new BABYLON.Vector3(
            movement.value.employee.position.x,
            .35,
            movement.value.employee.position.z
          )
        ];
      movement.index = Math.min(1, movement.points.length);
      movement.holdUntil = performance.now() + 900;
      return;
    }
    movement.value.walking = false;
    state.movement = null;
    clearNavigationRoute();
    return;
  }
  const delta = target.subtract(movement.value.root.position);
  const distance = delta.length();
  if (distance < .08) {
    movement.value.root.position.copyFrom(target);
    movement.index += 1;
    return;
  }
  const step = Math.min(distance, state.scene.getEngine().getDeltaTime() * .0025);
  movement.value.root.position.addInPlace(delta.normalize().scale(step));
  movement.value.root.rotation.y = lerpAngle(
    movement.value.root.rotation.y,
    headingForDirection(delta),
    .2
  );
  movement.value.root.position.y = .35 + Math.abs(Math.sin(performance.now() * .014)) * .08;
}

function updateOfficeActivity() {
  if (!state.scene) return;
  const now = performance.now();
  const delta = state.scene.getEngine().getDeltaTime();
  for (const [index, ambient] of state.ambientMeshes.entries()) {
    if (ambient.kind === "foliage") {
      ambient.mesh.rotation.z = Math.sin(now * .00065 + ambient.phase) * .035;
      ambient.mesh.rotation.x = Math.cos(now * .00048 + ambient.phase) * .018;
    } else {
      const pulse = .96 + Math.sin(now * .0012 + ambient.phase) * .06;
      ambient.mesh.scaling.setAll(pulse);
    }
    if (index > 30) break;
  }
  for (const value of state.employeeNodes.values()) {
    const active = value.employee.status.key === "idle" || value.employee.status.key === "complete";
    updateSemanticJourney(value, now);
    const ambientMoving = Boolean(value.ambient && !value.ambient.holdUntil);
    applyCharacterPose(value, now, value.walking || ambientMoving);
    if ((state.quality === "eco" && !value.ambient?.semantic)
      || value.dragging || value.walking) continue;
    if (!value.ambient && active && now >= value.nextAmbientAt) startAmbientWalk(value, now);
    if (!active && !value.ambient?.semantic) continue;
    const movement = value.ambient;
    if (!movement) continue;
    if (movement.holdUntil && now < movement.holdUntil) {
      value.root.position.y = .35 + Math.sin(now * .0025) * .018;
      continue;
    }
    if (movement.holdUntil) {
      movement.holdUntil = 0;
      const currentCell = worldToCell(value.root.position.x, value.root.position.z);
      const route = findGridPath(
        currentCell,
        value.employee.grid,
        new Set([value.employee.id])
      );
      movement.path = routeWorldPoints(route);
      movement.index = Math.min(1, movement.path.length);
      movement.target = movement.path[movement.index]
        || new BABYLON.Vector3(value.employee.position.x, .35, value.employee.position.z);
      movement.returning = true;
    }
    const target = new BABYLON.Vector3(movement.target.x, .35, movement.target.z);
    const direction = target.subtract(value.root.position);
    const distance = direction.length();
    if (distance < .05) {
      value.root.position.copyFrom(target);
      if (movement.path && movement.index < movement.path.length - 1) {
        movement.index += 1;
        movement.target = movement.path[movement.index];
        continue;
      }
      if (movement.semantic) {
        value.ambient = null;
        value.root.rotation.y = 0;
        if (movement.returning) {
          value.semanticLocation = null;
          value.semanticTarget = null;
          value.nextAmbientAt = now + 4000;
        } else {
          value.semanticLocation = value.behavior.destination;
        }
        continue;
      }
      if (movement.returning) {
        value.ambient = null;
        value.root.rotation.y = 0;
        value.nextAmbientAt = now + 5000 + hash(`${value.employee.id}:${Math.floor(now / 1000)}`) % 9000;
      } else {
        movement.holdUntil = now + 1200 + hash(value.employee.id) % 1800;
      }
      continue;
    }
    const step = Math.min(distance, delta * .00115);
    value.root.position.addInPlace(direction.normalize().scale(step));
    value.root.rotation.y = lerpAngle(
      value.root.rotation.y,
      headingForDirection(direction),
      .16
    );
    value.root.position.y = .35 + Math.abs(Math.sin(now * .012)) * .055;
    state.labelsDirty = true;
  }
}

function updateSemanticJourney(value, now) {
  if (value.walking || value.dragging) return;
  if (value.ambient?.semantic) {
    if (value.behavior.destination !== "operations" && !value.ambient.returning) {
      value.ambient = null;
      startSemanticJourney(value, value.employee.grid, true, now);
    }
    return;
  }
  if (value.ambient) return;
  if (value.behavior.destination === "operations" && value.semanticLocation !== "operations") {
    const target = semanticDestinationCell(value, "operations");
    if (target) startSemanticJourney(value, target, false, now);
    return;
  }
  if (value.behavior.destination !== "operations" && value.semanticLocation) {
    startSemanticJourney(value, value.employee.grid, true, now);
  }
}

function semanticDestinationCell(value, department) {
  const room = state.officeLayout.rooms.find((candidate) => candidate.key === department);
  if (!room) return null;
  const reserved = new Set([...state.employeeNodes.values()]
    .filter((candidate) => candidate !== value && candidate.semanticTarget)
    .map((candidate) => cellKey(
      candidate.semanticTarget.gridX,
      candidate.semanticTarget.gridZ
    )));
  const candidates = [];
  for (let gridX = room.grid_x; gridX < room.grid_x + room.width; gridX += 1) {
    for (let gridZ = room.grid_z; gridZ < room.grid_z + room.depth; gridZ += 1) {
      if (validOfficeCell(gridX, gridZ, value.employee.id)
        && !reserved.has(cellKey(gridX, gridZ))) {
        candidates.push({ gridX, gridZ });
      }
    }
  }
  if (!candidates.length) return null;
  const start = hash(value.employee.id) % candidates.length;
  const current = worldToCell(value.root.position.x, value.root.position.z);
  for (let offset = 0; offset < candidates.length; offset += 1) {
    const candidate = candidates[(start + offset) % candidates.length];
    if (findGridPath(current, candidate, new Set([value.employee.id])).length) return candidate;
  }
  return null;
}

function startSemanticJourney(value, destination, returning, now) {
  const current = worldToCell(value.root.position.x, value.root.position.z);
  const route = findGridPath(current, destination, new Set([value.employee.id]));
  if (route.length < 2) {
    if (returning) {
      value.semanticLocation = null;
      value.semanticTarget = null;
    } else {
      value.semanticLocation = value.behavior.destination;
      value.semanticTarget = { ...destination };
    }
    return;
  }
  const path = routeWorldPoints(route);
  value.semanticTarget = { ...destination };
  value.ambient = {
    path,
    index: 1,
    target: path[1],
    returning,
    semantic: true,
    holdUntil: 0,
    startedAt: now
  };
}

function applyCharacterPose(value, now, moving) {
  const phase = now * (moving ? .0115 : .00135) + value.posePhase;
  const stride = moving ? Math.sin(phase) : 0;
  value.legs.forEach((leg, index) => {
    leg.rotation.x = moving ? stride * (index ? -.62 : .62) : Math.sin(phase + index) * .018;
  });
  value.arms.forEach((arm, index) => {
    arm.rotation.x = moving ? stride * (index ? .42 : -.42) : Math.sin(phase * .8 + index) * .025;
  });
  if (!moving && value.behavior.key === "focused") {
    value.arms[0].rotation.x = -.36 + Math.sin(now * .003) * .035;
    value.arms[1].rotation.x = -.36 + Math.cos(now * .003) * .035;
  }
  const breath = Math.sin(now * .002 + value.posePhase);
  value.body.scaling.y = 1 + breath * .018;
  value.headPivot.position.y = 2.02 + (moving ? Math.abs(Math.sin(phase * 2)) * .045 : breath * .012);
  value.headPivot.rotation.y = moving ? stride * .04 : Math.sin(now * .0007 + value.posePhase) * .09;
  value.headPivot.rotation.z = moving ? -stride * .025 : (
    value.behavior.key === "blocked" ? Math.sin(now * .009) * .065 : 0
  );
  const blinkWindow = (now + hash(value.employee.id) % 3100) % 4300;
  const eyeScale = blinkWindow < 125 ? .08 : 1;
  value.eyes.forEach((eye) => { eye.scaling.y = eyeScale; });
  value.tablet.rotation.x = -.12 + Math.sin(now * .0017 + value.posePhase) * .025;
  value.tablet.rotation.z = Math.sin(now * .0013 + value.posePhase) * .03;
  const pulseStrength = value.behavior.key === "blocked" ? .075 : .025;
  const basePulse = 1 + Math.sin(now * .0022 + value.posePhase) * pulseStrength;
  value.base.scaling.setAll(basePulse);
  for (const item of value.preset.animated) {
    if (item.kind === "orbit") {
      item.mesh.rotation.z = now * .0012 + value.posePhase;
      item.mesh.position.y = item.baseY + Math.sin(now * .0018 + value.posePhase) * .035;
    } else if (item.kind === "spark") {
      item.mesh.rotation.y = now * .0016;
      item.mesh.position.y = item.baseY + Math.sin(now * .0025 + value.posePhase) * .09;
    } else {
      const pulse = 1 + Math.sin(now * .003 + value.posePhase) * .13;
      item.mesh.scaling.setAll(pulse);
    }
  }
  if (!moving) value.root.position.y = BABYLON.Scalar.Lerp(value.root.position.y, .35, .18);
}

function lerpAngle(current, target, amount) {
  let delta = (target - current + Math.PI) % (Math.PI * 2) - Math.PI;
  if (delta < -Math.PI) delta += Math.PI * 2;
  return current + delta * amount;
}

function headingForDirection(direction) {
  // Employee faces local -Z (eyes/tablet are on that side), while Babylon's
  // common yaw formula assumes local +Z is forward.
  return Math.atan2(-direction.x, -direction.z);
}

function startAmbientWalk(value, now) {
  const home = value.employee.grid;
  const room = roomForCell(home.gridX, home.gridZ);
  const candidates = [];
  if (room) {
    for (let gridX = room.grid_x; gridX < room.grid_x + room.width; gridX += 1) {
      for (let gridZ = room.grid_z; gridZ < room.grid_z + room.depth; gridZ += 1) {
        const distance = Math.abs(gridX - home.gridX) + Math.abs(gridZ - home.gridZ);
        if (distance >= 1 && distance <= 3 && validOfficeCell(gridX, gridZ, value.employee.id)) {
          candidates.push({ gridX, gridZ });
        }
      }
    }
  }
  if (!candidates.length) {
    value.nextAmbientAt = now + 5000;
    return;
  }
  const choice = candidates[hash(`${value.employee.id}:${Math.floor(now / 5000)}`) % candidates.length];
  const route = findGridPath(home, choice, new Set([value.employee.id]));
  if (route.length < 2) {
    value.nextAmbientAt = now + 5000;
    return;
  }
  const path = routeWorldPoints(route);
  value.ambient = {
    path,
    index: 1,
    target: path[1],
    returning: false,
    holdUntil: 0
  };
}

function render() {
  applyLanguage();
  renderTasks();
  renderRoster();
  renderSpaceMap();
  renderMission();
  renderInteractionFeed();
  renderInspector();
  $("employee-count").textContent = state.employees.length;
  $("working-count").textContent = state.employees.filter((employee) => employee.status.key === "working").length;
  $("blocked-count").textContent = state.employees.filter((employee) => employee.status.key === "blocked").length;
}

function applyLanguage() {
  document.documentElement.lang = state.language;
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => { node.title = t(node.dataset.i18nTitle); });
  $("language-toggle").textContent = t("language");
  $("quality-toggle").textContent = state.quality === "eco" ? t("low") : t("high");
  for (const [department, value] of state.departmentLabels) {
    value.element.innerHTML = `<span>${department.slice(0, 3).toUpperCase()}</span><strong>${escapeHtml(departmentName(department))}</strong><small>DEPARTMENT</small>`;
  }
  state.labelsDirty = true;
  updateLocation();
}

function renderTasks() {
  const query = $("task-search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => `${task.objective} ${task.status}`.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.map((task) => `<button class="task-card ${task.id === state.selectedTaskId ? "active" : ""}" type="button" data-task-id="${escapeHtml(task.id)}"><strong>${escapeHtml(task.objective)}</strong><span>${escapeHtml(task.status)} · ${task.subtasks.length || task.runs.length} units</span></button>`).join("");
  document.querySelectorAll("[data-task-id]").forEach((button) => button.addEventListener("click", async () => {
    state.selectedTaskId = button.dataset.taskId;
    await loadTaskInteractions();
    render();
    syncScene();
    animateLatestHandoff();
    animateLatestInteraction();
  }));
}

function renderRoster() {
  const selected = state.selectedEmployeeId || "";
  $("employee-select").innerHTML = ['<option value="">—</option>', ...state.employees.map((employee) => `<option value="${escapeHtml(employee.id)}">${escapeHtml(employee.name)} · ${escapeHtml(departmentName(employee.department))}</option>`)].join("");
  $("world-agent-options").innerHTML = state.employees.map((employee) => `<option value="${escapeHtml(employee.name)}"></option>`).join("");
  $("employee-select").value = selected;
}

function renderSpaceMap() {
  $("space-map").innerHTML = `${Object.entries(DEPARTMENTS).map(([key, zone]) => `<button type="button" data-zone="${escapeHtml(key)}" title="${escapeHtml(departmentName(key))}" style="--space-color:${escapeHtml(zone.color)}"><span>${escapeHtml(key.slice(0, 1).toUpperCase())}</span></button>`).join("")}<i></i><small data-i18n="minimap">${escapeHtml(t("minimap"))}</small>`;
  document.querySelectorAll("[data-zone]").forEach((button) => button.addEventListener("click", () => focusZone(button.dataset.zone)));
}

function renderMission() {
  const task = selectedTask();
  $("mission-title").textContent = task?.objective || t("company");
  $("selected-mission").textContent = task?.objective || "—";
  $("selected-status").textContent = task?.status || "IDLE";
  $("handoff-count").textContent = String(task?.handoffs?.length || 0);
  $("open-task").href = task ? `/?task=${encodeURIComponent(task.id)}` : "/";
}

function renderInspector() {
  const employee = state.employees.find((item) => item.id === state.selectedEmployeeId);
  $("inspector-empty").classList.toggle("hidden", Boolean(employee));
  $("inspector-content").classList.toggle("hidden", !employee);
  if (!employee) return;
  const version = employee.versions.find((item) => item.id === employee.defaultVersionId)
    || employee.versions.find((item) => item.status === "PUBLISHED") || employee.versions[0];
  $("profile-avatar").style.setProperty("--avatar", employee.color);
  $("profile-department").textContent = employee.organizationUnitName
    || departmentName(employee.department);
  $("profile-name").textContent = employee.name;
  $("profile-role").textContent = employee.positionTitle || version?.role || "General Agent";
  $("profile-status").className = `profile-status ${employee.status.key}`;
  $("profile-status").textContent = `${employee.status.label} · ${employeeBehavior(employee).label}`;
  $("profile-description").textContent = employee.description || version?.instructions?.slice(0, 220) || "Runtime Agent";
  $("profile-work").innerHTML = employee.assignment
    ? `<strong>${escapeHtml(employee.assignment.subtask?.objective || employee.assignment.task.objective)}</strong><span>${escapeHtml(employee.assignment.run.status)} · Run ${escapeHtml(shortId(employee.assignment.run.id))}</span>`
    : `<span>${escapeHtml(t("noWork"))}</span>`;
  $("profile-version").textContent = version ? `v${version.semantic_version} · ${version.status}` : "Runtime only";
  $("profile-lifecycle").textContent = employee.lifecycle;
  $("profile-capabilities").textContent = version?.declared_capabilities?.join(", ") || "general.task";
  $("profile-tools").textContent = version?.tool_profile?.allowed_tools?.join(", ") || version?.tool_profile?.allowed_tool_keys?.join(", ") || "—";
}

function samplePerformance() {
  if (!state.engine || state.quality !== "auto") return;
  state.frameSamples.push(state.engine.getFps());
  if (state.frameSamples.length < 180) return;
  const average = state.frameSamples.reduce((sum, value) => sum + value, 0) / state.frameSamples.length;
  state.frameSamples = [];
  if (average < 28) {
    state.quality = "eco";
    setHighDpi();
    applyLanguage();
    toast("Performance mode enabled");
  }
}

function setOnline(online) {
  $("system-status").className = `system-status ${online ? "online" : "error"}`;
  $("system-status").querySelector("span").textContent = online ? t("online") : t("offline");
}

let toastTimer = null;
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast show";
  toastTimer = window.setTimeout(() => { $("toast").className = "toast"; }, 2400);
}

const taskRoleDefaults = [
  { key: "research", role: "Researcher", agent: "demo-researcher", objective: "Collect facts, constraints, and source evidence", depends: "" },
  { key: "analysis", role: "Analyst", agent: "demo-analyst", objective: "Analyze evidence and propose options", depends: "research" },
  { key: "delivery", role: "Synthesizer", agent: "demo-synthesizer", objective: "Produce the final accepted deliverable", depends: "analysis" }
];

function addTaskRole(value = {}) {
  const row = document.createElement("div");
  row.className = "task-role-row";
  row.innerHTML = `
    <label><span>${escapeHtml(t("role"))}</span><input class="task-role-name" required maxlength="80" value="${escapeHtml(value.role || "Specialist")}"></label>
    <label><span>${escapeHtml(t("agent"))}</span><input class="task-role-agent" required maxlength="63" list="world-agent-options" value="${escapeHtml(value.agent || state.employees[0]?.name || "demo-agent")}"></label>
    <label><span>${escapeHtml(t("roleObjective"))}</span><input class="task-role-objective" required maxlength="20000" value="${escapeHtml(value.objective || "Complete the assigned work")}"></label>
    <label><span>${escapeHtml(t("dependencies"))}</span><input class="task-role-depends" value="${escapeHtml(value.depends || "")}" placeholder="research,analysis"></label>
    <button type="button" class="icon-button task-role-remove" aria-label="${escapeHtml(t("remove"))}">×</button>
    <input class="task-role-key" type="hidden" value="${escapeHtml(value.key || `role-${crypto.randomUUID().slice(0, 8)}`)}">`;
  row.querySelector(".task-role-remove").addEventListener("click", () => row.remove());
  $("task-role-list").append(row);
}

function syncTaskMode() {
  const coordinated = $("task-mode").value === "COORDINATED";
  $("task-team-fields").classList.toggle("hidden", !coordinated);
  $("task-concurrency").disabled = !coordinated;
}

function openTaskDialog() {
  $("create-task-form").reset();
  $("task-role-list").innerHTML = "";
  taskRoleDefaults.forEach(addTaskRole);
  const coordinatedEnabled = featureEnabled("coordinated_execution");
  $("task-mode").querySelector('option[value="COORDINATED"]').disabled = !coordinatedEnabled;
  $("task-mode").value = coordinatedEnabled ? "COORDINATED" : "DIRECT";
  $("task-form-error").textContent = "";
  syncTaskMode();
  $("create-task-dialog").showModal();
  window.setTimeout(() => $("task-objective").focus(), 30);
}

async function submitTask(event) {
  event.preventDefault();
  const objective = $("task-objective").value.trim();
  const mode = $("task-mode").value;
  const rows = [...document.querySelectorAll(".task-role-row")];
  const subtasks = mode === "COORDINATED" ? rows.map((row, index) => ({
    key: row.querySelector(".task-role-key").value.replace(/[^a-zA-Z0-9_-]/g, "-").toLowerCase() || `role-${index + 1}`,
    objective: row.querySelector(".task-role-objective").value.trim(),
    input: { role: row.querySelector(".task-role-name").value.trim() },
    required_capabilities: ["general.task"],
    preferred_agent_id: row.querySelector(".task-role-agent").value.trim(),
    depends_on: row.querySelector(".task-role-depends").value.split(",").map((item) => item.trim()).filter(Boolean)
  })) : [];
  if (mode === "COORDINATED" && subtasks.length < 2) {
    $("task-form-error").textContent = "Multi-Agent collaboration requires at least two roles.";
    return;
  }
  const payload = {
    objective,
    execution_mode: mode,
    ...(mode === "COORDINATED" ? { subtasks, max_concurrency: Number($("task-concurrency").value) } : {})
  };
  $("task-create-submit").disabled = true;
  $("task-form-error").textContent = "";
  try {
    const task = await api("/api/v1/tasks", { method: "POST", body: JSON.stringify(payload) });
    const startNow = $("task-start-now").checked;
    let startError = null;
    if (startNow) {
      try {
        await api(`/api/v1/tasks/${task.id}/runs`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() } });
      } catch (error) {
        startError = error;
      }
    }
    $("create-task-dialog").close();
    await loadCompany({ quiet: true });
    state.selectedTaskId = task.id;
    render();
    syncScene();
    toast(startError ? `${t("taskCreated")} · ${startError.message}` : startNow ? t("taskStarted") : t("taskCreated"));
  } catch (error) {
    $("task-form-error").textContent = error.message;
  } finally {
    $("task-create-submit").disabled = false;
  }
}

function renderCustomSpaceList() {
  $("custom-space-list").innerHTML = CUSTOM_SPACES.length
    ? CUSTOM_SPACES.map((space) => `<span class="custom-space-chip">${escapeHtml(space.name)} · ${escapeHtml(t(space.style))}</span>`).join("")
    : `<span class="custom-space-chip">${escapeHtml(t("noCustomSpaces"))}</span>`;
}

function openCampusPlanner() {
  $("campus-form").reset();
  $("space-color").value = "#4fb8ff";
  $("campus-form-error").textContent = "";
  renderCustomSpaceList();
  $("campus-dialog").showModal();
}

async function submitSpace(event) {
  event.preventDefault();
  if (CUSTOM_SPACES.length >= 8) {
    $("campus-form-error").textContent = "The current renderer supports up to eight custom spaces.";
    return;
  }
  $("space-create-submit").disabled = true;
  try {
    await api("/api/v1/office-layout/spaces", {
      method: "POST",
      body: JSON.stringify({
        name: $("space-name").value.trim(),
        style: $("space-style").value,
        color: $("space-color").value
      })
    });
    window.location.reload();
  } catch (error) {
    $("campus-form-error").textContent = error.message;
  } finally {
    $("space-create-submit").disabled = false;
  }
}

$("new-task-button").addEventListener("click", openTaskDialog);
$("create-task-form").addEventListener("submit", submitTask);
$("task-mode").addEventListener("change", syncTaskMode);
$("add-task-role").addEventListener("click", () => addTaskRole());
$("campus-planner").addEventListener("click", openCampusPlanner);
$("campus-form").addEventListener("submit", submitSpace);
$("reset-campus").addEventListener("click", async () => {
  if (window.confirm("Reset all custom campus spaces?")) {
    try {
      await api("/api/v1/office-layout/spaces", { method: "DELETE" });
      localStorage.removeItem(STORAGE_SPACES);
      window.location.reload();
    } catch (error) {
      $("campus-form-error").textContent = error.message;
    }
  }
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
$("task-search").addEventListener("input", renderTasks);
$("employee-select").addEventListener("change", () => {
  if ($("employee-select").value) selectEmployee($("employee-select").value);
});
$("language-toggle").addEventListener("click", () => {
  state.language = state.language === "en" ? "zh-CN" : "en";
  localStorage.setItem(STORAGE_LANGUAGE, state.language);
  state.employees = buildEmployees();
  render();
  syncScene();
});
$("quality-toggle").addEventListener("click", () => {
  state.quality = state.quality === "eco" ? "auto" : "eco";
  setHighDpi();
  applyLanguage();
});
$("zoom-in").addEventListener("click", () => changeZoom(.88));
$("zoom-out").addEventListener("click", () => changeZoom(1.12));
$("camera-home").addEventListener("click", focusCampus);
$("camera-focus").addEventListener("click", () => {
  const value = state.employeeNodes.get(state.selectedEmployeeId);
  if (value) focusPoint(value.root.position.x, value.root.position.z, 5.8);
});

applyLanguage();
if (window.BABYLON) {
  loadCompany().then(() => {
    createScene();
    render();
    syncScene();
    animateLatestHandoff();
    animateLatestInteraction();
    window.setInterval(
      () => loadCompany({ quiet: true }),
      featureEnabled("realtime_events") ? 15000 : 3000
    );
  });
} else {
  $("world-fallback").classList.remove("hidden");
}
