// LIOT 가상 집 — 3D 집(three.js) + 시나리오 재생.
//
// 화면은 아무것도 계산하지 않는다. 파급도·사건 시각·알림 문구·버튼은 전부
// POST /api/home3d/scenario 응답에서 온다(서버가 실제 표·사다리·송신 코드로 계산).
// 화면이 하는 일은 그 응답을 시간 순서대로 보여 주는 것뿐이다.
//
// 규칙(web/AGENTS.md): DOM 은 el() + textContent 로만 만든다(서버 값에 innerHTML 금지),
// 응답은 data.ok 를 먼저 본다, 외부 네트워크 로드는 없다(three.js 도 저장소 안 파일).

const API_CATALOG = "/api/catalog";
const API_SCENARIO = "/api/home3d/scenario";

/**
 * @typedef {{entity_id:string, name:string, category:string}} SensorMeta
 * @typedef {{kind:string, label:string, label_ko:string, role:string, stops_ladder:boolean, target?:string}} EventAction
 * @typedef {{t_s:number, who:"resident"|"guardian", guardian:number|null, kind:string, title:string, body:string, priority:string, severity_ko:string|null, actions:EventAction[]}} ScenarioEvent
 * @typedef {{floor:number, floor_ko:string, final:number, name:string, ko:string, sources:string[], sources_ko:string[]}} ScenarioSeverity
 * @typedef {{t_s:number, label:string}} ScenarioMarker
 * @typedef {{ok:boolean, error?:string, state:"abnormal"|"unmonitorable"|"normal", entity_id:string, category:string, situation_ko:string, severity:ScenarioSeverity|null, markers:ScenarioMarker[], horizon_s:number, acked_at_s:number|null, events:ScenarioEvent[]}} ScenarioResponse
 * @typedef {{key:string, name:string, x0:number, z0:number, x1:number, z1:number}} Room
 */

/** 시나리오 = 요청 입력(센서와 값)일 뿐이다. 결과(파급도·사건)는 서버가 정한다. */
const SCENARIOS = [
  { key: "gas", label: "주방 가스 누출", entity_id: "binary_sensor.kitchen_gas", value: "on" },
  { key: "smoke", label: "복도 연기", entity_id: "binary_sensor.hall_smoke", value: "on" },
  { key: "lock", label: "도어락 잠금장치 이상", entity_id: "lock.front_door", value: "on" },
  {
    key: "gas_unavailable",
    label: "가스 센서 감시 불가",
    entity_id: "binary_sensor.kitchen_gas",
    value: "unavailable",
  },
];

/** 집 평면(미터). 가로 10 × 세로 7. @type {Room[]} */
const ROOMS = [
  { key: "living", name: "거실", x0: 0, z0: 0, x1: 6, z1: 4.2 },
  { key: "kitchen", name: "주방", x0: 6, z0: 0, x1: 10, z1: 4.2 },
  { key: "entry", name: "현관", x0: 0, z0: 4.2, x1: 2.4, z1: 7 },
  { key: "hall", name: "복도", x0: 2.4, z0: 4.2, x1: 6, z1: 7 },
  { key: "bath", name: "욕실", x0: 6, z0: 4.2, x1: 10, z1: 7 },
];
const PLAN_W = 10;
const PLAN_D = 7;

/** 벽: [x0, z0, x1, z1, 문 시작, 문 끝](문 = 벽을 따라 잰 거리, 없으면 생략). */
const WALLS = [
  [0, 0, 10, 0],
  [10, 0, 10, 7],
  [0, 7, 10, 7],
  [0, 0, 0, 7],
  [0, 4.2, 2.4, 4.2],
  [2.4, 4.2, 6, 4.2, 1.1, 2.3],
  [6, 4.2, 10, 4.2],
  [6, 0, 6, 4.2, 1.4, 2.8],
  [6, 4.2, 6, 7, 0.8, 1.8],
  [2.4, 4.2, 2.4, 7, 0.8, 2.0],
];

