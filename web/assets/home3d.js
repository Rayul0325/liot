// LIOT 가상 집 — 3D 집(three.js) + 시나리오 재생.
//
// 화면은 아무것도 계산하지 않는다. 파급도·사건 시각·알림 문구·버튼은 전부
// POST /api/home3d/scenario 응답에서 온다(서버가 실제 표·사다리·송신 코드로 계산).
// 화면이 하는 일은 그 응답을 시간 순서대로 보여 주는 것뿐이다.
//
// 규칙(web/AGENTS.md): DOM 은 el() + textContent 로만 만든다(서버 값을 HTML 로 해석해 넣지 않는다),
// 응답은 data.ok 를 먼저 본다, 외부 네트워크 로드는 없다(three.js 도 저장소 안 파일).

const API_CATALOG = "/api/catalog";
const API_SCENARIO = "/api/home3d/scenario";

/**
 * @typedef {{entity_id:string, name:string, category:string}} SensorMeta
 * @typedef {{kind:string, label:string, label_ko:string, role:string, stops_ladder:boolean, target?:string}} EventAction
 * @typedef {{t_s:number, who:"resident"|"guardian", guardian:number|null, kind:string, title:string, body:string, priority:string, severity_ko:string|null, actions:EventAction[]}} ScenarioEvent
 * @typedef {{floor:number, floor_ko:string, final:number, name:string, ko:string, sources:string[], sources_ko:string[]}} ScenarioSeverity
 * @typedef {{t_s:number, label:string}} ScenarioMarker
 * @typedef {{ok:boolean, error?:string, state:"abnormal"|"unmonitorable"|"normal", opened_at:string, consented_guardians:number, entity_id:string, category:string, situation_ko:string, severity:ScenarioSeverity|null, markers:ScenarioMarker[], horizon_s:number, acked_at_s:number|null, events:ScenarioEvent[]}} ScenarioResponse
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
  /** 폰에 이미 보인 사건 수(events 앞부분) */ shown: 0,
  /** @type {{resident:number, guardian:number}} */ realerts: { resident: 0, guardian: 0 },
  ackShown: false,
  /** @type {{idx:number, role:string}|null} */ chosen: null,
  /** @type {{who:"resident"|"guardian", label:string}|null} */ ackInfo: null,
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
    // 화면 비율이 좁을수록 조금 물러나 집 전체가 들어오게.
    const aspect = width / height;
    orbit.r = aspect < 1.1 ? 20 : aspect < 1.45 ? 18 : aspect < 1.8 ? 16 : 14.5;
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
  const hint = document.getElementById("homeHint");
  if (hint) hint.hidden = true; // 평면도는 돌릴 수 없다
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
 * 확인 재요청에는 처음 응답의 opened_at 을 그대로 실어, 이미 보인 알림 문구의 시각이 바뀌지 않게 한다.
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
  if (ackAt != null) {
    body.ack_at_s = ackAt;
    if (state.data) body.opened_at = state.data.opened_at;
  } else {
    state.chosen = null;
    state.ackInfo = null;
  }
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
    syncPlayButton();
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
  else setStatus(state.playing ? "running" : "ready", "확인됨 — 이후 알림이 멈췄어요");
}

/** 응답이 바뀌었을 때(새 시나리오·확인) 화면 다시 그리기. */
function onDataChanged() {
  resetPhones();
  buildTimeline();
  applyTime();
}

/** 현재 재생 시각을 화면에 반영한다. */
function applyTime() {
  const horizon = state.data ? state.data.horizon_s : 0;
  const clock = document.getElementById("clock");
  if (clock) clock.textContent = `${formatTime(state.t)} / ${formatTime(horizon)}`;
  const fill = document.getElementById("tlFill");
  if (fill) fill.style.width = horizon > 0 ? `${(state.t / horizon) * 100}%` : "0%";
  const range = /** @type {HTMLInputElement|null} */ (document.getElementById("tlRange"));
  if (range && document.activeElement !== range) range.value = String(Math.floor(state.t));
  if (range) range.setAttribute("aria-valuetext", formatTime(state.t));
  renderPhones();
}

// ── 폰 두 대 ────────────────────────────────────────────────────────────
/** 사건 종류 → 카드 머리 표기. */
const KIND_KO = {
  alert: "상황 알림",
  realert: "재알림",
  forward: "보호자 전달",
  guidance_119: "119 안내",
  unmonitorable: "감시 불가 알림",
};

/**
 * @param {"resident"|"guardian"} who
 * @returns {{root:HTMLElement, feed:HTMLElement, empty:HTMLElement, clock:HTMLElement}|null}
 */
