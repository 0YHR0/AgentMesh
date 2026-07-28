const $ = (id) => document.getElementById(id);
const TERMINAL_TASKS = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const ACTIVE_RUNS = new Set(["READY", "RUNNING", "PAUSE_REQUESTED", "PAUSED"]);
const STORAGE_LANGUAGE = "agentmesh-language";
const STORAGE_MOTION = "agentmesh-world-reduced-motion";
const EMPLOYEE_COLORS = ["#35e7ff", "#a977ff", "#ffca68", "#71f6a5", "#ff7189", "#62a8ff", "#ff9d68"];
const WORLD_WIDTH = 3328;
const WORLD_HEIGHT = 1920;
const CAMERA_MIN_ZOOM = 0.36;
const CAMERA_MAX_ZOOM = 1.15;
const CAMERA_DEFAULT_ZOOM = 0.58;
const MAX_VISIBLE_EMPLOYEES = 50;

const COPY = {
  en: {
    tasks: "Company tasks", employees: "Employees", working: "Working", blocked: "Blocked",
    waiting: "Waiting", idle: "Idle", searchTasks: "Search tasks", liveProjection: "Live runtime projection",
    officeTitle: "Your AI company", research: "Research", analysis: "Analysis", engineering: "Engineering",
    operations: "Review & Ops", handoffHub: "HANDOFF HUB", emptyOffice: "The office is waiting for employees",
    emptyOfficeHint: "Enable Agent Registry or run a task to populate the company.",
    selectedMission: "Selected mission", runtimeState: "Runtime state", collaboration: "Collaboration",
    inspectConsole: "Inspect in Console", selectEmployee: "Select an employee",
    selectEmployeeHint: "Click a character to inspect its real Agent definition, version and current work.",
    currentWork: "Current work", realConfiguration: "Real configuration", version: "Version",
    lifecycle: "Lifecycle", capabilities: "Capabilities", tools: "Tools",
    configureEmployee: "Configure employee", connection: "Connection",
    tokenHint: "When Identity/RBAC is enabled, enter a Bearer token. It stays only in this browser tab.",
    cancel: "Cancel", saveReconnect: "Save & reconnect", connecting: "Connecting to company…",
    online: "Company systems online", degraded: "Company data unavailable", noTasks: "No tasks yet",
    noMission: "No active mission", noWork: "No assigned work", handoffs: "{count} handoffs",
    taskProgress: "{done}/{total} units", updated: "Updated {value}", statusIdle: "IDLE · AT DESK",
    statusWorking: "WORKING · {value}", statusWaiting: "WAITING · {value}",
    statusBlocked: "BLOCKED · {value}", statusComplete: "COMPLETE · RETURNING TO DESK",
    handoff: "{source} handed context to {target}", unknownRole: "General employee",
    agentRegistryDisabled: "Agent Registry is disabled; employees are projected from Task Runs.",
    languageName: "中文", connectionButton: "Connection", consoleButton: "Control Console",
    campusMap: "AGENTMESH CAMPUS", moveMap: "move", dragMap: "pan", zoomMap: "zoom",
    minimap: "MINIMAP", centerMap: "Center map", focusEmployee: "Focus selected employee",
    campusView: "View", employeePicker: "Employee", allCampus: "Whole campus",
    motionToggle: "Toggle reduced motion", motionOn: "REDUCED", motionOff: "MOTION",
    soundOn: "Ambient sound on", soundOff: "Ambient sound off", mapFallback: "Using compatibility map"
  },
  "zh-CN": {
    tasks: "公司任务", employees: "员工", working: "工作中", blocked: "阻塞",
    waiting: "等待", idle: "空闲", searchTasks: "搜索任务", liveProjection: "实时运行状态投影",
    officeTitle: "你的 AI 公司", research: "研究部", analysis: "分析部", engineering: "工程部",
    operations: "评审与运营部", handoffHub: "任务交接中心", emptyOffice: "办公室正在等待员工",
    emptyOfficeHint: "开启 Agent Registry 或运行一个任务即可看到员工。",
    selectedMission: "当前任务", runtimeState: "运行状态", collaboration: "协作",
    inspectConsole: "在控制台查看", selectEmployee: "选择一名员工",
    selectEmployeeHint: "点击角色，查看其真实 Agent 定义、版本和当前工作。",
    currentWork: "当前工作", realConfiguration: "真实配置", version: "版本",
    lifecycle: "生命周期", capabilities: "能力", tools: "工具",
    configureEmployee: "配置员工", connection: "连接设置",
    tokenHint: "启用 Identity/RBAC 时填写 Bearer Token。它只保存在当前浏览器标签页。",
    cancel: "取消", saveReconnect: "保存并重连", connecting: "正在连接公司…",
    online: "公司系统在线", degraded: "公司数据暂时不可用", noTasks: "还没有任务",
    noMission: "没有活跃任务", noWork: "暂无分配工作", handoffs: "{count} 次交接",
    taskProgress: "{done}/{total} 个单元", updated: "{value}前更新", statusIdle: "空闲 · 在工位",
    statusWorking: "工作中 · {value}", statusWaiting: "等待 · {value}",
    statusBlocked: "阻塞 · {value}", statusComplete: "已完成 · 返回工位",
    handoff: "{source} 已向 {target} 交接上下文", unknownRole: "通用员工",
    agentRegistryDisabled: "Agent Registry 未开启；当前员工来自真实 Task Run 投影。",
    languageName: "EN", connectionButton: "连接设置", consoleButton: "控制台",
    campusMap: "AGENTMESH 园区", moveMap: "移动", dragMap: "拖动", zoomMap: "缩放",
    minimap: "小地图", centerMap: "回到地图中心", focusEmployee: "聚焦选中员工",
    campusView: "视图", employeePicker: "员工", allCampus: "整个园区",
    motionToggle: "切换减少动态效果", motionOn: "低动态", motionOff: "动态",
    soundOn: "环境音已开启", soundOff: "环境音已关闭", mapFallback: "正在使用兼容地图"
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
  interactions: [],
  streamCursor: sessionStorage.getItem("agentmesh-world-cursor") || "$",
  animatedHandoffs: new Set(),
  campus: AgentMeshWorld.compileCampus(AgentMeshWorld.fallbackCampus),
  campusFallback: true,
  selectedZoneId: "campus",
  reducedMotion: localStorage.getItem(STORAGE_MOTION) === "true"
    || (localStorage.getItem(STORAGE_MOTION) == null && window.matchMedia("(prefers-reduced-motion: reduce)").matches),
  pollTimer: null,
  streamGeneration: 0,
  loadInFlight: false
};

let officeScene = null;
let officeGame = null;

class OfficeScene extends Phaser.Scene {
  constructor() {
    super({ key: "AgentMeshOffice" });
    this.employeeObjects = new Map();
    this.routePackets = [];
    this.routeTweens = [];
    this.dragStart = null;
    this.lastHudState = "";
    this.currentTaskId = null;
    this.environmentTweens = [];
    this.ready = false;
  }

  create() {
    officeScene = this;
    this.createEmployeeAnimations();
    this.add.rectangle(
      WORLD_WIDTH / 2, WORLD_HEIGHT / 2, WORLD_WIDTH, WORLD_HEIGHT, 0x07101c, 0.04
    ).setDepth(1);
    this.createZoneLabels();
    this.routeGraphics = this.add.graphics().setDepth(3);
    this.packetLayer = this.add.container(0, 0).setDepth(4);
    this.employeeLayer = this.add.container(0, 0).setDepth(6);
    this.clusterLayer = this.add.container(0, 0).setDepth(5);
    this.createEnvironment();
    this.configureCamera();
    this.ready = true;
    this.sync(state.employees, selectedTask());
  }

  createEmployeeAnimations() {
    if (!this.textures.exists("employee-sprite")) this.createEmployeeTexture();
    if (!this.textures.exists("employee-sprite")) return;
    const definitions = {
      down: [0, 1],
      right: [2, 3],
      left: [4, 5],
      up: [6, 7]
    };
    for (const [direction, frames] of Object.entries(definitions)) {
      const key = `employee-walk-${direction}`;
      if (this.anims.exists(key)) continue;
      this.anims.create({
        key,
        frames: frames.map((frame) => ({ key: "employee-sprite", frame })),
        frameRate: 6,
        repeat: -1
      });
    }
  }

  createEmployeeTexture() {
    const sheet = this.textures.createCanvas("employee-sprite", 256, 32);
    const context = sheet.context;
    context.imageSmoothingEnabled = false;
    const rectangle = (frame, x, y, width, height, fill) => {
      context.fillStyle = fill;
      context.fillRect(frame * 32 + x, y, width, height);
    };
    const employee = (frame, direction, step) => {
      const lift = step ? 1 : 0;
      rectangle(frame, 8, 28, 17, 2, "#02060c");
      rectangle(frame, 8, 20 - lift, 8, 9, "#18263d");
      rectangle(frame, 18, 20, 7, 9 - lift, "#223451");
      rectangle(frame, 7, 14 - lift, 18, 10, "#35e7ff");
      rectangle(frame, 5, 16 - lift, 4, 7, "#1fb0c9");
      rectangle(frame, 23, 16 - lift, 4, 7, "#1fb0c9");
      rectangle(frame, 10, 5 - lift, 12, 10, "#ffc99f");
      if (direction === "up") {
        rectangle(frame, 8, 3 - lift, 16, 11, "#26334d");
        rectangle(frame, 10, 13 - lift, 12, 3, "#26334d");
      } else {
        rectangle(frame, 8, 3 - lift, 16, 5, "#26334d");
        if (direction === "down") {
          rectangle(frame, 12, 10 - lift, 2, 2, "#172239");
          rectangle(frame, 18, 10 - lift, 2, 2, "#172239");
        } else {
          rectangle(frame, direction === "right" ? 19 : 11, 10 - lift, 2, 2, "#172239");
        }
      }
    };
    ["down", "right", "left", "up"].forEach((direction, directionIndex) => {
      employee(directionIndex * 2, direction, 0);
      employee(directionIndex * 2 + 1, direction, 1);
    });
    for (let frame = 0; frame < 8; frame += 1) sheet.add(frame, 0, frame * 32, 0, 32, 32);
    sheet.refresh();
  }

  createZoneLabels() {
    const labelStyle = {
      color: "#dff9ff",
      fontFamily: "monospace",
      fontSize: "18px",
      fontStyle: "bold",
      backgroundColor: "rgba(4,12,25,0.82)",
      padding: { x: 12, y: 7 }
    };
    this.zoneLabels = {
      research: this.add.text(WORLD_WIDTH * 0.05, WORLD_HEIGHT * 0.06, "", labelStyle),
      analysis: this.add.text(WORLD_WIDTH * 0.95, WORLD_HEIGHT * 0.06, "", labelStyle).setOrigin(1, 0),
      engineering: this.add.text(WORLD_WIDTH * 0.05, WORLD_HEIGHT * 0.94, "", labelStyle).setOrigin(0, 1),
      operations: this.add.text(WORLD_WIDTH * 0.95, WORLD_HEIGHT * 0.94, "", labelStyle).setOrigin(1, 1),
      hub: this.add.text(WORLD_WIDTH * 0.5, WORLD_HEIGHT * 0.55, "", {
        ...labelStyle,
        color: "#72efff",
        fontSize: "15px",
        letterSpacing: 4
      }).setOrigin(0.5)
    };
    Object.values(this.zoneLabels).forEach((label) => label.setDepth(2).setStroke("#02050c", 3));
    this.updateZoneLabels();
  }

  createEnvironment() {
    const points = [
      [WORLD_WIDTH * 0.5, WORLD_HEIGHT * 0.48, 0x35e7ff],
      [WORLD_WIDTH * 0.18, WORLD_HEIGHT * 0.14, 0xa977ff],
      [WORLD_WIDTH * 0.82, WORLD_HEIGHT * 0.14, 0x35e7ff],
      [WORLD_WIDTH * 0.18, WORLD_HEIGHT * 0.86, 0x35e7ff],
      [WORLD_WIDTH * 0.82, WORLD_HEIGHT * 0.86, 0xa977ff]
    ];
    this.environmentNodes = points.map(([x, y, color]) => {
      const glow = this.add.circle(x, y, 16, color, 0.12).setStrokeStyle(3, color, 0.7).setDepth(2);
      const core = this.add.rectangle(x, y, 8, 8, color, 0.9).setDepth(2);
      this.environmentTweens.push(this.tweens.add({
        targets: [glow, core],
        alpha: { from: 0.3, to: 1 },
        scale: { from: 0.8, to: 1.18 },
        duration: 1250,
        yoyo: true,
        repeat: -1,
        paused: state.reducedMotion
      }));
      return { glow, core };
    });
  }

  setReducedMotion(enabled) {
    this.environmentTweens.forEach((tween) => enabled ? tween.pause() : tween.resume());
    if (enabled) {
      this.routeTweens.forEach((tween) => tween.pause());
      for (const value of this.employeeObjects.values()) this.cancelMovement(value);
    } else {
      this.routeTweens.forEach((tween) => tween.resume());
    }
  }

  updateZoneLabels() {
    if (!this.zoneLabels) return;
    this.zoneLabels.research.setText(`01  ${t("research").toUpperCase()}`);
    this.zoneLabels.analysis.setText(`02  ${t("analysis").toUpperCase()}`);
    this.zoneLabels.engineering.setText(`03  ${t("engineering").toUpperCase()}`);
    this.zoneLabels.operations.setText(`04  ${t("operations").toUpperCase()}`);
    this.zoneLabels.hub.setText(t("handoffHub"));
  }

  configureCamera() {
    const camera = this.cameras.main;
    camera.setBounds(0, 0, WORLD_WIDTH, WORLD_HEIGHT);
    camera.setZoom(CAMERA_DEFAULT_ZOOM);
    camera.centerOn(WORLD_WIDTH / 2, WORLD_HEIGHT / 2);
    camera.roundPixels = true;
    this.moveKeys = this.input.keyboard.addKeys({
      up: Phaser.Input.Keyboard.KeyCodes.W,
      down: Phaser.Input.Keyboard.KeyCodes.S,
      left: Phaser.Input.Keyboard.KeyCodes.A,
      right: Phaser.Input.Keyboard.KeyCodes.D
    });
    this.cursorKeys = this.input.keyboard.createCursorKeys();
    this.input.keyboard.on("keydown", (event) => {
      if (event.repeat) return;
      const nudge = 56 / camera.zoom;
      if (["KeyA", "ArrowLeft"].includes(event.code)) camera.scrollX -= nudge;
      if (["KeyD", "ArrowRight"].includes(event.code)) camera.scrollX += nudge;
      if (["KeyW", "ArrowUp"].includes(event.code)) camera.scrollY -= nudge;
      if (["KeyS", "ArrowDown"].includes(event.code)) camera.scrollY += nudge;
    });
    this.input.on("pointerdown", (pointer, gameObjects) => {
      if (!pointer.leftButtonDown() || gameObjects.length) return;
      this.dragStart = { x: pointer.x, y: pointer.y, scrollX: camera.scrollX, scrollY: camera.scrollY };
      $("office-stage").classList.add("dragging");
    });
    this.input.on("pointermove", (pointer) => {
      if (!this.dragStart || !pointer.isDown) return;
      camera.setScroll(
        this.dragStart.scrollX - (pointer.x - this.dragStart.x) / camera.zoom,
        this.dragStart.scrollY - (pointer.y - this.dragStart.y) / camera.zoom
      );
    });
    this.input.on("pointerup", () => this.stopDragging());
    this.input.on("pointerupoutside", () => this.stopDragging());
    this.input.on("wheel", (_pointer, _objects, _deltaX, deltaY) => {
      this.changeZoom(deltaY > 0 ? -0.08 : 0.08);
    });
  }

  stopDragging() {
    this.dragStart = null;
    $("office-stage").classList.remove("dragging");
  }

  update(_time, delta) {
    if (!this.ready) return;
    const camera = this.cameras.main;
    const speed = (760 * delta / 1000) / camera.zoom;
    const left = this.moveKeys.left.isDown || this.cursorKeys.left.isDown;
    const right = this.moveKeys.right.isDown || this.cursorKeys.right.isDown;
    const up = this.moveKeys.up.isDown || this.cursorKeys.up.isDown;
    const down = this.moveKeys.down.isDown || this.cursorKeys.down.isDown;
    if (left) camera.scrollX -= speed;
    if (right) camera.scrollX += speed;
    if (up) camera.scrollY -= speed;
    if (down) camera.scrollY += speed;
    this.updateCameraHud();
  }

  changeZoom(delta) {
    const camera = this.cameras.main;
    camera.setZoom(Phaser.Math.Clamp(camera.zoom + delta, CAMERA_MIN_ZOOM, CAMERA_MAX_ZOOM));
    this.updateCameraHud(true);
  }

  centerMap() {
    state.selectedZoneId = "campus";
    $("zone-select").value = "campus";
    this.cameras.main.centerOn(WORLD_WIDTH / 2, WORLD_HEIGHT / 2);
    this.cameras.main.setZoom(CAMERA_DEFAULT_ZOOM);
    this.updateCameraHud(true);
  }

  focusZone(zoneId) {
    state.selectedZoneId = zoneId;
    if (zoneId === "campus") {
      this.centerMap();
      return;
    }
    const zone = state.campus.zones.find((item) => item.id === zoneId);
    if (!zone) return;
    const camera = this.cameras.main;
    camera.centerOn(zone.x + zone.width / 2, zone.y + zone.height / 2);
    const zoom = Math.min(camera.width / (zone.width + 160), camera.height / (zone.height + 160));
    camera.setZoom(Phaser.Math.Clamp(zoom, CAMERA_MIN_ZOOM, CAMERA_MAX_ZOOM));
    this.updateCameraHud(true);
  }

  focusEmployee(employeeId = state.selectedEmployeeId) {
    const employee = this.employeeObjects.get(employeeId);
    if (!employee) {
      this.centerMap();
      return;
    }
    this.cameras.main.centerOn(employee.container.x, employee.container.y);
    if (this.cameras.main.zoom < 0.68) this.cameras.main.setZoom(0.68);
    this.updateCameraHud(true);
  }

  centerAtRatio(x, y) {
    this.cameras.main.centerOn(
      Phaser.Math.Clamp(x, 0, 1) * WORLD_WIDTH,
      Phaser.Math.Clamp(y, 0, 1) * WORLD_HEIGHT
    );
    this.updateCameraHud(true);
  }

  updateCameraHud(force = false) {
    const camera = this.cameras.main;
    const view = camera.worldView;
    const signature = [
      Math.round(camera.midPoint.x), Math.round(camera.midPoint.y), Math.round(camera.zoom * 100)
    ].join(":");
    if (!force && signature === this.lastHudState) return;
    this.lastHudState = signature;
    $("camera-zoom").textContent = `${Math.round(camera.zoom * 100)}%`;
    const activeZone = AgentMeshWorld.zoneForPoint(state.campus, camera.midPoint);
    const zoneLabel = activeZone ? ` · ${activeZone.label}` : "";
    $("map-position").textContent = `X ${String(Math.round(camera.midPoint.x)).padStart(4, "0")} · Y ${String(Math.round(camera.midPoint.y)).padStart(4, "0")}${zoneLabel}`;
    $("world-map-layer").style.transform = `translate3d(${-camera.scrollX * camera.zoom}px, ${-camera.scrollY * camera.zoom}px, 0) scale(${camera.zoom})`;
    const minimap = $("minimap-viewport");
    minimap.style.left = `${Phaser.Math.Clamp(view.x / WORLD_WIDTH, 0, 1) * 100}%`;
    minimap.style.top = `${Phaser.Math.Clamp(view.y / WORLD_HEIGHT, 0, 1) * 100}%`;
    minimap.style.width = `${Phaser.Math.Clamp(view.width / WORLD_WIDTH, 0.03, 1) * 100}%`;
    minimap.style.height = `${Phaser.Math.Clamp(view.height / WORLD_HEIGHT, 0.03, 1) * 100}%`;
  }

  sync(employees, task) {
    if (!this.ready) return;
    if (this.currentTaskId && this.currentTaskId !== task?.id) this.cancelAllMovements();
    this.currentTaskId = task?.id || null;
    this.updateZoneLabels();
    const visible = this.visibleEmployees(employees);
    const activeIds = new Set(visible.map((employee) => employee.id));
    for (const [id, value] of this.employeeObjects) {
      if (!activeIds.has(id)) {
        value.container.destroy(true);
        this.employeeObjects.delete(id);
      }
    }
    for (const employee of visible) {
      let value = this.employeeObjects.get(employee.id);
      if (!value) {
        value = this.createEmployee(employee);
        this.employeeObjects.set(employee.id, value);
      }
      this.updateEmployee(value, employee);
    }
    this.renderClusters(employees, visible);
    this.drawRoutes(collaborationEdges(task));
  }

  visibleEmployees(employees) {
    if (employees.length <= MAX_VISIBLE_EMPLOYEES) return employees;
    const selected = employees.find((employee) => employee.id === state.selectedEmployeeId);
    const result = employees.slice(0, MAX_VISIBLE_EMPLOYEES);
    if (selected && !result.includes(selected)) result[result.length - 1] = selected;
    return result;
  }

  renderClusters(allEmployees, visibleEmployees) {
    this.clusterLayer.removeAll(true);
    if (allEmployees.length <= visibleEmployees.length) return;
    const visibleIds = new Set(visibleEmployees.map((employee) => employee.id));
    const grouped = new Map();
    for (const employee of allEmployees) {
      if (visibleIds.has(employee.id)) continue;
      grouped.set(employee.department, (grouped.get(employee.department) || 0) + 1);
    }
    for (const [department, count] of grouped) {
      const zone = state.campus.zones.find((item) => item.id === department);
      if (!zone) continue;
      const marker = this.add.container(zone.x + zone.width - 80, zone.y + 72);
      marker.add([
        this.add.circle(0, 0, 28, 0x0a1424, 0.94).setStrokeStyle(3, 0x35e7ff, 0.85),
        this.add.text(0, -1, `+${count}`, { color: "#dffcff", fontFamily: "monospace", fontSize: "16px", fontStyle: "bold" }).setOrigin(0.5)
      ]);
      this.clusterLayer.add(marker);
    }
  }

  createEmployee(employee) {
    const point = this.worldPoint(employee.home);
    const color = Phaser.Display.Color.HexStringToColor(employee.color).color;
    const container = this.add.container(point.x, point.y)
      .setScale(1.7).setDepth(6 + point.y / 1000);
    const selection = this.add.ellipse(0, 7, 50, 22, 0x35e7ff, 0.12)
      .setStrokeStyle(2, 0x35e7ff, 0.95).setVisible(false);
    const shadow = this.add.ellipse(0, 8, 34, 12, 0x000000, 0.52);
    const leftLeg = this.add.rectangle(-6, 0, 8, 16, 0x18263d).setOrigin(0.5, 0);
    const rightLeg = this.add.rectangle(6, 0, 8, 16, 0x18263d).setOrigin(0.5, 0);
    const body = this.add.rectangle(0, -10, 28, 24, color).setStrokeStyle(2, 0x07101c, 0.7);
    const head = this.add.rectangle(0, -29, 20, 19, 0xffc99f).setStrokeStyle(2, 0x5e3c38, 0.7);
    const hair = this.add.rectangle(0, -38, 24, 7, 0x26334d);
    const leftEye = this.add.rectangle(-4, -29, 2, 2, 0x172239);
    const rightEye = this.add.rectangle(4, -29, 2, 2, 0x172239);
    const hasSprite = this.textures.exists("employee-sprite");
    const sprite = hasSprite ? this.add.sprite(0, -10, "employee-sprite", 0).setScale(2) : null;
    const shirtBadge = hasSprite
      ? this.add.rectangle(0, -6, 7, 4, color).setStrokeStyle(1, 0x07101c, 0.8)
      : null;
    for (const item of [shadow, leftLeg, rightLeg, body, head, hair, leftEye, rightEye]) {
      item.setVisible(!hasSprite);
    }
    const statusBackground = this.add.rectangle(0, -61, 84, 19, 0x07101e, 0.94)
      .setStrokeStyle(1, 0x52667c, 1);
    const statusText = this.add.text(0, -61, "", {
      color: "#d8e5f0", fontFamily: "monospace", fontSize: "9px", fontStyle: "bold"
    }).setOrigin(0.5);
    const nameText = this.add.text(0, 20, employee.name, {
      color: "#f0fbff", fontFamily: "monospace", fontSize: "9px",
      backgroundColor: "rgba(4,9,17,0.82)", padding: { x: 3, y: 1 }
    }).setOrigin(0.5, 0);
    container.add([
      selection, shadow, leftLeg, rightLeg, body, head, hair, leftEye, rightEye,
      ...(sprite ? [sprite, shirtBadge] : []),
      statusBackground, statusText, nameText
    ]);
    container.setSize(92, 92).setInteractive(
      new Phaser.Geom.Rectangle(-46, -70, 92, 100),
      Phaser.Geom.Rectangle.Contains
    );
    container.on("pointerover", () => container.setScale(1.84));
    container.on("pointerout", () => container.setScale(1.7));
    container.on("pointerdown", () => selectEmployee(employee.id));
    this.employeeLayer.add(container);
    return {
      container, selection, leftLeg, rightLeg, body, statusBackground, statusText,
      nameText, leftEye, rightEye, hair, sprite, employee, walking: false,
      direction: "down", movementToken: 0
    };
  }

  updateEmployee(value, employee) {
    value.employee = employee;
    value.selection.setVisible(employee.id === state.selectedEmployeeId);
    value.nameText.setText(employee.name);
    value.statusText.setText(this.compactStatus(employee.state));
    value.statusBackground.setDisplaySize(
      Math.max(76, Math.min(150, value.statusText.width + 12)),
      19
    );
    const statusColors = {
      idle: 0x71839a, working: 0x35e7ff, waiting: 0xffca68,
      blocked: 0xff7189, complete: 0x71f6a5
    };
    const statusColor = statusColors[employee.state.key] || statusColors.idle;
    value.statusBackground.setStrokeStyle(1, statusColor, 1);
    value.statusText.setColor(`#${statusColor.toString(16).padStart(6, "0")}`);
    if (!value.walking) {
      const point = this.worldPoint(employee.home);
      value.container.setPosition(point.x, point.y).setDepth(6 + point.y / 1000);
    }
  }

  compactStatus(employeeState) {
    const labels = {
      idle: state.language === "zh-CN" ? "空闲" : "IDLE",
      working: state.language === "zh-CN" ? "工作中" : "WORKING",
      waiting: state.language === "zh-CN" ? "等待" : "WAITING",
      blocked: state.language === "zh-CN" ? "阻塞" : "BLOCKED",
      complete: state.language === "zh-CN" ? "已完成" : "COMPLETE"
    };
    return labels[employeeState.key] || labels.idle;
  }

  drawRoutes(edges) {
    this.routeGraphics.clear();
    this.routeTweens.forEach((tween) => tween.remove());
    this.routeTweens = [];
    this.routePackets.forEach((packet) => packet.destroy());
    this.routePackets = [];
    for (const edge of edges) {
      const source = this.employeeObjects.get(employeeByName(edge.source)?.id);
      const target = this.employeeObjects.get(employeeByName(edge.target)?.id);
      if (!source || !target) continue;
      const start = new Phaser.Math.Vector2(source.container.x, source.container.y);
      const end = new Phaser.Math.Vector2(target.container.x, target.container.y);
      const control = new Phaser.Math.Vector2(WORLD_WIDTH / 2, WORLD_HEIGHT / 2);
      const color = String(edge.status).includes("PENDING") ? 0xffca68 : 0x35e7ff;
      this.routeGraphics.lineStyle(4, color, 0.56);
      this.routeGraphics.beginPath();
      this.routeGraphics.moveTo(start.x, start.y);
      this.routeGraphics.quadraticBezierTo(control.x, control.y, end.x, end.y);
      this.routeGraphics.strokePath();
      const packet = this.add.rectangle(start.x, start.y, 12, 12, 0xf3ffff)
        .setStrokeStyle(2, color, 1);
      this.packetLayer.add(packet);
      this.routePackets.push(packet);
      const curve = new Phaser.Curves.QuadraticBezier(start, control, end);
      const progress = { value: 0 };
      const tween = this.tweens.add({
        targets: progress,
        value: 1,
        duration: 2800,
        repeat: state.reducedMotion ? 0 : -1,
        delay: hash(edge.id) % 900,
        onUpdate: () => {
          const point = curve.getPoint(progress.value);
          packet.setPosition(point.x, point.y);
        }
      });
      this.routeTweens.push(tween);
    }
  }

  animateHandoff(handoff, sourceEmployee, targetEmployee) {
    if (!this.ready) return;
    const source = this.employeeObjects.get(sourceEmployee.id);
    const target = this.employeeObjects.get(targetEmployee.id);
    if (!source || !target || source.walking) return;
    if (state.reducedMotion) {
      showHandoffCard(sourceEmployee.name, targetEmployee.name);
      return;
    }
    const destination = { x: target.container.x - 48, y: target.container.y + 12 };
    const route = AgentMeshWorld.findPath(
      state.campus,
      { x: source.container.x, y: source.container.y },
      destination
    );
    if (!route.length) {
      showHandoffCard(sourceEmployee.name, targetEmployee.name);
      return;
    }
    source.walking = true;
    source.container.setDepth(20);
    this.walkCycle(source, true);
    const token = ++source.movementToken;
    this.walkRoute(source, route, token, () => {
      showHandoffCard(sourceEmployee.name, targetEmployee.name);
      this.time.delayedCall(1000, () => {
        if (token !== source.movementToken) return;
        const home = this.worldPoint(sourceEmployee.home);
        const returnRoute = AgentMeshWorld.findPath(
          state.campus,
          { x: source.container.x, y: source.container.y },
          home
        );
        this.walkRoute(source, returnRoute, token, () => {
          source.walking = false;
          source.container.setPosition(home.x, home.y).setDepth(6 + home.y / 1000);
          this.walkCycle(source, false);
          this.faceDirection(source, "down");
        });
      });
    });
  }

  walkRoute(value, points, token, onComplete, index = 0) {
    if (token !== value.movementToken || index >= points.length) {
      if (token === value.movementToken) onComplete();
      return;
    }
    const point = points[index];
    const dx = point.x - value.container.x;
    const dy = point.y - value.container.y;
    this.faceDirection(value, Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? "left" : "right") : (dy < 0 ? "up" : "down"));
    const distance = Math.hypot(dx, dy);
    value.movementTween = this.tweens.add({
      targets: value.container,
      x: point.x,
      y: point.y,
      duration: Math.max(120, distance * 2.1),
      ease: "Linear",
      onUpdate: () => value.container.setDepth(20 + value.container.y / 1000),
      onComplete: () => this.walkRoute(value, points, token, onComplete, index + 1)
    });
  }

  faceDirection(value, direction) {
    value.direction = direction;
    if (value.sprite) {
      const firstFrame = { down: 0, right: 2, left: 4, up: 6 }[direction];
      if (value.walking && !state.reducedMotion) value.sprite.play(`employee-walk-${direction}`, true);
      else {
        value.sprite.stop();
        value.sprite.setFrame(firstFrame);
      }
      return;
    }
    const eyeVisible = direction !== "up";
    value.leftEye.setVisible(eyeVisible);
    value.rightEye.setVisible(eyeVisible);
    value.hair.y = direction === "up" ? -31 : -38;
  }

  cancelMovement(value) {
    value.movementToken += 1;
    value.movementTween?.stop();
    value.movementTween = null;
    value.walking = false;
    this.walkCycle(value, false);
    const home = this.worldPoint(value.employee.home);
    value.container.setPosition(home.x, home.y).setDepth(6 + home.y / 1000);
    this.faceDirection(value, "down");
  }

  cancelAllMovements() {
    for (const value of this.employeeObjects.values()) {
      if (value.walking) this.cancelMovement(value);
    }
  }

  walkCycle(value, enabled) {
    if (value.sprite) {
      value.walking = enabled;
      this.faceDirection(value, value.direction);
      return;
    }
    this.tweens.killTweensOf([value.leftLeg, value.rightLeg]);
    value.leftLeg.y = 0;
    value.rightLeg.y = 0;
    if (!enabled) return;
    this.tweens.add({ targets: value.leftLeg, y: -4, duration: 150, yoyo: true, repeat: -1 });
    this.tweens.add({ targets: value.rightLeg, y: -4, duration: 150, yoyo: true, repeat: -1, delay: 150 });
  }

  worldPoint(percent) {
    return { x: percent.x * WORLD_WIDTH / 100, y: percent.y * WORLD_HEIGHT / 100 };
  }
}