/** entity_id → 방. 이 화면만의 배치표다(서버에는 방 개념이 없다). */
const ROOM_RULES = [
  [/kitchen/, "kitchen"],
  [/hall/, "hall"],
  [/bath/, "bath"],
  [/front_door/, "entry"],
  [/living/, "living"],
];

/** 알려진 센서의 자리(x, z). 나머지는 방 안에 차례로 놓는다. */
const ANCHORS = {
  "binary_sensor.kitchen_gas": [8.3, 1.2],
  "binary_sensor.hall_smoke": [4.2, 5.6],
  "binary_sensor.bath_leak": [8.4, 6.0],
  "lock.front_door": [1.2, 6.6],
};

const WALL_H = 1.1;
const DOT_Y = 1.25;

const state = {
  /** @type {SensorMeta[]} */ sensors: [],
  scenario: SCENARIOS[0],
  vulnerable: false,
  guardians: 1,
  /** @type {ScenarioResponse|null} */ data: null,
  t: 0,
  playing: false,
  speed: 10,
  lastFrame: 0,
  requestSeq: 0,
};

/** @type {{render:()=>void, setRoomTint:(key:string|null, color:string|null)=>void}|null} */
let scene3d = null;
/** @type {Map<string, HTMLElement>} */
const dots = new Map();
/** @type {Map<string, [number, number]>} */
const dotPos = new Map();

// ── 작은 도우미 ─────────────────────────────────────────────────────────
/**
 * 요소 생성 헬퍼.
 * @param {string} tag 태그명
 * @param {string} [cls] class 속성
 * @param {string} [text] textContent (null/undefined 면 미설정)
 * @returns {HTMLElement}
 */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * GET JSON.
 * @param {string} url 엔드포인트
 * @returns {Promise<any>}
 */
async function getJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  return res.json();
}

/**
 * POST JSON. 응답 본문을 그대로 돌려준다(400 도 {ok:false, error} 본문이 있다).
 * @param {string} url 엔드포인트
 * @param {object} payload 본문
 * @returns {Promise<any>}
 */
async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  try {
    return await res.json();
  } catch (_) {
    return { ok: false, error: "응답을 읽지 못했습니다." };
  }
}

/**
 * 헤더 상태 표시.
 * @param {"loading"|"ready"|"running"|"silent"|"proposal"|"error"} kind
 * @param {string} text 표시 문구
 * @returns {void}
 */
function setStatus(kind, text) {
  const box = document.getElementById("status");
  if (box) box.dataset.state = kind;
  const label = document.getElementById("statusText");
  if (label) label.textContent = text;
}

/**
 * 초 → "1분 30초".
 * @param {number} s 초
 * @returns {string}
 */
function formatTime(s) {
  const total = Math.max(0, Math.floor(s));
  const m = Math.floor(total / 60);
  const r = total % 60;
  if (m === 0) return `${r}초`;
  return r === 0 ? `${m}분` : `${m}분 ${r}초`;
}

/** @returns {boolean} 움직임 줄이기 설정인가 */
function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * CSS 토큰 값(색).
 * @param {string} name 변수명(--sev-3 등)
 * @returns {string}
 */
function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── 센서 배치 ───────────────────────────────────────────────────────────
/**
 * @param {string} entityId
 * @returns {Room|undefined}
 */
function roomOf(entityId) {
  for (const [re, key] of ROOM_RULES) {
    if (/** @type {RegExp} */ (re).test(entityId)) return ROOMS.find((r) => r.key === key);
  }
  return undefined;
}

/**
 * 카탈로그 센서를 방에 놓는다(앵커가 없으면 방 안 격자로).
 * @param {SensorMeta[]} sensors
 * @returns {void}
 */