function phone(who) {
  const root = document.getElementById(who === "resident" ? "residentPhone" : "guardianPhone");
  if (!root) return null;
  const feed = /** @type {HTMLElement|null} */ (root.querySelector('[data-role="feed"]'));
  const empty = /** @type {HTMLElement|null} */ (root.querySelector('[data-role="empty"]'));
  const clock = /** @type {HTMLElement|null} */ (root.querySelector('[data-role="clock"]'));
  if (!feed || !empty || !clock) return null;
  return { root, feed, empty, clock };
}

/** 두 폰을 비운다(다음 renderPhones 가 현재 시각까지 다시 채운다). */
function resetPhones() {
  for (const who of /** @type {const} */ (["resident", "guardian"])) {
    const p = phone(who);
    if (p) p.feed.replaceChildren();
  }
  state.shown = 0;
  state.realerts = { resident: 0, guardian: 0 };
  state.ackShown = false;
  syncBadges();
}

/** 현재 시각까지의 사건을 폰에 보이게 한다(사건은 서버가 시각 순으로 준다). */
function renderPhones() {
  const data = state.data;
  let visible = 0;
  if (data) {
    while (visible < data.events.length && data.events[visible].t_s <= state.t) visible += 1;
  }
  if (visible < state.shown) resetPhones();
  for (let i = state.shown; i < visible; i += 1) {
    if (data) addEvent(data.events[i], i);
  }
  state.shown = visible;
  if (data && data.acked_at_s != null && state.t >= data.acked_at_s && !state.ackShown) {
    state.ackShown = true;
    addAckNotes(data.acked_at_s);
  }
  for (const who of /** @type {const} */ (["resident", "guardian"])) {
    const p = phone(who);
    if (!p) continue;
    p.clock.textContent = formatTime(state.t);
    const hasItems = p.feed.childElementCount > 0;
    p.empty.hidden = hasItems;
    if (who === "guardian" && data && data.consented_guardians === 0) {
      p.empty.textContent = "동의한 보호자가 없어요. 보호자에게는 아무것도 가지 않아요.";
    } else {
      p.empty.textContent = "아직 알림이 없어요";
    }
  }
}

/**
 * 사건 하나를 해당 폰 맨 위에 올린다.
 * @param {ScenarioEvent} ev
 * @param {number} idx 응답 events 안의 순번
 * @returns {void}
 */
function addEvent(ev, idx) {
  const p = phone(ev.who);
  if (!p) return;
  const fresh = state.playing && !reducedMotion();
  if (ev.kind === "realert") {
    state.realerts[ev.who] += 1;
    const line = el("div", "h3-realert");
    line.dataset.priority = ev.priority;
    line.append(el("span", "h3-realert-time tabnum", formatTime(ev.t_s)));
    line.append(el("span", "h3-realert-text", `같은 알림을 다시 보냈어요 · ${state.realerts[ev.who]}번째`));
    line.title = `우선순위 ${ev.priority}`;
    if (fresh) line.classList.add("is-new");
    p.feed.prepend(line);
    bumpBadge(ev.who);
    return;
  }
  const card = el("article", "notif h3-card");
  card.dataset.kind = ev.kind;
  card.dataset.priority = ev.priority;
  card.dataset.index = String(idx);
  if (fresh) card.classList.add("is-new");
  const head = el("div", "notif-head");
  head.append(el("span", "notif-app", ev.title));
  head.append(el("span", "h3-card-time tabnum", formatTime(ev.t_s)));
  card.append(head);
  const kind = /** @type {Record<string, string>} */ (KIND_KO)[ev.kind] || ev.kind;
  card.append(el("p", "h3-card-kind", ev.guardian ? `${kind} · 보호자 ${ev.guardian}` : kind));
  card.append(el("p", "h3-card-body", ev.body));
  const meta = el("div", "h3-card-meta");
  meta.append(el("span", "h3-prio", `우선순위 ${ev.priority}`));
  if (ev.severity_ko) meta.append(el("span", "h3-card-sev", ev.severity_ko));
  card.append(meta);
  if (ev.actions.length) {
    const actions = el("div", "notif-actions h3-actions");
    actions.style.gridTemplateColumns = `repeat(${ev.actions.length}, minmax(0, 1fr))`;
    const acked = Boolean(state.data && state.data.acked_at_s != null);
    for (const action of ev.actions) {
      const b = /** @type {HTMLButtonElement} */ (el("button", "notif-btn", action.label_ko));
      b.type = "button";
      b.dataset.role = action.role;
      if (action.kind === "view") {
        b.classList.add("h3-btn-call");
        b.addEventListener("click", () => showToast(ev.who, action));
      } else {
        if (state.chosen && state.chosen.idx === idx && state.chosen.role === action.role) {
          b.classList.add("is-chosen");
        }
        b.disabled = acked;
        b.addEventListener("click", () => acknowledge(idx, ev, action));
      }
      actions.append(b);
    }
    card.append(actions);
  }
  p.feed.prepend(card);
  bumpBadge(ev.who);
  // 좁은 화면(폰이 탭)에서는 새 알림이 온 폰으로 넘어간다(재알림은 반복이라 넘기지 않는다).
  if (state.playing && narrowScreen()) selectPhone(ev.who);
}

