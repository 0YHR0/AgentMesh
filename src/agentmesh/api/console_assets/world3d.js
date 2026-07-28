const $ = (id) => document.getElementById(id);
const STORAGE_LANGUAGE = "agentmesh-language";
const ACTIVE_RUNS = new Set(["READY", "RUNNING", "PAUSE_REQUESTED", "PAUSED"]);
const TERMINAL_TASKS = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const COLORS = ["#28d9f5", "#9d7bff", "#ffc95e", "#56e39f", "#ff6f91", "#5ca8ff"];
const DEPARTMENTS = {
  research: { x: -10.4, z: -6.2, color: "#45c9ed", accent: "#9ff6ff", floor: "#247fa1" },
  analysis: { x: 10.4, z: -6.2, color: "#9878f3", accent: "#d5bdff", floor: "#5d49a2" },
  engineering: { x: -10.4, z: 6.2, color: "#4cd9a4", accent: "#b6ffd9", floor: "#298c70" },
  operations: { x: 10.4, z: 6.2, color: "#f0b957", accent: "#fff0a8", floor: "#a16f24" }
};
const COPY = {
  en: {
    connecting: "Connecting…", online: "Company systems online", offline: "Company data unavailable",
    lightMode: "Lightweight Office", console: "Control Console", missions: "Company missions",
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
    operations: "Review Court", hub: "Central Nexus"
  },
  "zh-CN": {
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
    operations: "评审大厅", hub: "中央枢纽"
  }
};

const state = {
  language: localStorage.getItem(STORAGE_LANGUAGE) === "zh-CN" ? "zh-CN" : "en",
  token: sessionStorage.getItem("agentmesh-token") || "",
  features: new Map(),
  tasks: [],
  agents: [],
  employees: [],
  selectedTaskId: null,
  selectedEmployeeId: null,
  scene: null,
  engine: null,
  camera: null,
  cameraTarget: null,
  orthoSize: 14.5,
  employeeNodes: new Map(),
  departmentLabels: new Map(),
  movement: null,
  animatedHandoffs: new Set(),
  quality: "auto",
  frameSamples: [],
  keys: new Set(),
  loadInFlight: false
};

function t(key) { return COPY[state.language][key] || COPY.en[key] || key; }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}
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

async function api(path) {
  const headers = { Accept: "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { headers });
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
    const taskPayload = await api("/api/v1/tasks?limit=50&offset=0");
    state.tasks = taskPayload.items;
    state.agents = featureEnabled("agent_registry_management")
      ? (await api("/api/v1/agents?limit=100&offset=0")).items
      : [];
    state.employees = buildEmployees();
    if (!state.selectedTaskId || !state.tasks.some((task) => task.id === state.selectedTaskId)) {
      state.selectedTaskId = state.tasks.find((task) => !TERMINAL_TASKS.has(task.status))?.id || state.tasks[0]?.id || null;
    }
    render();
    syncScene();
    animateLatestHandoff();
    setOnline(true);
  } catch (error) {
    setOnline(false);
    if (!quiet) toast(error.message);
  } finally {
    state.loadInFlight = false;
  }
}