function placeSensors(sensors) {
  /** @type {Record<string, number>} */
  const used = {};
  for (const s of sensors) {
    const room = roomOf(s.entity_id);
    if (!room) continue;
    const anchor = /** @type {Record<string, [number, number]>} */ (ANCHORS)[s.entity_id];
    if (anchor) {
      dotPos.set(s.entity_id, anchor);
      continue;
    }
    const i = used[room.key] || 0;
    used[room.key] = i + 1;
    const cols = 3;
    const x = room.x0 + 0.9 + (i % cols) * 1.5;
    const z = room.z0 + 1.0 + Math.floor(i / cols) * 1.2;
    dotPos.set(s.entity_id, [Math.min(x, room.x1 - 0.5), Math.min(z, room.z1 - 0.5)]);
  }
}

/**
 * 센서 점(HTML) 하나. 3D 모드에서는 투영 위치로, 평면도 모드에서는 % 위치로 놓인다.
 * @param {SensorMeta} s
 * @returns {HTMLElement}
 */
function makeDot(s) {
  const dot = el("span", "h3-dot is-idle");
  dot.dataset.entity = s.entity_id;
  // 이름표는 시나리오에 쓰는 센서(앵커가 있는 것)에만 늘 보인다. 나머지는 점만.
  if (!(s.entity_id in ANCHORS)) dot.classList.add("is-minor");
  dot.setAttribute("role", "img");
  dot.setAttribute("aria-label", `${s.name} — 대기`);
  dot.append(el("span", "h3-dot-core"));
  dot.append(el("span", "h3-dot-label", s.name));
  return dot;
}

// ── 3D 집 ──────────────────────────────────────────────────────────────
/** @returns {boolean} 이 브라우저에서 WebGL 컨텍스트를 만들 수 있는가 */
function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return Boolean(c.getContext("webgl2") || c.getContext("webgl"));
  } catch (_) {
    return false;
  }
}

/**
 * three.js 로 집을 그린다. WebGL 이나 모듈을 쓸 수 없으면 null(평면도로 대체).
 * @param {HTMLElement} host 캔버스를 넣을 곳
 * @param {HTMLElement} overlay 점·방 이름 층
 * @returns {Promise<{render:()=>void, setRoomTint:(key:string|null, color:string|null)=>void}|null>}
 */