/** @returns {boolean} 폰이 탭으로 바뀌는 좁은 화면인가 */
function narrowScreen() {
  return window.matchMedia("(max-width: 640px)").matches;
}

/**
 * 좁은 화면에서 보이는 폰을 고른다.
 * @param {"resident"|"guardian"} who
 * @returns {void}
 */
function selectPhone(who) {
  const layout = document.getElementById("layout");
  if (layout) layout.dataset.phone = who;
  for (const tab of document.querySelectorAll(".h3-tab")) {
    const tabEl = /** @type {HTMLElement} */ (tab);
    tabEl.setAttribute("aria-selected", tabEl.dataset.who === who ? "true" : "false");
    tabEl.tabIndex = tabEl.dataset.who === who ? 0 : -1;
  }
}

/**
 * 탭 배지 = 그 폰에 지금 떠 있는 알림 수. 새 알림이면 잠깐 튄다.
 * @param {"resident"|"guardian"} who
 * @returns {void}
 */
function bumpBadge(who) {
  const badge = document.querySelector(`.h3-tab[data-who="${who}"] [data-role="badge"]`);
  const p = phone(who);
  if (!badge || !p) return;
  const count = p.feed.querySelectorAll(".h3-card, .h3-realert").length;
  badge.textContent = String(count);
  if (state.playing && !reducedMotion()) {
    badge.classList.remove("is-bump");
    void (/** @type {HTMLElement} */ (badge).offsetWidth);
    badge.classList.add("is-bump");
  }
}

/** 배지를 현재 폰 내용과 맞춘다(되감기·새 시나리오 뒤). */
function syncBadges() {
  for (const who of /** @type {const} */ (["resident", "guardian"])) {
    const badge = document.querySelector(`.h3-tab[data-who="${who}"] [data-role="badge"]`);
    const p = phone(who);
    if (badge && p) badge.textContent = String(p.feed.querySelectorAll(".h3-card, .h3-realert").length);
  }
}

/**
 * 거주자 응답·보호자 확인 → 그 시각으로 다시 요청(서버가 이후 사건을 뺀 타임라인을 준다).
 * @param {number} idx
 * @param {ScenarioEvent} ev
 * @param {EventAction} action
 * @returns {void}
 */
function acknowledge(idx, ev, action) {
  if (!state.data || state.data.acked_at_s != null || !action.stops_ladder) return;
  const at = Math.floor(Math.max(ev.t_s, state.t));
  state.chosen = { idx, role: action.role };
  state.ackInfo = { who: ev.who, label: action.label_ko };
  loadScenario(at);
}

/**
 * 확인 뒤 두 폰에 '멈췄어요' 안내 한 줄.
 * @param {number} at 확인 시각(초)
 * @returns {void}
 */
function addAckNotes(at) {
  const info = state.ackInfo;
  const whoKo = info && info.who === "guardian" ? "보호자" : "거주자";
  const label = info ? info.label : "확인";
  for (const who of /** @type {const} */ (["resident", "guardian"])) {
    const p = phone(who);
    if (!p) continue;
    const note = el("div", "h3-ack-note");
    note.append(el("span", "h3-realert-time tabnum", formatTime(at)));
    note.append(el("span", "", `${whoKo}가 [${label}] 버튼을 눌렀어요. 이후 재알림·보호자 전달·119 안내는 멈춰요.`));
    p.feed.prepend(note);
  }
}

/**
 * '119 전화'는 표시만 한다(데모는 발신하지 않는다).
 * @param {"resident"|"guardian"} who
 * @param {EventAction} action
 * @returns {void}
 */