function initWorldGame() {
  officeGame = new Phaser.Game({
    // The Office is an operational visualization rather than a GPU-heavy game.
    // Canvas keeps it compatible with remote desktops, software-rendered
    // browsers, and small operator machines where WebGL texture uploads may fail.
    type: Phaser.CANVAS,
    width: 960,
    height: 540,
    parent: "office-game",
    transparent: true,
    pixelArt: true,
    roundPixels: true,
    render: { antialias: false, pixelArt: true, roundPixels: true },
    scale: { mode: Phaser.Scale.RESIZE, autoCenter: Phaser.Scale.NO_CENTER },
    scene: OfficeScene,
    callbacks: {
      postBoot: (game) => { officeScene = game.scene.getScene("AgentMeshOffice"); }
    }
  });
}

function t(key, values = {}) {
  let result = COPY[state.language][key] || COPY.en[key] || key;
  Object.entries(values).forEach(([name, value]) => { result = result.replace(`{${name}}`, value); });
  return result;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function featureEnabled(name) { return state.features.get(name) === true; }
function statusClass(value) { return String(value || "").toLowerCase().replace(/[^a-z0-9_-]/g, "-"); }
function shortId(value) { return String(value || "—").slice(0, 8); }
function hash(value) {
  let result = 2166136261;
  for (const char of String(value)) result = Math.imul(result ^ char.charCodeAt(0), 16777619);
  return result >>> 0;
}
function age(value) {
  if (!value) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.message || payload?.detail || `${response.status} ${response.statusText}`);
  return payload;
}