async function initThree(host, overlay) {
  if (!webglAvailable()) return null;
  /** @type {any} */
  let THREE;
  try {
    THREE = await import("./vendor/three.module.min.js");
  } catch (_) {
    return null;
  }
  /** @type {any} */
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (_) {
    return null;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = "h3-canvas";
  host.prepend(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200);
  const target = new THREE.Vector3(PLAN_W / 2, 0, PLAN_D / 2);
  const orbit = { theta: -0.55, phi: 0.9, r: 14.5 };

  const lineCol = new THREE.Color(token("--h3-line") || "#2b4a66");
  const accent = new THREE.Color(token("--accent") || "#35c6ff");

  // 바닥: 부드러운 격자.
  const grid = new THREE.GridHelper(40, 40, lineCol, lineCol);
  grid.position.set(PLAN_W / 2, -0.01, PLAN_D / 2);
  grid.material.transparent = true;
  grid.material.opacity = 0.28;
  scene.add(grid);

  // 방 바닥 + 테두리.
  /** @type {Map<string, any>} */
  const floors = new Map();
  const baseFloor = new THREE.Color(token("--h3-floor") || "#101a28");
  for (const room of ROOMS) {
    const w = room.x1 - room.x0;
    const d = room.z1 - room.z0;
    const geo = new THREE.PlaneGeometry(w - 0.06, d - 0.06);
    const mat = new THREE.MeshBasicMaterial({
      color: baseFloor.clone(),
      transparent: true,
      opacity: 0.85,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(room.x0 + w / 2, 0, room.z0 + d / 2);
    scene.add(mesh);
    floors.set(room.key, mesh);
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geo),
      new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.22 })
    );
    edges.rotation.x = -Math.PI / 2;
    edges.position.set(room.x0 + w / 2, 0.005, room.z0 + d / 2);
    scene.add(edges);
  }

  // 벽: 반투명 판 + 모서리 선(그래프 같은 느낌).
  const wallMat = new THREE.MeshBasicMaterial({
    color: new THREE.Color(token("--h3-wall") || "#16263a"),
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  });
  const wallEdge = new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.5 });
  /**
   * @param {number} x0
   * @param {number} z0
   * @param {number} x1
   * @param {number} z1
   */
  const addWall = (x0, z0, x1, z1) => {
    const len = Math.hypot(x1 - x0, z1 - z0);
    if (len < 0.05) return;
    const geo = new THREE.BoxGeometry(len, WALL_H, 0.08);
    const mesh = new THREE.Mesh(geo, wallMat);
    mesh.position.set((x0 + x1) / 2, WALL_H / 2, (z0 + z1) / 2);
    mesh.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
    scene.add(mesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo), wallEdge);
    edges.position.copy(mesh.position);
    edges.rotation.copy(mesh.rotation);
    scene.add(edges);
  };
  for (const w of WALLS) {
    const [x0, z0, x1, z1, g0, g1] = w;
    if (g0 === undefined || g1 === undefined) {
      addWall(x0, z0, x1, z1);
      continue;
    }
    const len = Math.hypot(x1 - x0, z1 - z0);
    const ux = (x1 - x0) / len;
    const uz = (z1 - z0) / len;
    addWall(x0, z0, x0 + ux * g0, z0 + uz * g0);
    addWall(x0 + ux * g1, z0 + uz * g1, x1, z1);
  }

  // 센서 기둥(바닥 → 점 높이의 가는 선).
  const stemMat = new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.35 });
  for (const [, [x, z]] of dotPos) {
    const g = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(x, 0, z),
      new THREE.Vector3(x, DOT_Y, z),
    ]);
    scene.add(new THREE.Line(g, stemMat));
  }

  // 방 이름 표(HTML).
  /** @type {Array<[HTMLElement, any]>} */
  const tags = [];
  for (const room of ROOMS) {
    const tag = el("span", "h3-room-tag", room.name);
    overlay.append(tag);
    // 방 이름은 방 모서리 쪽에 — 가운데는 센서 점 자리라 겹친다.
    tags.push([tag, new THREE.Vector3(room.x0 + 0.75, 0.02, room.z0 + 0.5)]);
  }
  /** @type {Array<[HTMLElement, any]>} */
  const dotAnchors = [];
  for (const [eid, [x, z]] of dotPos) {
    const dot = dots.get(eid);
    if (dot) dotAnchors.push([dot, new THREE.Vector3(x, DOT_Y, z)]);
  }

  const v = new THREE.Vector3();
  let width = 1;
  let height = 1;
  const place = () => {
    for (const [node, pos] of [...tags, ...dotAnchors]) {
      v.copy(pos).project(camera);
      const x = (v.x * 0.5 + 0.5) * width;
      const y = (-v.y * 0.5 + 0.5) * height;
      node.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    }
  };
  let queued = false;
  const render = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const sp = Math.sin(orbit.phi);
      camera.position.set(
        target.x + orbit.r * sp * Math.sin(orbit.theta),
        target.y + orbit.r * Math.cos(orbit.phi),
        target.z + orbit.r * sp * Math.cos(orbit.theta)
      );
      camera.lookAt(target);
      renderer.render(scene, camera);
      place();
    });
  };
  const resize = () => {
    width = Math.max(1, host.clientWidth);
    height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    // 좁은 화면에서는 조금 물러나 집 전체가 들어오게.
    orbit.r = width < 520 ? 18 : 14.5;
    camera.updateProjectionMatrix();
    render();
  };
  new ResizeObserver(resize).observe(host);

  // 회전: pointer events(마우스·터치 공통). 세로 끌기는 페이지 스크롤과 겹치지 않게
  // touch-action: pan-y 로 두고(CSS), 가로 끌기만 확실히 돌린다.
  /** @type {{id:number, x:number, y:number}|null} */
  let drag = null;
  host.addEventListener("pointerdown", (e) => {
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY };
    host.setPointerCapture(e.pointerId);
    host.classList.add("is-dragging");
  });
  host.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    orbit.theta -= dx * 0.008;
    if (e.pointerType === "mouse") orbit.phi = Math.min(1.35, Math.max(0.35, orbit.phi - dy * 0.006));
    render();
  });
  const endDrag = (/** @type {PointerEvent} */ e) => {
    if (drag && drag.id === e.pointerId) drag = null;
    host.classList.remove("is-dragging");
  };
  host.addEventListener("pointerup", endDrag);
  host.addEventListener("pointercancel", endDrag);
  host.addEventListener("keydown", (e) => {
    const step = 0.12;
    if (e.key === "ArrowLeft") orbit.theta += step;
    else if (e.key === "ArrowRight") orbit.theta -= step;
    else if (e.key === "ArrowUp") orbit.phi = Math.max(0.35, orbit.phi - step);
    else if (e.key === "ArrowDown") orbit.phi = Math.min(1.35, orbit.phi + step);
    else return;
    e.preventDefault();
    render();
  });

  const setRoomTint = (/** @type {string|null} */ key, /** @type {string|null} */ color) => {
    for (const [k, mesh] of floors) {
      if (k === key && color) {
        mesh.material.color.copy(baseFloor).lerp(new THREE.Color(color), 0.35);
      } else {
        mesh.material.color.copy(baseFloor);
      }
    }
    render();
  };

  resize();
  return { render, setRoomTint };
}