function showToast(who, action) {
  const p = phone(who);
  if (!p) return;
  const old = p.root.querySelector(".h3-toast");
  if (old) old.remove();
  const toast = el(
    "p",
    "h3-toast",
    `데모라서 전화를 걸지 않아요. 실제 알림에서는 전화 앱이 열리고(${action.target || ""}) 누르는 건 사람이 해요.`
  );
  toast.setAttribute("role", "status");
  p.root.querySelector(".h3-phone-frame")?.append(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

// ── 타임라인 ────────────────────────────────────────────────────────────
/** 눈금(서버 markers)·사건 점·확인 표시를 다시 그린다. */
function buildTimeline() {
  const marks = document.getElementById("tlMarks");
  const range = /** @type {HTMLInputElement|null} */ (document.getElementById("tlRange"));
  if (!marks) return;
  marks.replaceChildren();
  const data = state.data;
  const horizon = data ? data.horizon_s : 0;
  if (range) {
    range.max = String(Math.max(1, horizon));
    range.disabled = !data;
  }
  if (!data || horizon <= 0) return;
  /** @param {number} t */
  const pct = (t) => `${(Math.min(t, horizon) / horizon) * 100}%`;
  for (const ev of data.events) {
    if (ev.kind === "realert") continue;
    const tick = el("span", `h3-tl-tick is-${ev.who}`);
    tick.style.left = pct(ev.t_s);
    marks.append(tick);
  }
  for (const m of data.markers) {
    const mark = el("span", "h3-tl-mark");
    const ratio = m.t_s / horizon;
    if (ratio > 0.85) mark.classList.add("is-end");
    else if (ratio < 0.06) mark.classList.add("is-start");
    mark.style.left = pct(m.t_s);
    mark.append(el("span", "h3-tl-mark-label", `${m.label} ${formatTime(m.t_s)}`));
    marks.append(mark);
  }
  if (data.acked_at_s != null) {
    const ack = el("span", "h3-tl-mark is-ack");
    ack.style.left = pct(data.acked_at_s);
    marks.append(ack);
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
/**
 * 라디오처럼 동작하는 버튼 묶음.
 * @param {HTMLElement} group
 * @param {string} cls 버튼 class
 * @param {Array<[string, () => boolean, () => void]>} items [표기, 선택됐나, 누르면]
 * @returns {void}
 */
function radioButtons(group, cls, items) {
  for (const [label, isOn, onPick] of items) {
    const b = el("button", cls, label);
    b.setAttribute("type", "button");
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", isOn() ? "true" : "false");
    b.addEventListener("click", () => {
      onPick();
      for (const other of group.querySelectorAll(`.${cls.split(" ")[0]}`)) {
        other.setAttribute("aria-checked", other === b ? "true" : "false");
      }
    });
    group.append(b);
  }
}

function buildControls() {
  const picker = document.getElementById("scenarioPicker");
  if (picker) {
    radioButtons(
      picker,
      "h3-chip",
      SCENARIOS.map((sc) => [
        sc.label,
        () => sc === state.scenario,
        () => {
          state.scenario = sc;
          loadScenario(null);
        },
      ])
    );
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
    radioButtons(
      seg,
      "h3-seg-btn",
      [0, 1, 2].map((n) => [
        `${n}명`,
        () => n === state.guardians,
        () => {
          state.guardians = n;
          loadScenario(null);
        },
      ])
    );
  }
  const speed = document.getElementById("speedSeg");
  if (speed) {
    radioButtons(
      speed,
      "h3-seg-btn",
      [10, 60].map((n) => [
        `×${n}`,
        () => n === state.speed,
        () => {
          state.speed = n;
        },
      ])
    );
  }
  const playBtn = document.getElementById("playBtn");
  if (playBtn) playBtn.addEventListener("click", () => (state.playing ? pause() : play()));
  const restart = document.getElementById("restartBtn");
  if (restart) restart.addEventListener("click", () => loadScenario(null));
  const tabs = document.getElementById("phoneTabs");
  if (tabs) {
    for (const tab of tabs.querySelectorAll(".h3-tab")) {
      const tabEl = /** @type {HTMLElement} */ (tab);
      tabEl.addEventListener("click", () => {
        selectPhone(tabEl.dataset.who === "guardian" ? "guardian" : "resident");
      });
    }
    // 탭 사이를 좌우 방향키로 옮긴다(탭 목록 관례).
    tabs.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const layout = document.getElementById("layout");
      const next = layout && layout.dataset.phone === "resident" ? "guardian" : "resident";
      selectPhone(next);
      /** @type {HTMLElement|null} */ (tabs.querySelector(`.h3-tab[data-who="${next}"]`))?.focus();
      e.preventDefault();
    });
    selectPhone("resident");
  }
  const range = /** @type {HTMLInputElement|null} */ (document.getElementById("tlRange"));
  if (range) {
    range.addEventListener("input", () => {
      state.t = Number(range.value) || 0;
      applyTime();
    });
  }
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