async function loadFeatures() {
  const payload = await api("/api/v1/features");
  state.features = new Map(payload.features.map((item) => [item.name, item.enabled]));
}

async function loadCompany({ quiet = false } = {}) {
  if (state.loadInFlight) return;
  state.loadInFlight = true;
  try {
    if (!state.features.size) await loadFeatures();
    const taskPayload = await api("/api/v1/tasks?limit=50&offset=0");
    state.tasks = taskPayload.items;
    if (featureEnabled("agent_registry_management")) {
      const agentPayload = await api("/api/v1/agents?limit=100&offset=0");
      state.agents = agentPayload.items;
    } else {
      state.agents = [];
    }
    state.employees = buildEmployees();
    if (!state.selectedTaskId || !state.tasks.some((task) => task.id === state.selectedTaskId)) {
      state.selectedTaskId = state.tasks.find((task) => !TERMINAL_TASKS.has(task.status))?.id || state.tasks[0]?.id || null;
    }
    if (state.selectedTaskId && featureEnabled("activity_timeline")) {
      try {
        state.interactions = (await api(`/api/v1/tasks/${state.selectedTaskId}/interactions?limit=100`)).items;
      } catch {
        state.interactions = [];
      }
    } else {
      state.interactions = [];
    }
    setConnection(true);
    render();
    animateLatestHandoff();
  } catch (error) {
    setConnection(false);
    if (!quiet) toast(error.message, true);
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
    for (const subtask of task.subtasks) {
      if (subtask.preferred_agent_id && !definitions.has(subtask.preferred_agent_id)) {
        definitions.set(subtask.preferred_agent_id, syntheticAgent(subtask.preferred_agent_id));
      }
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
      tags: agent.tags || [],
      versions: agent.versions || [],
      department,
      color: EMPLOYEE_COLORS[hash(agent.name) % EMPLOYEE_COLORS.length],
      home: homePosition(department, index, agent.name),
      assignment,
      state: employeeState(assignment)
    };
  }).sort((left, right) => left.name.localeCompare(right.name));
}