/**
 * WebGL 을 쓸 수 없을 때의 2D 평면도. 같은 점 요소를 % 위치로 놓는다.
 * @param {HTMLElement} host
 * @param {HTMLElement} overlay
 * @returns {{render:()=>void, setRoomTint:(key:string|null, color:string|null)=>void}}
 */
function initPlan(host, overlay) {
  host.classList.add("is-plan");
  const plan = el("div", "h3-plan");
  /** @type {Map<string, HTMLElement>} */
  const rooms = new Map();
  for (const room of ROOMS) {
    const box = el("div", "h3-plan-room");
    box.style.left = `${(room.x0 / PLAN_W) * 100}%`;
    box.style.top = `${(room.z0 / PLAN_D) * 100}%`;
    box.style.width = `${((room.x1 - room.x0) / PLAN_W) * 100}%`;
    box.style.height = `${((room.z1 - room.z0) / PLAN_D) * 100}%`;
    box.append(el("span", "h3-room-tag", room.name));
    plan.append(box);
    rooms.set(room.key, box);
  }
  for (const [eid, [x, z]] of dotPos) {
    const dot = dots.get(eid);
    if (!dot) continue;
    dot.style.left = `${(x / PLAN_W) * 100}%`;
    dot.style.top = `${(z / PLAN_D) * 100}%`;
    plan.append(dot);
  }
  overlay.replaceChildren();
  host.append(plan);
  const note = el("p", "h3-plan-note", "이 브라우저는 3D(WebGL)를 쓸 수 없어 평면도로 보여 드려요.");
  host.append(note);
  return {
    render: () => {},
    setRoomTint: (key, color) => {
      for (const [k, box] of rooms) {
        if (k === key && color) box.style.setProperty("--room-tint", color);
        else box.style.removeProperty("--room-tint");
      }
    },
  };
}

// ── 시나리오 ────────────────────────────────────────────────────────────
/**
 * 점의 상태 class 를 서버 응답 그대로 붙인다(화면은 파급도를 계산하지 않는다).
 * @returns {void}
 */