function buildEmployees() {
  const definitions = new Map(state.agents.map((agent) => [agent.name, agent]));
  for (const task of state.tasks) {
    for (const run of task.runs) {
      if (!definitions.has(run.agent_id)) definitions.set(run.agent_id, syntheticAgent(run.agent_id));
    }
  }
  return [...definitions.values()].map((agent, index) => {
    const assignment = findAssignment(agent.name);
    const department = departmentFor(agent);
    return {
      id: agent.id || `runtime:${agent.name}`,
      name: agent.name,
      description: agent.description || "",
      lifecycle: agent.lifecycle || "RUNTIME",
      defaultVersionId: agent.default_version_id || null,
      versions: agent.versions || [],
      department,
      color: COLORS[hash(agent.name) % COLORS.length],
      position: homePosition(department, index, agent.name),
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
  return Object.keys(DEPARTMENTS)[hash(agent.name) % 4];
}

function homePosition(department, index, name) {
  const zone = DEPARTMENTS[department];
  const slot = (index + hash(name)) % 6;
  return {
    x: zone.x + ((slot % 3) - 1) * 2.1,
    z: zone.z + (Math.floor(slot / 3) ? 1.7 : -1.2)
  };
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
    scene.clearColor = new BABYLON.Color4(0.53, 0.78, 0.87, 1);
    scene.imageProcessingConfiguration.contrast = 1.15;
    scene.imageProcessingConfiguration.saturation = 1.12;
    const cameraTarget = new BABYLON.Vector3(0, 0, 0);
    state.cameraTarget = cameraTarget;
    const camera = new BABYLON.ArcRotateCamera(
      "office-camera", -Math.PI / 4, Math.PI / 3.1, 28, cameraTarget, scene
    );
    camera.mode = BABYLON.Camera.ORTHOGRAPHIC_CAMERA;
    camera.inputs.clear();
    state.camera = camera;
    applyOrthographicCamera();
    const ambient = new BABYLON.HemisphericLight("sky", new BABYLON.Vector3(0.2, 1, -0.2), scene);
    ambient.intensity = 1.15;
    ambient.diffuse = new BABYLON.Color3(0.88, 0.95, 1);
    ambient.groundColor = new BABYLON.Color3(0.22, 0.32, 0.38);
    const sun = new BABYLON.DirectionalLight("sun", new BABYLON.Vector3(-0.55, -1, 0.4), scene);
    sun.position = new BABYLON.Vector3(12, 24, -16);
    sun.intensity = 0.8;
    createCampus(scene);
    configureInput(canvas);
    scene.onPointerObservable.add((event) => {
      if (event.type !== BABYLON.PointerEventTypes.POINTERPICK) return;
      const employeeId = event.pickInfo?.pickedMesh?.metadata?.employeeId;
      if (employeeId) selectEmployee(employeeId, false);
    });
    engine.runRenderLoop(() => {
      updateCamera();
      updateMovement();
      updateLabels();
      scene.render();
      samplePerformance();
    });
    window.addEventListener("resize", () => {
      engine.resize();
      applyOrthographicCamera();
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
  result.specularColor = new BABYLON.Color3(0.12, 0.16, 0.2);
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

function createCampus(scene) {
  const grass = material(scene, "campus-grass", "#69ad91");
  const base = BABYLON.MeshBuilder.CreateBox("campus-base", { width: 40, depth: 27, height: 0.7 }, scene);
  base.position.y = -0.42;
  base.material = grass;
  const lowerBase = meshBox(
    scene, "campus-foundation", { width: 41.2, depth: 28.2, height: .55 },
    [0, -.9, 0], material(scene, "campus-foundation-material", "#173d59")
  );
  lowerBase.receiveShadows = true;
  const border = material(scene, "campus-border", "#27536d");
  for (const [x, z, width, depth] of [[0, -13.25, 40, .55], [0, 13.25, 40, .55], [-19.75, 0, .55, 27], [19.75, 0, .55, 27]]) {
    const wall = BABYLON.MeshBuilder.CreateBox("campus-wall", { width, depth, height: 1.2 }, scene);
    wall.position.set(x, 0.15, z);
    wall.material = border;
  }
  createPaths(scene);
  for (const [department, zone] of Object.entries(DEPARTMENTS)) createDepartment(scene, department, zone);
  createHub(scene);
  createCampusAmenities(scene);
}

function createDepartment(scene, department, zone) {
  const plateMaterial = material(scene, `${department}-floor`, zone.floor);
  const plate = BABYLON.MeshBuilder.CreateBox(`${department}-plate`, {
    width: 15.6, depth: 10.2, height: 0.38
  }, scene);
  plate.position.set(zone.x, -0.06, zone.z);
  plate.material = plateMaterial;
  plate.metadata = { zone: department };
  const trim = material(scene, `${department}-trim`, zone.color, { emissive: 0.25 });
  for (const [dx, dz, width, depth] of [[0, -4.95, 15.6, .22], [0, 4.95, 15.6, .22], [-7.7, 0, .22, 10], [7.7, 0, .22, 10]]) {
    const line = BABYLON.MeshBuilder.CreateBox(`${department}-trim`, { width, depth, height: .16 }, scene);
    line.position.set(zone.x + dx, .2, zone.z + dz);
    line.material = trim;
  }
  const creators = {
    research: createResearchLab,
    analysis: createAnalysisStudio,
    engineering: createEngineeringBay,
    operations: createReviewCourt
  };
  creators[department](scene, zone, trim);
  const label = document.createElement("div");
  label.className = `department-label ${department}`;
  label.style.setProperty("--department-color", zone.color);
  label.innerHTML = `<span>${department.slice(0, 3).toUpperCase()}</span><strong>${escapeHtml(t(department))}</strong>`;
  $("agent-labels").append(label);
  state.departmentLabels.set(department, {
    element: label,
    point: new BABYLON.Vector3(zone.x, 1.05, zone.z - 4.3)
  });
}

function createWorkstation(scene, name, x, z, accent, angle = 0) {
  const shell = material(scene, `${name}-shell-material`, "#d7e6e8");
  const desk = meshBox(scene, `${name}-desk`, { width: 2.25, depth: 1.02, height: .68 }, [x, .55, z], shell);
  desk.rotation.y = angle;
  const screen = meshBox(scene, `${name}-screen`, { width: .92, depth: .12, height: .62 }, [x, 1.25, z - .28], accent, [-.12, angle, 0]);
  screen.rotation.y = angle;
  return desk;
}

function createResearchLab(scene, zone, accent) {
  const dark = material(scene, "research-structure", "#173f5c");
  const glass = material(scene, "research-glass", zone.accent, { emissive: .18, alpha: .72 });
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
  const glass = material(scene, "analysis-glass", zone.accent, { emissive: .22, alpha: .76 });
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
  const hazard = material(scene, "engineering-hazard", "#f8d35d", { emissive: .08 });
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
  const display = material(scene, "operations-display", zone.accent, { emissive: .24 });
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

function createHub(scene) {
  const hubMaterial = material(scene, "hub", "#286f92");
  const hub = BABYLON.MeshBuilder.CreateCylinder("handoff-hub", {
    diameter: 5.2, height: .5, tessellation: 32
  }, scene);
  hub.position.y = .05;
  hub.material = hubMaterial;
  const ringMaterial = material(scene, "hub-ring", "#53e8ff", { emissive: 0.55 });
  for (const diameter of [2.2, 3.7, 5]) {
    const ring = BABYLON.MeshBuilder.CreateTorus("hub-ring", {
      diameter, thickness: .1, tessellation: 48
    }, scene);
    ring.position.y = .36;
    ring.material = ringMaterial;
  }
  const core = BABYLON.MeshBuilder.CreatePolyhedron("hub-core", { type: 1, size: 1.05 }, scene);
  core.position.y = 1.5;
  core.material = material(scene, "hub-core-material", "#9d7bff", { emissive: .45 });
  scene.onBeforeRenderObservable.add(() => {
    if (state.quality === "eco") return;
    core.rotation.y += scene.getEngine().getDeltaTime() * .0007;
    core.position.y = 1.5 + Math.sin(performance.now() * .002) * .12;
  });
}

function createPaths(scene) {
  const pathMaterial = material(scene, "path", "#e6eee5");
  const pathEdge = material(scene, "path-edge", "#79cdda", { emissive: .12 });
  for (const [x, z, width, depth] of [[0, 0, 36, 2.15], [0, 0, 2.15, 23]]) {
    const path = BABYLON.MeshBuilder.CreateBox("campus-path", { width, depth, height: .15 }, scene);
    path.position.y = .18;
    path.material = pathMaterial;
  }
  for (const [x, z, width, depth] of [[0, -1.15, 36, .12], [0, 1.15, 36, .12], [-1.15, 0, .12, 23], [1.15, 0, .12, 23]]) {
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
  const foliage = material(scene, "tree-foliage", "#48c987");
  const lamp = material(scene, "campus-lamp", "#2d5266");
  const light = material(scene, "campus-lamp-light", "#d9ffff", { emissive: .7 });
  for (const [index, x, z] of [
    [0, -17.8, -11.5], [1, -17.8, 11.4], [2, 17.8, -11.5], [3, 17.8, 11.4],
    [4, -2.8, -11.6], [5, 2.8, -11.6], [6, -2.8, 11.6], [7, 2.8, 11.6]
  ]) {
    meshCylinder(scene, `campus-tree-trunk-${index}`, { diameter: .42, height: 1.65, tessellation: 8 }, [x, .92, z], trunk);
    const crown = BABYLON.MeshBuilder.CreatePolyhedron(`campus-tree-${index}`, { type: 1, size: 1.25 }, scene);
    crown.position.set(x, 2.15, z);
    crown.material = foliage;
  }
  for (const [index, x, z] of [[0, -5.4, -1.8], [1, 5.4, -1.8], [2, -5.4, 1.8], [3, 5.4, 1.8]]) {
    meshCylinder(scene, `campus-lamp-${index}`, { diameter: .22, height: 2.1, tessellation: 8 }, [x, 1.28, z], lamp);
    const bulb = BABYLON.MeshBuilder.CreateSphere(`campus-lamp-bulb-${index}`, { diameter: .48, segments: 8 }, scene);
    bulb.position.set(x, 2.42, z);
    bulb.material = light;
  }
}

function createEmployeeNode(employee) {
  const scene = state.scene;
  const root = new BABYLON.TransformNode(`employee:${employee.id}`, scene);
  root.position.set(employee.position.x, .35, employee.position.z);
  const shirt = material(scene, `shirt:${employee.id}`, employee.color);
  const skin = material(scene, `skin:${employee.id}`, "#f3bb91");
  const dark = material(scene, `dark:${employee.id}`, "#24364f");
  const body = BABYLON.MeshBuilder.CreateBox(`body:${employee.id}`, { width: .72, depth: .48, height: .95 }, scene);
  body.parent = root; body.position.y = 1.15; body.material = shirt;
  const head = BABYLON.MeshBuilder.CreateSphere(`head:${employee.id}`, { diameter: .68, segments: 8 }, scene);
  head.parent = root; head.position.y = 1.98; head.material = skin;
  const hair = BABYLON.MeshBuilder.CreateBox(`hair:${employee.id}`, { width: .67, depth: .66, height: .2 }, scene);
  hair.parent = root; hair.position.y = 2.28; hair.material = dark;
  for (const side of [-1, 1]) {
    const leg = BABYLON.MeshBuilder.CreateBox(`leg:${employee.id}`, { width: .24, depth: .3, height: .62 }, scene);
    leg.parent = root; leg.position.set(side * .2, .38, 0); leg.material = dark;
    const arm = BABYLON.MeshBuilder.CreateBox(`arm:${employee.id}`, { width: .2, depth: .25, height: .75 }, scene);
    arm.parent = root; arm.position.set(side * .48, 1.14, 0); arm.material = shirt;
  }
  const base = BABYLON.MeshBuilder.CreateTorus(`base:${employee.id}`, { diameter: 1.25, thickness: .08, tessellation: 28 }, scene);
  base.parent = root; base.position.y = .08;
  base.material = material(scene, `base-mat:${employee.id}`, employee.color, { emissive: .4 });
  root.getChildMeshes().forEach((mesh) => { mesh.metadata = { employeeId: employee.id }; });
  const label = document.createElement("button");
  label.type = "button";
  label.className = `agent-label ${employee.status.key}`;
  label.dataset.employeeId = employee.id;
  label.innerHTML = `<strong>${escapeHtml(employee.name)}</strong><span>${escapeHtml(employee.status.label)}</span>`;
  label.addEventListener("click", () => selectEmployee(employee.id, false));
  $("agent-labels").append(label);
  return { root, label, base, employee, walking: false };
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
    value.label.className = `agent-label ${employee.status.key}${employee.id === state.selectedEmployeeId ? " selected" : ""}`;
    value.label.innerHTML = `<strong>${escapeHtml(employee.name)}</strong><span>${escapeHtml(employee.status.label)}</span>`;
    value.root.setEnabled(true);
    if (!value.walking) value.root.position.set(employee.position.x, .35, employee.position.z);
  }
}

function updateLabels() {
  if (!state.scene || !state.camera || !state.engine) return;
  const width = state.engine.getRenderWidth();
  const height = state.engine.getRenderHeight();
  const cssWidth = $("world-canvas").clientWidth;
  const cssHeight = $("world-canvas").clientHeight;
  for (const value of state.employeeNodes.values()) {
    const projected = BABYLON.Vector3.Project(
      value.root.position.add(new BABYLON.Vector3(0, 2.75, 0)),
      BABYLON.Matrix.Identity(),
      state.scene.getTransformMatrix(),
      state.camera.viewport.toGlobal(width, height)
    );
    const visible = projected.z > 0 && projected.z < 1
      && projected.x >= -60 && projected.x <= width + 60
      && projected.y >= -50 && projected.y <= height + 50;
    value.label.hidden = !visible;
    if (!visible) continue;
    value.label.style.left = `${projected.x / width * cssWidth}px`;
    value.label.style.top = `${projected.y / height * cssHeight}px`;
  }
  for (const value of state.departmentLabels.values()) {
    const projected = BABYLON.Vector3.Project(
      value.point,
      BABYLON.Matrix.Identity(),
      state.scene.getTransformMatrix(),
      state.camera.viewport.toGlobal(width, height)
    );
    const visible = projected.z > 0 && projected.z < 1
      && projected.x >= -80 && projected.x <= width + 80
      && projected.y >= -40 && projected.y <= height + 40;
    value.element.hidden = !visible;
    if (!visible) continue;
    value.element.style.left = `${projected.x / width * cssWidth}px`;
    value.element.style.top = `${projected.y / height * cssHeight}px`;
  }
}

function configureInput(canvas) {
  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    drag = { x: event.clientX, y: event.clientY, moved: false };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    panCamera(-dx * state.orthoSize / 430, dy * state.orthoSize / 430);
    drag.x = event.clientX;
    drag.y = event.clientY;
  });
  const endDrag = () => { drag = null; };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    changeZoom(event.deltaY > 0 ? 1.1 : .9);
  }, { passive: false });
  window.addEventListener("keydown", (event) => state.keys.add(event.code));
  window.addEventListener("keyup", (event) => state.keys.delete(event.code));
}

function panCamera(dx, dz) {
  if (!state.cameraTarget) return;
  state.cameraTarget.x = BABYLON.Scalar.Clamp(state.cameraTarget.x + dx, -13, 13);
  state.cameraTarget.z = BABYLON.Scalar.Clamp(state.cameraTarget.z + dz, -8, 8);
  state.camera.setTarget(state.cameraTarget);
  updateLocation();
}

function updateCamera() {
  const speed = state.orthoSize * .012;
  if (state.keys.has("KeyA") || state.keys.has("ArrowLeft")) panCamera(-speed, 0);
  if (state.keys.has("KeyD") || state.keys.has("ArrowRight")) panCamera(speed, 0);
  if (state.keys.has("KeyW") || state.keys.has("ArrowUp")) panCamera(0, -speed);
  if (state.keys.has("KeyS") || state.keys.has("ArrowDown")) panCamera(0, speed);
}

function changeZoom(multiplier) {
  state.orthoSize = BABYLON.Scalar.Clamp(state.orthoSize * multiplier, 4.5, 17);
  applyOrthographicCamera();
}

function focusPoint(x, z, size = 7.5) {
  state.cameraTarget.set(x, 0, z);
  state.camera.setTarget(state.cameraTarget);
  state.orthoSize = size;
  applyOrthographicCamera();
  updateLocation();
}

function focusZone(department) {
  const zone = DEPARTMENTS[department];
  if (zone) focusPoint(zone.x, zone.z, 6.7);
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
  $("camera-location").textContent = t(location).toUpperCase();
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
  const points = [
    sourceNode.root.position.clone(),
    new BABYLON.Vector3(0, .35, 0),
    targetNode.root.position.add(new BABYLON.Vector3(-1.1, 0, .25))
  ];
  sourceNode.walking = true;
  state.movement = { value: sourceNode, points, index: 1, holdUntil: 0, returning: false };
}

function updateMovement() {
  const movement = state.movement;
  if (!movement || !state.scene) return;
  if (movement.holdUntil) {
    if (performance.now() < movement.holdUntil) return;
    movement.holdUntil = 0;
  }
  const target = movement.points[movement.index];
  if (!target) {
    if (!movement.returning) {
      movement.returning = true;
      movement.points = [movement.value.root.position.clone(), new BABYLON.Vector3(0, .35, 0), new BABYLON.Vector3(movement.value.employee.position.x, .35, movement.value.employee.position.z)];
      movement.index = 1;
      movement.holdUntil = performance.now() + 900;
      return;
    }
    movement.value.walking = false;
    state.movement = null;
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
  movement.value.root.rotation.y = Math.atan2(delta.x, delta.z);
  movement.value.root.position.y = .35 + Math.abs(Math.sin(performance.now() * .014)) * .08;
}

function render() {
  applyLanguage();
  renderTasks();
  renderRoster();
  renderMission();
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
    value.element.innerHTML = `<span>${department.slice(0, 3).toUpperCase()}</span><strong>${escapeHtml(t(department))}</strong>`;
  }
  updateLocation();
}

function renderTasks() {
  const query = $("task-search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => `${task.objective} ${task.status}`.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.map((task) => `<button class="task-card ${task.id === state.selectedTaskId ? "active" : ""}" type="button" data-task-id="${escapeHtml(task.id)}"><strong>${escapeHtml(task.objective)}</strong><span>${escapeHtml(task.status)} · ${task.subtasks.length || task.runs.length} units</span></button>`).join("");
  document.querySelectorAll("[data-task-id]").forEach((button) => button.addEventListener("click", () => {
    state.selectedTaskId = button.dataset.taskId;
    render();
    syncScene();
    animateLatestHandoff();
  }));
}

function renderRoster() {
  const selected = state.selectedEmployeeId || "";
  $("employee-select").innerHTML = ['<option value="">—</option>', ...state.employees.map((employee) => `<option value="${escapeHtml(employee.id)}">${escapeHtml(employee.name)} · ${escapeHtml(t(employee.department))}</option>`)].join("");
  $("employee-select").value = selected;
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
  $("profile-department").textContent = t(employee.department);
  $("profile-name").textContent = employee.name;
  $("profile-role").textContent = version?.role || "General Agent";
  $("profile-status").className = `profile-status ${employee.status.key}`;
  $("profile-status").textContent = employee.status.label;
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
$("camera-home").addEventListener("click", () => focusPoint(0, 0, 12));
$("camera-focus").addEventListener("click", () => {
  const value = state.employeeNodes.get(state.selectedEmployeeId);
  if (value) focusPoint(value.root.position.x, value.root.position.z, 5.8);
});
document.querySelectorAll("[data-zone]").forEach((button) => button.addEventListener("click", () => focusZone(button.dataset.zone)));

applyLanguage();
if (window.BABYLON) {
  createScene();
  loadCompany().then(() => window.setInterval(() => loadCompany({ quiet: true }), featureEnabled("realtime_events") ? 15000 : 3000));
} else {
  $("world-fallback").classList.remove("hidden");
}