function syntheticAgent(name) {
  return { id: `runtime:${name}`, name, description: "", lifecycle: "RUNTIME", tags: [], versions: [] };
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
  const version = agent.versions?.find((item) => item.id === agent.default_version_id) || agent.versions?.find((item) => item.status === "PUBLISHED") || agent.versions?.[0];
  const words = `${agent.name} ${agent.description} ${(agent.tags || []).join(" ")} ${version?.role || ""} ${(version?.declared_capabilities || []).join(" ")}`.toLowerCase();
  if (/research|investigat|source|knowledge|search/.test(words)) return "research";
  if (/analy|data|finance|metric|insight/.test(words)) return "analysis";
  if (/engineer|develop|code|tool|system|build/.test(words)) return "engineering";
  if (/review|supervis|operat|synth|approv|manager/.test(words)) return "operations";
  return ["research", "analysis", "engineering", "operations"][hash(agent.name) % 4];
}

const HOMES = {
  research: [[18, 23], [31, 23], [24, 36], [36, 34], [14, 38]],
  analysis: [[67, 22], [80, 23], [70, 36], [82, 37], [62, 34]],
  engineering: [[17, 67], [30, 66], [23, 80], [37, 78], [13, 81]],
  operations: [[65, 67], [79, 66], [69, 80], [82, 80], [61, 78]]
};