function paintDots() {
  const data = state.data;
  for (const [eid, dot] of dots) {
    const meta = state.sensors.find((s) => s.entity_id === eid);
    const name = meta ? meta.name : eid;
    const minor = !(eid in ANCHORS) ? " is-minor" : "";
    let cls = `h3-dot is-idle${minor}`;
    let label = `${name} — 대기`;
    if (data && data.entity_id === eid) {
      if (data.state === "abnormal" && data.severity) {
        cls = `h3-dot is-target sev-${data.severity.final}`;
        label = `${name} — ${data.severity.ko}`;
      } else if (data.state === "unmonitorable") {
        cls = "h3-dot is-target is-unmonitorable";
        label = `${name} — 감시 불가`;
      } else {
        cls = "h3-dot is-target is-normal";
        label = `${name} — 정상`;
      }
    }
    dot.className = cls;
    dot.setAttribute("aria-label", label);
  }
  if (scene3d) {
    const room = data ? roomOf(data.entity_id) : undefined;
    let color = null;
    if (data && data.state === "abnormal" && data.severity) color = token(`--sev-${data.severity.final}`);
    else if (data && data.state === "unmonitorable") color = token("--sev-unk");
    scene3d.setRoomTint(room ? room.key : null, color);
  }
}

/** 시나리오 요약 한 줄(서버 값만). */
function renderSummary() {
  const box = document.getElementById("scenarioSummary");
  if (!box) return;
  box.replaceChildren();
  const data = state.data;
  if (!data) return;
  if (data.state === "abnormal" && data.severity) {
    box.append(el("span", "h3-sum-key", "상황"));
    box.append(el("span", "h3-sum-val", data.situation_ko));
    box.append(el("span", "h3-sum-key", "파급도"));
    const sev = el("span", `h3-sev-chip sev-${data.severity.final}`, data.severity.ko);
    box.append(sev);
    box.append(el("span", "h3-sum-src", `출처 ${data.severity.sources_ko.join(" · ")}`));
  } else if (data.state === "unmonitorable") {
    box.append(el("span", "h3-sum-key", "상황"));
    box.append(el("span", "h3-sev-chip is-unmonitorable", "감시 불가"));
    box.append(el("span", "h3-sum-src", "센서를 읽지 못함 — 위험 상황은 열리지 않아요"));
  } else {
    box.append(el("span", "h3-sum-key", "상황"));
    box.append(el("span", "h3-sev-chip is-normal", "정상"));
  }
}

/**
 * 시나리오를 서버에 묻는다. ackAt 이 있으면 그 시각의 확인을 반영한 타임라인을 받는다.
 * @param {number|null} ackAt 확인 시각(초)
 * @returns {Promise<void>}
 */
async function loadScenario(ackAt) {
  const seq = ++state.requestSeq;
  setStatus("running", "계산 중…");
  /** @type {Record<string, any>} */
  const body = {
    entity_id: state.scenario.entity_id,
    value: state.scenario.value,
    vulnerable_resident: state.vulnerable,
    consented_guardians: state.guardians,
  };
  if (ackAt != null) body.ack_at_s = ackAt;
  let data;
  try {
    data = await postJson(API_SCENARIO, body);
  } catch (err) {
    data = { ok: false, error: `서버에 닿지 못했습니다: ${String(err)}` };
  }
  if (seq !== state.requestSeq) return; // 더 새 요청이 있다
  if (!data || !data.ok) {
    state.data = null;
    state.playing = false;
    setStatus("error", (data && data.error) || "시나리오를 불러오지 못했습니다.");
    paintDots();
    renderSummary();
    onDataChanged();
    return;
  }
  state.data = /** @type {ScenarioResponse} */ (data);
  if (ackAt == null) state.t = 0;
  paintDots();
  renderSummary();
  onDataChanged();
  if (ackAt == null) play();
  else setStatus(state.playing ? "running" : "ready", "확인됨 — 사다리가 멈췄어요");
}

/** 응답이 바뀌었을 때(새 시나리오·확인) 화면 다시 그리기. */
function onDataChanged() {
  applyTime();
}

/** 현재 재생 시각을 화면에 반영한다. */
function applyTime() {
  const clock = document.getElementById("clock");
  if (clock) {
    const horizon = state.data ? state.data.horizon_s : 0;
    clock.textContent = `${formatTime(state.t)} / ${formatTime(horizon)}`;
  }
}