function homePosition(department, index, name) {
  const options = HOMES[department];
  const base = options[(index + hash(name)) % options.length];
  const cycle = Math.floor(index / options.length);
  return { x: base[0] + cycle * 2, y: base[1] + cycle };
}

function employeeState(assignment) {
  if (!assignment) return { key: "idle", label: t("statusIdle") };
  const { task, run, subtask } = assignment;
  const detail = subtask?.key || shortId(task.id);
  if (["FAILED", "CANCELLED"].includes(run.status) || ["FAILED", "CANCELLED"].includes(task.status)) {
    return { key: "blocked", label: t("statusBlocked", { value: detail }) };
  }
  if (["PAUSED", "PAUSE_REQUESTED"].includes(run.status) || ["PAUSED", "WAITING_APPROVAL"].includes(task.status)) {
    return { key: "waiting", label: t("statusWaiting", { value: detail }) };
  }
  if (["READY", "RUNNING"].includes(run.status)) {
    return { key: "working", label: t("statusWorking", { value: detail }) };
  }
  if (run.status === "SUCCEEDED") return { key: "complete", label: t("statusComplete") };
  return { key: "idle", label: t("statusIdle") };
}

function render() {
  applyLanguage();
  renderTaskList();
  renderOffice();
  renderWorldSelectors();
  renderMissionStrip();
  renderInspector();
}