// ── 재생 ────────────────────────────────────────────────────────────────
function play() {
  if (!state.data) return;
  if (state.t >= state.data.horizon_s) state.t = 0;
  state.playing = true;
  state.lastFrame = performance.now();
  syncPlayButton();
  setStatus("running", "재생 중");
  requestAnimationFrame(tick);
}

function pause() {
  state.playing = false;
  syncPlayButton();
  setStatus("ready", "일시 정지");
}

function syncPlayButton() {
  const btn = document.getElementById("playBtn");
  if (!btn) return;
  btn.textContent = state.playing ? "일시 정지" : "재생";
  btn.setAttribute("aria-pressed", state.playing ? "true" : "false");
}

/** @param {number} now rAF 시각 */
function tick(now) {
  if (!state.playing || !state.data) return;
  const dt = Math.min(0.25, (now - state.lastFrame) / 1000);
  state.lastFrame = now;
  state.t = Math.min(state.data.horizon_s, state.t + dt * state.speed);
  applyTime();
  if (state.t >= state.data.horizon_s) {
    state.playing = false;
    syncPlayButton();
    setStatus("ready", "재생 끝");
    return;
  }
  requestAnimationFrame(tick);
}

// ── 조작부 ──────────────────────────────────────────────────────────────
function buildControls() {
  const picker = document.getElementById("scenarioPicker");
  if (picker) {
    for (const sc of SCENARIOS) {
      const b = el("button", "h3-chip", sc.label);
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.key = sc.key;
      b.setAttribute("aria-checked", sc === state.scenario ? "true" : "false");
      b.addEventListener("click", () => {
        state.scenario = sc;
        for (const other of picker.querySelectorAll(".h3-chip")) {
          other.setAttribute("aria-checked", other === b ? "true" : "false");
        }
        loadScenario(null);
      });
      picker.append(b);
    }
  }
  const vul = /** @type {HTMLInputElement|null} */ (document.getElementById("vulnerableToggle"));
  if (vul) {
    vul.checked = state.vulnerable;
    vul.addEventListener("change", () => {
      state.vulnerable = vul.checked;
      loadScenario(null);
    });
  }
  const seg = document.getElementById("guardianSeg");
  if (seg) {
    for (const n of [0, 1, 2]) {
      const b = el("button", "h3-seg-btn", `${n}명`);
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", n === state.guardians ? "true" : "false");
      b.addEventListener("click", () => {
        state.guardians = n;
        for (const other of seg.querySelectorAll(".h3-seg-btn")) {
          other.setAttribute("aria-checked", other === b ? "true" : "false");
        }
        loadScenario(null);
      });
      seg.append(b);
    }
  }
  const playBtn = document.getElementById("playBtn");
  if (playBtn) playBtn.addEventListener("click", () => (state.playing ? pause() : play()));
}

// ── 시작 ────────────────────────────────────────────────────────────────
async function init() {
  buildControls();
  const stage = document.getElementById("homeStage");
  const overlay = document.getElementById("homeOverlay");
  if (!stage || !overlay) return;
  let catalog;
  try {
    catalog = await getJson(API_CATALOG);
  } catch (err) {
    setStatus("error", "센서 목록을 불러오지 못했습니다.");
    return;
  }
  if (!catalog || !Array.isArray(catalog.sensors)) {
    setStatus("error", "센서 목록 형식이 올바르지 않습니다.");
    return;
  }
  state.sensors = catalog.sensors;
  placeSensors(state.sensors);
  for (const s of state.sensors) {
    if (!dotPos.has(s.entity_id)) continue;
    const dot = makeDot(s);
    dots.set(s.entity_id, dot);
    overlay.append(dot);
  }
  scene3d = await initThree(stage, overlay);
  if (!scene3d) scene3d = initPlan(stage, overlay);
  document.body.dataset.render = stage.classList.contains("is-plan") ? "plan" : "webgl";
  setStatus("ready", "준비됨");
  await loadScenario(null);
}

init();