function applyLanguage() {
  document.documentElement.lang = state.language;
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => { node.title = t(node.dataset.i18nTitle); });
  $("language-toggle").textContent = t("languageName");
  $("connection-button").textContent = t("connectionButton");
  document.querySelector(".top-actions a.primary").textContent = t("consoleButton");
  $("motion-toggle").textContent = state.reducedMotion ? t("motionOn") : t("motionOff");
  $("motion-toggle").setAttribute("aria-pressed", String(state.reducedMotion));
}

function renderWorldSelectors() {
  const previousZone = state.selectedZoneId;
  $("zone-select").innerHTML = [
    `<option value="campus">${escapeHtml(t("allCampus"))}</option>`,
    ...state.campus.zones
      .filter((zone) => zone.id !== "hub")
      .map((zone) => `<option value="${escapeHtml(zone.id)}">${escapeHtml(t(zone.id) || zone.label)}</option>`)
  ].join("");
  $("zone-select").value = previousZone;
  const previousEmployee = state.selectedEmployeeId || "";
  $("employee-picker").innerHTML = [
    '<option value="">—</option>',
    ...state.employees.map((employee) => (
      `<option value="${escapeHtml(employee.id)}">${escapeHtml(employee.name)} · ${escapeHtml(t(employee.department))}</option>`
    ))
  ].join("");
  $("employee-picker").value = previousEmployee;
}

function renderTaskList() {
  const query = $("task-search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((task) => `${task.objective} ${task.status} ${task.project_id}`.toLowerCase().includes(query));
  $("task-list").innerHTML = tasks.length ? tasks.map((task) => {
    const units = task.subtasks.length || Math.max(1, task.runs.length);
    const done = task.subtasks.length ? task.subtasks.filter((item) => item.status === "COMPLETED").length : (task.status === "COMPLETED" ? 1 : 0);
    return `<button class="task-card ${task.id === state.selectedTaskId ? "active" : ""}" type="button" data-task-id="${task.id}">
      <strong>${escapeHtml(task.objective)}</strong>
      <div><span class="status-chip ${statusClass(task.status)}">${escapeHtml(task.status)}</span><span>${escapeHtml(t("taskProgress", { done, total: units }))}</span></div>
    </button>`;
  }).join("") : `<div class="inspector-empty"><p>${escapeHtml(t("noTasks"))}</p></div>`;
  document.querySelectorAll("[data-task-id]").forEach((node) => node.addEventListener("click", () => selectTask(node.dataset.taskId)));
}

async function selectTask(taskId) {
  state.selectedTaskId = taskId;
  await loadCompany({ quiet: true });
}

function renderOffice() {
  $("employee-count").textContent = state.employees.length;
  $("working-count").textContent = state.employees.filter((item) => item.state.key === "working").length;
  $("blocked-count").textContent = state.employees.filter((item) => item.state.key === "blocked").length;
  $("office-empty").classList.toggle("hidden", state.employees.length > 0);
  officeScene?.sync(state.employees, selectedTask());
}

async function loadCampusMap() {
  try {
    const response = await fetch("/console/assets/world-campus.json", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    state.campus = AgentMeshWorld.compileCampus(await response.json());
    state.campusFallback = false;
  } catch (error) {
    state.campus = AgentMeshWorld.compileCampus(AgentMeshWorld.fallbackCampus);
    state.campusFallback = true;
    console.warn("AgentMesh Office campus map fallback:", error);
  }
}

function selectEmployee(employeeId) {
  state.selectedEmployeeId = employeeId;
  renderOffice();
  renderInspector();
  $("inspector").classList.add("open");
}

function selectedTask() { return state.tasks.find((task) => task.id === state.selectedTaskId) || null; }
function employeeByName(name) { return state.employees.find((employee) => employee.name === name) || null; }

function taskAgentForSubtask(task, subtaskId) {
  const run = [...task.runs].reverse().find((item) => item.subtask_id === subtaskId);
  return run?.agent_id || task.subtasks.find((item) => item.id === subtaskId)?.preferred_agent_id || null;
}

function collaborationEdges(task) {
  if (!task) return [];
  const edges = task.handoffs.map((handoff) => ({
    id: `handoff:${handoff.id}`,
    source: handoff.source_agent_id,
    target: handoff.target_agent_id,
    status: handoff.status
  }));
  for (const target of task.subtasks) {
    for (const sourceKey of target.depends_on) {
      const source = task.subtasks.find((item) => item.key === sourceKey);
      if (!source) continue;
      const sourceAgent = taskAgentForSubtask(task, source.id);
      const targetAgent = taskAgentForSubtask(task, target.id);
      if (sourceAgent && targetAgent && sourceAgent !== targetAgent) {
        edges.push({ id: `dependency:${source.id}:${target.id}`, source: sourceAgent, target: targetAgent, status: target.status });
      }
    }
  }
  return [...new Map(edges.map((edge) => [edge.id, edge])).values()];
}

function renderMissionStrip() {
  const task = selectedTask();
  $("mission-title").textContent = task?.objective || t("officeTitle");
  $("selected-mission").textContent = task?.objective || t("noMission");
  $("selected-status").textContent = task?.status || "IDLE";
  $("selected-collaboration").textContent = t("handoffs", { count: task?.handoffs.length || 0 });
  $("open-task").href = task ? `/?task=${encodeURIComponent(task.id)}` : "/";
}

function renderInspector() {
  const employee = state.employees.find((item) => item.id === state.selectedEmployeeId);
  $("inspector-empty").classList.toggle("hidden", Boolean(employee));
  $("inspector-content").classList.toggle("hidden", !employee);
  if (!employee) return;
  const version = employee.versions.find((item) => item.id === employee.defaultVersionId)
    || employee.versions.find((item) => item.status === "PUBLISHED")
    || employee.versions[0];
  const assignment = employee.assignment;
  $("profile-avatar").style.setProperty("--shirt", employee.color);
  $("profile-department").textContent = t(employee.department);
  $("profile-name").textContent = employee.name;
  $("profile-role").textContent = version?.role || t("unknownRole");
  $("profile-status").className = `profile-status ${employee.state.key}`;
  $("profile-status").textContent = employee.state.label;
  $("profile-description").textContent = employee.description || version?.instructions?.slice(0, 240) || t("unknownRole");
  $("profile-work").innerHTML = assignment
    ? `<strong>${escapeHtml(assignment.subtask?.objective || assignment.task.objective)}</strong><span>${escapeHtml(assignment.task.status)} · Run ${escapeHtml(shortId(assignment.run.id))} · ${escapeHtml(assignment.run.status)}</span>`
    : `<span>${escapeHtml(t("noWork"))}</span>`;
  $("profile-version").textContent = version ? `v${version.semantic_version} · ${version.status}` : "Runtime only";
  $("profile-lifecycle").textContent = employee.lifecycle;
  $("profile-capabilities").textContent = version?.declared_capabilities?.join(", ") || "general.task";
  $("profile-tools").textContent = version?.tool_profile?.allowed_tools?.join(", ") || version?.tool_profile?.allowed_tool_keys?.join(", ") || "—";
}

function animateLatestHandoff() {
  const task = selectedTask();
  const handoff = task?.handoffs?.[task.handoffs.length - 1];
  if (!handoff || state.animatedHandoffs.has(handoff.id)) return;
  const source = employeeByName(handoff.source_agent_id);
  const target = employeeByName(handoff.target_agent_id);
  if (!source || !target) return;
  state.animatedHandoffs.add(handoff.id);
  officeScene?.animateHandoff(handoff, source, target);
}

function showHandoffCard(source, target) {
  $("handoff-layer").innerHTML = `<div class="handoff-card"><i></i><span>${escapeHtml(t("handoff", { source, target }))}</span></div>`;
  window.setTimeout(() => { $("handoff-layer").innerHTML = ""; }, 2200);
}

function setConnection(online) {
  const node = document.querySelector(".company-status");
  node.classList.toggle("online", online);
  node.classList.toggle("error", !online);
  $("company-status").textContent = online ? t("online") : t("degraded");
}

let toastTimer;
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = `toast show${error ? " error" : ""}`;
  toastTimer = window.setTimeout(() => { $("toast").className = "toast"; }, 3000);
}

let ambientAudio = null;
async function toggleAmbientSound() {
  if (ambientAudio) {
    ambientAudio.oscillators.forEach((oscillator) => oscillator.stop());
    await ambientAudio.context.close();
    ambientAudio = null;
    $("sound-toggle").classList.remove("active");
    $("sound-toggle").setAttribute("aria-pressed", "false");
    toast(t("soundOff"));
    return;
  }
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) {
    toast("Web Audio is unavailable.", true);
    return;
  }
  const context = new AudioContext();
  const gain = context.createGain();
  gain.gain.value = 0.012;
  gain.connect(context.destination);
  const oscillators = [55, 82.5].map((frequency, index) => {
    const oscillator = context.createOscillator();
    const filter = context.createBiquadFilter();
    oscillator.type = index ? "sine" : "triangle";
    oscillator.frequency.value = frequency;
    filter.type = "lowpass";
    filter.frequency.value = 180;
    oscillator.connect(filter);
    filter.connect(gain);
    oscillator.start();
    return oscillator;
  });
  ambientAudio = { context, oscillators };
  $("sound-toggle").classList.add("active");
  $("sound-toggle").setAttribute("aria-pressed", "true");
  toast(t("soundOn"));
}

function startUpdates() {
  clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(() => loadCompany({ quiet: true }), featureEnabled("realtime_events") ? 15000 : 3000);
  if (featureEnabled("realtime_events")) connectRealtime(++state.streamGeneration);
}

async function connectRealtime(generation) {
  const headers = { Accept: "text/event-stream", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), "Last-Event-ID": state.streamCursor };
  try {
    const response = await fetch("/api/v1/events", { headers });
    if (!response.ok || !response.body) throw new Error(`${response.status} ${response.statusText}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (generation === state.streamGeneration) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const id = frame.split("\n").find((line) => line.startsWith("id:"))?.slice(3).trim();
        const event = frame.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
        if (id) {
          state.streamCursor = id;
          sessionStorage.setItem("agentmesh-world-cursor", id);
        }
        if (event === "domain") await loadCompany({ quiet: true });
      }
    }
  } catch {
    if (generation === state.streamGeneration) window.setTimeout(() => connectRealtime(generation), 3000);
  }
}

$("task-search").addEventListener("input", renderTaskList);
$("language-toggle").addEventListener("click", () => {
  state.language = state.language === "en" ? "zh-CN" : "en";
  localStorage.setItem(STORAGE_LANGUAGE, state.language);
  state.employees = buildEmployees();
  render();
});
$("connection-button").addEventListener("click", () => {
  $("token").value = state.token;
  $("connection-dialog").showModal();
});
$("connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.token = $("token").value.trim();
  state.token ? sessionStorage.setItem("agentmesh-token", state.token) : sessionStorage.removeItem("agentmesh-token");
  $("connection-dialog").close();
  state.features.clear();
  state.streamGeneration += 1;
  await loadCompany();
  startUpdates();
});
document.querySelectorAll("[data-close-dialog]").forEach((node) => node.addEventListener("click", () => $(node.dataset.closeDialog).close()));
$("sound-toggle").setAttribute("aria-pressed", "false");
$("sound-toggle").addEventListener("click", toggleAmbientSound);
$("camera-zoom-out").addEventListener("click", () => officeScene?.changeZoom(-0.1));
$("camera-zoom-in").addEventListener("click", () => officeScene?.changeZoom(0.1));
$("camera-center").addEventListener("click", () => officeScene?.centerMap());
$("camera-focus").addEventListener("click", () => officeScene?.focusEmployee());
$("world-minimap").addEventListener("click", (event) => {
  const bounds = $("world-minimap").getBoundingClientRect();
  officeScene?.centerAtRatio(
    (event.clientX - bounds.left) / bounds.width,
    (event.clientY - bounds.top) / bounds.height
  );
});
$("zone-select").addEventListener("change", () => officeScene?.focusZone($("zone-select").value));
$("employee-picker").addEventListener("change", () => {
  if (!$("employee-picker").value) return;
  selectEmployee($("employee-picker").value);
  officeScene?.focusEmployee($("employee-picker").value);
});
$("motion-toggle").addEventListener("click", () => {
  state.reducedMotion = !state.reducedMotion;
  localStorage.setItem(STORAGE_MOTION, String(state.reducedMotion));
  officeScene?.setReducedMotion(state.reducedMotion);
  applyLanguage();
});

async function bootstrapWorld() {
  applyLanguage();
  await loadCampusMap();
  renderWorldSelectors();
  if (state.campusFallback) toast(t("mapFallback"), true);
  initWorldGame();
  await loadCompany();
  startUpdates();
}

bootstrapWorld();
