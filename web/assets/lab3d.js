/* LIOT 3D 랩 — 1인칭 워크스루 집.
   클릭해 포인터락으로 들어가 WASD로 걷고, 마우스로 둘러보고, 중앙 조준점으로
   기기를 겨눠 조작하면 실제 파이프라인(/api/simulate)이 로컬 LLM까지 태워 판단하고,
   제안을 수락하면 3D 집이 실제로 바뀐다.
   여기서 판단하지 않는다 — 서버의 진짜 LiotApp + 로컬 LLM 응답만 시각화한다.
   낮/밤은 시간대 버튼에 따라 하늘·태양광·앰비언트·창문·별이 부드럽게 전환된다. */
import * as THREE from "three";

/* ===== 팔레트 (실내 오브젝트 고정색; 하늘/조명은 낮밤 시스템이 관리) ===== */
var C = {
  floor: 0x271f18,
  rug: 0x322419,
  wall: 0x1c1712,
  wallR: 0x17120e,
  ceil: 0x120e0b,
  sofa: 0x453a30,
  wood: 0x5a4636,
  tv: 0x0d0b0a,
  ember: 0xff5c38,
  emberDeep: 0xc93a1d,
  breath: 0xe8b44f,
  person: 0xd8cfc2,
  metal: 0x6b6157,
  safe: 0x7fb069,
};

var $ = function (id) {
  return document.getElementById(id);
};
var esc = function (s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
};
var clamp = function (v, a, b) {
  return v < a ? a : v > b ? b : v;
};

/* ===== 씬 기본 ===== */
var mount = $("scene");
var scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0908);
scene.fog = new THREE.Fog(0x0b0908, 14, 42);

var camera = new THREE.PerspectiveCamera(
  72,
  mount.clientWidth / mount.clientHeight,
  0.05,
  120,
);
camera.rotation.order = "YXZ";
camera.position.set(0.4, 1.62, 3.4);

var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(mount.clientWidth, mount.clientHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
mount.appendChild(renderer.domElement);

/* ===== 조명 (세기·색은 낮밤 시스템이 매 프레임 갱신) ===== */
var amb = new THREE.HemisphereLight(0x9fb4c4, 0x3a2f28, 0.5);
scene.add(amb);
var sun = new THREE.DirectionalLight(0xffe8d6, 0.6);
sun.position.set(8, 14, 6);
sun.castShadow = true;
sun.shadow.mapSize.set(1024, 1024);
sun.shadow.camera.left = -10;
sun.shadow.camera.right = 10;
sun.shadow.camera.top = 10;
sun.shadow.camera.bottom = -10;
scene.add(sun);
/* 천장 조명이 켜졌을 때의 실내 포인트 라이트 */
var roomLight = new THREE.PointLight(0xffc98a, 0, 20, 2);
roomLight.position.set(0, 3.6, 0);
scene.add(roomLight);

/* ===== 헬퍼 ===== */
/**
 * 로우폴리 표준 머티리얼을 만든다(플랫 셰이딩 기본).
 * @param {number} color 16진 색
 * @param {{rough?:number,metal?:number,emissive?:number,ei?:number,transparent?:boolean,opacity?:number,flat?:boolean}} [opts]
 * @returns {THREE.MeshStandardMaterial}
 */
function mat(color, opts) {
  opts = opts || {};
  return new THREE.MeshStandardMaterial({
    color: color,
    roughness: opts.rough == null ? 0.85 : opts.rough,
    metalness: opts.metal == null ? 0.05 : opts.metal,
    emissive: opts.emissive == null ? 0x000000 : opts.emissive,
    emissiveIntensity: opts.ei == null ? 1 : opts.ei,
    transparent: !!opts.transparent,
    opacity: opts.opacity == null ? 1 : opts.opacity,
    flatShading: opts.flat == null ? true : opts.flat,
  });
}
/**
 * 그림자 캐스트/리시브가 켜진 박스 메시를 만든다.
 * @param {number} w 너비 @param {number} h 높이 @param {number} d 깊이
 * @param {number} color 색 @param {object} [opts] mat() 옵션
 * @returns {THREE.Mesh}
 */
function box(w, h, d, color, opts) {
  var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(color, opts));
  m.castShadow = true;
  m.receiveShadow = true;
  return m;
}

/* ===== 낮/밤 하늘 텍스처 (창문에 매핑) ===== */
var skyCanvas = document.createElement("canvas");
skyCanvas.width = 256;
skyCanvas.height = 256;
var skyCtx = skyCanvas.getContext("2d");
var skyTex = new THREE.CanvasTexture(skyCanvas);
skyTex.colorSpace = THREE.SRGBColorSpace;
/* 별 위치(한 번만 산포) */
var STARS = [];
for (var si = 0; si < 80; si++) {
  STARS.push([Math.random(), Math.random() * 0.62, 0.6 + Math.random() * 0.9]);
}

/* ===== 낮/밤 상태 (dl = 현재값, 매 프레임 target으로 이징) ===== */
function col(hex) {
  return new THREE.Color(hex);
}
var PHASE = {
  day: {
    bg: 0x9db6c6,
    fogN: 16,
    fogF: 52,
    fog: 0xa9bcc8,
    ambInt: 1.0,
    skyHemi: 0xbcd3e4,
    groundHemi: 0x4a4038,
    sunCol: 0xfff3df,
    sunInt: 1.25,
    sunPos: [7, 13, 5],
    skyTop: 0x6fa9d6,
    skyBot: 0xe4edf0,
    disc: 0xfff6de,
    discY: 0.26,
    starA: 0,
  },
  evening: {
    bg: 0x3a2a2a,
    fogN: 13,
    fogF: 42,
    fog: 0x4a3128,
    ambInt: 0.52,
    skyHemi: 0x8a6a52,
    groundHemi: 0x2a1e18,
    sunCol: 0xff8a4a,
    sunInt: 0.72,
    sunPos: [-8, 4, 3],
    skyTop: 0x37345f,
    skyBot: 0xe07a42,
    disc: 0xffb15a,
    discY: 0.6,
    starA: 0.18,
  },
  night: {
    bg: 0x090b12,
    fogN: 11,
    fogF: 34,
    fog: 0x0c0e16,
    ambInt: 0.3,
    skyHemi: 0x25324a,
    groundHemi: 0x0d0b0a,
    sunCol: 0x9fb6e0,
    sunInt: 0.16,
    sunPos: [4, 14, -6],
    skyTop: 0x04060d,
    skyBot: 0x18213a,
    disc: 0xdce6f7,
    discY: 0.3,
    starA: 1,
  },
};
/** 현재 낮밤 상태(색은 Color 인스턴스, 나머지는 스칼라). */
var dl = {
  bg: col(0),
  fog: col(0),
  fogN: 0,
  fogF: 0,
  ambInt: 0,
  skyHemi: col(0),
  groundHemi: col(0),
  sunCol: col(0),
  sunInt: 0,
  sunPos: [0, 0, 0],
  skyTop: col(0),
  skyBot: col(0),
  disc: col(0),
  discY: 0,
  starA: 0,
};
var dlTarget = PHASE.evening;
/** dl을 target 값으로 즉시 스냅(초기화용). */
function snapDaylight(t) {
  dl.bg.set(t.bg);
  dl.fog.set(t.fog);
  dl.fogN = t.fogN;
  dl.fogF = t.fogF;
  dl.ambInt = t.ambInt;
  dl.skyHemi.set(t.skyHemi);
  dl.groundHemi.set(t.groundHemi);
  dl.sunCol.set(t.sunCol);
  dl.sunInt = t.sunInt;
  dl.sunPos = t.sunPos.slice();
  dl.skyTop.set(t.skyTop);
  dl.skyBot.set(t.skyBot);
  dl.disc.set(t.disc);
  dl.discY = t.discY;
  dl.starA = t.starA;
}
/**
 * 시간대를 바꾼다. 부드럽게 이징(instant=true면 즉시).
 * @param {"day"|"evening"|"night"} key
 * @param {boolean} [instant]
 */
function setDaylight(key, instant) {
  dlTarget = PHASE[key] || PHASE.evening;
  if (instant) snapDaylight(dlTarget);
}
/** dl을 dlTarget으로 한 스텝 이징하고 씬에 적용. */
function stepDaylight(dt) {
  var k = Math.min(1, dt * 3.2);
  var t = dlTarget;
  dl.bg.lerp(col(t.bg), k);
  dl.fog.lerp(col(t.fog), k);
  dl.fogN += (t.fogN - dl.fogN) * k;
  dl.fogF += (t.fogF - dl.fogF) * k;
  dl.ambInt += (t.ambInt - dl.ambInt) * k;
  dl.skyHemi.lerp(col(t.skyHemi), k);
  dl.groundHemi.lerp(col(t.groundHemi), k);
  dl.sunCol.lerp(col(t.sunCol), k);
  dl.sunInt += (t.sunInt - dl.sunInt) * k;
  for (var i = 0; i < 3; i++) dl.sunPos[i] += (t.sunPos[i] - dl.sunPos[i]) * k;
  dl.skyTop.lerp(col(t.skyTop), k);
  dl.skyBot.lerp(col(t.skyBot), k);
  dl.disc.lerp(col(t.disc), k);
  dl.discY += (t.discY - dl.discY) * k;
  dl.starA += (t.starA - dl.starA) * k;

  scene.background.copy(dl.bg);
  scene.fog.color.copy(dl.fog);
  scene.fog.near = dl.fogN;
  scene.fog.far = dl.fogF;
  amb.color.copy(dl.skyHemi);
  amb.groundColor.copy(dl.groundHemi);
  amb.intensity = dl.ambInt + (devices.light && devices.light.on ? 0.32 : 0);
  sun.color.copy(dl.sunCol);
  sun.intensity = dl.sunInt;
  sun.position.set(dl.sunPos[0], dl.sunPos[1], dl.sunPos[2]);
  drawSky();
}
/** 창문에 매핑되는 하늘 텍스처를 현재 dl 상태로 그린다. */
function drawSky() {
  var x = skyCtx,
    w = 256,
    h = 256;
  var g = x.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, "#" + dl.skyTop.getHexString());
  g.addColorStop(1, "#" + dl.skyBot.getHexString());
  x.fillStyle = g;
  x.fillRect(0, 0, w, h);
  if (dl.starA > 0.02) {
    x.fillStyle = "rgba(255,255,255," + (0.9 * dl.starA).toFixed(3) + ")";
    for (var i = 0; i < STARS.length; i++) {
      x.beginPath();
      x.arc(STARS[i][0] * w, STARS[i][1] * h, STARS[i][2], 0, 6.283);
      x.fill();
    }
  }
  var cx = 0.68 * w,
    cy = dl.discY * h,
    R = 24;
  var rg = x.createRadialGradient(cx, cy, 2, cx, cy, R * 2.8);
  rg.addColorStop(0, "#" + dl.disc.getHexString());
  rg.addColorStop(1, "rgba(0,0,0,0)");
  x.fillStyle = rg;
  x.beginPath();
  x.arc(cx, cy, R * 2.8, 0, 6.283);
  x.fill();
  x.fillStyle = "#" + dl.disc.getHexString();
  x.beginPath();
  x.arc(cx, cy, R, 0, 6.283);
  x.fill();
  skyTex.needsUpdate = true;
}

/* ===== 방 셸 (1인칭이므로 완전히 둘러싼 4벽 + 천장) ===== */
var room = new THREE.Group();
scene.add(room);
var W = 9,
  DP = 9,
  H = 5;
var floor = new THREE.Mesh(
  new THREE.BoxGeometry(W, 0.3, DP),
  mat(C.floor, { rough: 0.95 }),
);
floor.position.y = -0.15;
floor.receiveShadow = true;
room.add(floor);
var rug = new THREE.Mesh(
  new THREE.CylinderGeometry(2.6, 2.6, 0.04, 8),
  mat(C.rug, { rough: 1 }),
);
rug.position.set(-0.3, 0.02, 0.6);
rug.receiveShadow = true;
room.add(rug);
var ceil = new THREE.Mesh(
  new THREE.BoxGeometry(W, 0.25, DP),
  mat(C.ceil, { rough: 1 }),
);
ceil.position.y = H;
ceil.receiveShadow = true;
room.add(ceil);
/* 4벽 (안쪽을 향한 면) */
function wall(w, h, d, x, z, color) {
  var m = box(w, h, d, color, { rough: 1 });
  m.position.set(x, h / 2, z);
  room.add(m);
  return m;
}
wall(W, H, 0.25, 0, -DP / 2, C.wall); // 뒤 (주방쪽)
wall(0.25, H, DP, -W / 2, 0, C.wallR); // 좌
wall(0.25, H, DP, W / 2, 0, C.wallR); // 우
wall(W, H, 0.25, 0, DP / 2, C.wall); // 앞 (스폰 뒤)
/* 걸레받이(엠버 트림) */
var trimB = box(W, 0.12, 0.32, C.emberDeep, {
  emissive: C.emberDeep,
  ei: 0.25,
});
trimB.position.set(0, 0.06, -DP / 2 + 0.16);
room.add(trimB);

/* ===== 창문 (하늘 텍스처를 보여주는 발광 없는 패널 + 프레임) ===== */
/**
 * 벽에 붙는 창문을 만든다(하늘 텍스처 패널 + 나무 프레임).
 * @param {number} ww 창 너비 @param {number} wh 창 높이
 * @param {THREE.Vector3} pos 창 중심 @param {number} rotY 패널 회전(벽 방향)
 */
function windowPane(ww, wh, pos, rotY) {
  var g = new THREE.Group();
  var pane = new THREE.Mesh(
    new THREE.PlaneGeometry(ww, wh),
    new THREE.MeshBasicMaterial({ map: skyTex, fog: false }),
  );
  pane.rotation.y = rotY;
  g.add(pane);
  // 프레임(가로/세로 살)
  var fm = mat(0x2a231d, { metal: 0.2, rough: 0.6 });
  var bar = function (bw, bh) {
    var b = new THREE.Mesh(new THREE.BoxGeometry(0.06, bh, bw), fm);
    b.rotation.y = rotY;
    return b;
  };
  var mullV = bar(0.06, wh + 0.1);
  g.add(mullV);
  var mullH = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.06, ww + 0.1), fm);
  mullH.rotation.y = rotY;
  g.add(mullH);
  g.position.copy(pos);
  room.add(g);
}
/* 큰 창 — 우벽 */
windowPane(3.2, 2.4, new THREE.Vector3(W / 2 - 0.14, 2.5, 0.4), -Math.PI / 2);
/* 작은 창 — 뒤벽(주방 옆) */
windowPane(2.0, 1.9, new THREE.Vector3(-2.6, 2.5, -DP / 2 + 0.14), 0);

/* ===== 기기 레지스트리 ===== */
var devices = {};
/**
 * 기기를 씬에 등록한다(레이캐스트 id 부여 + 상태 setter + 라벨 앵커).
 * @param {string} id 기기 id
 * @param {string} label 한국어 표시명
 * @param {string} sensorKey 파이프라인 센서 엔티티 키
 * @param {THREE.Group} group 기기 3D 그룹
 * @param {THREE.Vector3} anchor 라벨 투영 앵커
 * @param {(on:boolean, init?:boolean)=>void} setter 상태 시각화 함수
 * @param {{on?:boolean, clickable?:boolean}} [opts]
 * @returns {object} 등록된 device 레코드
 */
function reg(id, label, sensorKey, group, anchor, setter, opts) {
  opts = opts || {};
  group.traverse(function (o) {
    if (o.isMesh) o.userData.deviceId = id;
  });
  scene.add(group);
  devices[id] = {
    id: id,
    label: label,
    sensorKey: sensorKey,
    group: group,
    anchor: anchor,
    on: !!opts.on,
    clickable: opts.clickable !== false,
    setter: setter,
    labelEl: null,
  };
  setter(!!opts.on, true);
  return devices[id];
}

/* --- 천장 조명 (light.living_room) --- */
(function () {
  var g = new THREE.Group();
  var cord = new THREE.Mesh(
    new THREE.CylinderGeometry(0.03, 0.03, 1.2, 6),
    mat(0x2a231d),
  );
  cord.position.y = 4.4;
  g.add(cord);
  var shade = new THREE.Mesh(
    new THREE.ConeGeometry(0.62, 0.62, 12, 1, true),
    mat(0x2a231d, { metal: 0.4, rough: 0.5 }),
  );
  shade.position.y = 3.65;
  shade.rotation.x = Math.PI;
  g.add(shade);
  var bulb = new THREE.Mesh(
    new THREE.SphereGeometry(0.26, 16, 12),
    mat(0x3a352e),
  );
  bulb.position.y = 3.45;
  g.add(bulb);
  reg(
    "light",
    "거실 조명",
    "light.living_room",
    g,
    new THREE.Vector3(0, 3.6, 0),
    function (on) {
      bulb.material.emissive.setHex(on ? 0xffd39a : 0x000000);
      bulb.material.emissiveIntensity = on ? 1.5 : 0;
      bulb.material.color.setHex(on ? 0xfff1d8 : 0x3a352e);
      roomLight.intensity = on ? 1.5 : 0.0;
    },
    { on: true },
  );
})();

/* --- 소파 (decor) --- */
(function () {
  var g = new THREE.Group();
  var seat = box(3, 0.5, 1.2, C.sofa);
  seat.position.set(0, 0.45, 0);
  var back = box(3, 1.0, 0.35, C.sofa);
  back.position.set(0, 0.9, -0.42);
  var aL = box(0.35, 0.8, 1.2, C.sofa);
  aL.position.set(-1.5, 0.6, 0);
  var aR = box(0.35, 0.8, 1.2, C.sofa);
  aR.position.set(1.5, 0.6, 0);
  [seat, back, aL, aR].forEach(function (m) {
    g.add(m);
  });
  g.position.set(-0.6, 0, -2.7);
  scene.add(g);
})();

/* --- TV (decor, 좌벽) --- */
(function () {
  var g = new THREE.Group();
  var stand = box(2.2, 0.5, 0.5, C.wood);
  stand.position.set(0, 0.25, 0);
  var panel = box(0.12, 1.4, 2.4, C.tv, { metal: 0.3, rough: 0.4 });
  panel.position.set(0.12, 1.5, 0);
  var screen = new THREE.Mesh(
    new THREE.PlaneGeometry(2.1, 1.15),
    mat(0x101820, { emissive: 0x14202a, ei: 0.8 }),
  );
  screen.rotation.y = Math.PI / 2;
  screen.position.set(0.19, 1.5, 0);
  g.add(stand);
  g.add(panel);
  g.add(screen);
  g.position.set(-4.2, 0, 0.6);
  scene.add(g);
})();

/* --- 가스레인지 (binary_sensor.kitchen_gas) — 겨눠서 조작=가스 감지 --- */
var gasBurners = [];
(function () {
  var g = new THREE.Group();
  var counter = box(2.2, 1.0, 1.4, C.wood);
  counter.position.set(0, 0.5, 0);
  var top = box(2.2, 0.08, 1.4, 0x2b2620, { metal: 0.5, rough: 0.4 });
  top.position.set(0, 1.02, 0);
  g.add(counter);
  g.add(top);
  [
    [-0.5, -0.3],
    [0.5, -0.3],
    [-0.5, 0.35],
    [0.5, 0.35],
  ].forEach(function (p) {
    var b = new THREE.Mesh(
      new THREE.CylinderGeometry(0.22, 0.22, 0.06, 12),
      mat(0x1a1613),
    );
    b.position.set(p[0], 1.08, p[1]);
    g.add(b);
    gasBurners.push(b);
  });
  var hazard = new THREE.Mesh(
    new THREE.TorusGeometry(1.15, 0.06, 8, 24),
    mat(C.ember, { emissive: C.ember, ei: 1.2 }),
  );
  hazard.rotation.x = Math.PI / 2;
  hazard.position.set(0, 1.15, 0);
  hazard.visible = false;
  g.add(hazard);
  g.position.set(2.7, 0, -2.9);
  reg(
    "gas",
    "가스레인지",
    "binary_sensor.kitchen_gas",
    g,
    new THREE.Vector3(2.7, 1.7, -2.9),
    function (on) {
      gasBurners.forEach(function (b) {
        b.material.emissive.setHex(on ? C.ember : 0x000000);
        b.material.emissiveIntensity = on ? 1.4 : 0;
      });
      hazard.visible = on;
      g.userData.hazard = hazard;
    },
    { on: false },
  );
})();

/* --- 재실 인물 (binary_sensor.living_motion) — 바닥 디스크 겨눠 토글 --- */
(function () {
  var g = new THREE.Group();
  var body = new THREE.Mesh(
    new THREE.CapsuleGeometry(0.32, 0.8, 4, 8),
    mat(C.person, { rough: 0.7 }),
  );
  body.position.y = 0.85;
  body.castShadow = true;
  var head = new THREE.Mesh(
    new THREE.SphereGeometry(0.28, 16, 12),
    mat(C.person, { rough: 0.7 }),
  );
  head.position.y = 1.55;
  head.castShadow = true;
  var figure = new THREE.Group();
  figure.add(body);
  figure.add(head);
  var disc = new THREE.Mesh(
    new THREE.CylinderGeometry(0.6, 0.6, 0.03, 20),
    mat(C.ember, {
      emissive: C.ember,
      ei: 0.4,
      transparent: true,
      opacity: 0.5,
    }),
  );
  disc.position.y = 0.03;
  g.add(figure);
  g.add(disc);
  g.position.set(1.6, 0, 1.2);
  reg(
    "person",
    "재실(사람)",
    "binary_sensor.living_motion",
    g,
    new THREE.Vector3(1.6, 2.0, 1.2),
    function (on) {
      figure.visible = on;
      disc.material.color.setHex(on ? C.ember : C.metal);
      disc.material.emissive.setHex(on ? C.ember : 0x000000);
    },
    { on: false },
  );
})();

/* --- 연기 감지 (binary_sensor.hall_smoke) — 천장 감지기 겨눠 --- */
var smokePuffs = [];
(function () {
  var g = new THREE.Group();
  var det = new THREE.Mesh(
    new THREE.CylinderGeometry(0.28, 0.28, 0.12, 16),
    mat(0xece7df, { rough: 0.6 }),
  );
  det.position.y = 4.35;
  g.add(det);
  var led = new THREE.Mesh(
    new THREE.SphereGeometry(0.05, 8, 8),
    mat(0x333, { emissive: C.safe, ei: 1 }),
  );
  led.position.set(0.14, 4.29, 0);
  g.add(led);
  g.userData.led = led;
  for (var i = 0; i < 5; i++) {
    var p = new THREE.Mesh(
      new THREE.SphereGeometry(0.3 + i * 0.06, 8, 6),
      mat(0x9a9186, { transparent: true, opacity: 0.28, rough: 1 }),
    );
    p.position.set(2.5 + (i % 2) * 0.2, 1.4 + i * 0.5, -2.6);
    p.visible = false;
    g.add(p);
    smokePuffs.push(p);
  }
  g.position.set(-1.5, 0, -1.5);
  reg(
    "smoke",
    "연기 감지기",
    "binary_sensor.hall_smoke",
    g,
    new THREE.Vector3(-1.5, 4.4, -1.5),
    function (on) {
      led.material.emissive.setHex(on ? C.ember : C.safe);
      smokePuffs.forEach(function (p) {
        p.visible = on;
      });
    },
    { on: false },
  );
})();

/* --- 누수 (binary_sensor.bath_leak) — 바닥 웅덩이 겨눠 --- */
(function () {
  var g = new THREE.Group();
  var pad = new THREE.Mesh(
    new THREE.CircleGeometry(0.7, 20),
    mat(C.metal, { transparent: true, opacity: 0.35, rough: 1 }),
  );
  pad.rotation.x = -Math.PI / 2;
  pad.position.y = 0.02;
  var puddle = new THREE.Mesh(
    new THREE.CircleGeometry(0.62, 20),
    mat(0x2f5c6e, {
      metal: 0.6,
      rough: 0.15,
      emissive: 0x11333f,
      ei: 0.6,
      transparent: true,
      opacity: 0.85,
    }),
  );
  puddle.rotation.x = -Math.PI / 2;
  puddle.position.y = 0.03;
  puddle.visible = false;
  g.add(pad);
  g.add(puddle);
  g.position.set(3.4, 0, 2.6);
  reg(
    "leak",
    "누수 센서",
    "binary_sensor.bath_leak",
    g,
    new THREE.Vector3(3.4, 0.9, 2.6),
    function (on) {
      puddle.visible = on;
      pad.material.opacity = on ? 0.1 : 0.35;
    },
    { on: false },
  );
})();

/* --- 온도계 패널 (sensor.living_temperature, 표시 전용) --- */
var thermoCanvas, thermoTex, thermoMesh;
(function () {
  var g = new THREE.Group();
  var frame = box(0.9, 0.6, 0.08, 0x2a231d, { metal: 0.3, rough: 0.5 });
  thermoCanvas = document.createElement("canvas");
  thermoCanvas.width = 256;
  thermoCanvas.height = 160;
  thermoTex = new THREE.CanvasTexture(thermoCanvas);
  var scr = new THREE.Mesh(
    new THREE.PlaneGeometry(0.78, 0.48),
    new THREE.MeshBasicMaterial({ map: thermoTex, fog: false }),
  );
  scr.position.z = 0.05;
  g.add(frame);
  g.add(scr);
  g.position.set(1.4, 1.7, -DP / 2 + 0.2);
  scene.add(g);
  thermoMesh = g;
})();
function drawThermo(t) {
  var x = thermoCanvas.getContext("2d");
  x.fillStyle = "#141014";
  x.fillRect(0, 0, 256, 160);
  x.fillStyle = "#ff5c38";
  x.font = "700 84px 'JetBrains Mono', monospace";
  x.textBaseline = "middle";
  x.fillText(t.toFixed(0) + "°", 40, 84);
  x.fillStyle = "#8f857a";
  x.font = "600 22px 'JetBrains Mono', monospace";
  x.fillText("LIVING", 44, 138);
  thermoTex.needsUpdate = true;
}

/* ===== HTML 라벨 (3D 앵커를 화면으로 투영; 상태 표시 + 미락 시 클릭 대체) ===== */
var labelsBox = $("labels");
Object.keys(devices).forEach(function (id) {
  var d = devices[id];
  var el = document.createElement("button");
  el.className = "d3-label" + (d.clickable ? " is-clickable" : "");
  el.type = "button";
  el.dataset.deviceId = id;
  el.innerHTML =
    '<span class="dl-name">' +
    esc(d.label) +
    '</span><span class="dl-state"></span>';
  if (d.clickable)
    el.addEventListener("click", function () {
      if (!locked) toggleDevice(id);
    });
  labelsBox.appendChild(el);
  d.labelEl = el;
});
function updateLabelState(d) {
  var st = d.labelEl.querySelector(".dl-state");
  var on = d.on;
  var txt = on ? "켜짐" : "꺼짐";
  if (d.id === "person") txt = on ? "있음" : "없음";
  if (d.id === "gas" || d.id === "smoke" || d.id === "leak")
    txt = on ? "감지" : "정상";
  st.textContent = txt;
  d.labelEl.classList.toggle("is-on", on);
  d.labelEl.classList.toggle(
    "is-danger",
    on && (d.id === "gas" || d.id === "smoke" || d.id === "leak"),
  );
}
Object.keys(devices).forEach(function (id) {
  updateLabelState(devices[id]);
});

var _v = new THREE.Vector3();
function positionLabels() {
  var w = mount.clientWidth,
    h = mount.clientHeight;
  Object.keys(devices).forEach(function (id) {
    var d = devices[id];
    _v.copy(d.anchor).project(camera);
    var x = (_v.x * 0.5 + 0.5) * w;
    var y = (-_v.y * 0.5 + 0.5) * h;
    var vis = _v.z < 1 && _v.x > -1.05 && _v.x < 1.05 && _v.y > -1.05;
    d.labelEl.style.display = vis ? "flex" : "none";
    d.labelEl.style.transform =
      "translate(-50%,-50%) translate(" +
      x.toFixed(1) +
      "px," +
      y.toFixed(1) +
      "px)";
  });
}

/* ===== 1인칭 컨트롤 (포인터락 + WASD) ===== */
var UP = new THREE.Vector3(0, 1, 0);
var fwd = new THREE.Vector3();
var rightV = new THREE.Vector3();
var move = new THREE.Vector3();
var yaw = 0,
  pitch = 0;
var keys = { w: false, a: false, s: false, d: false };
var locked = false;
var EYE = 1.62,
  BOUND = 4.0,
  SPEED = 3.4;
/* 걸을 수 없는 가구(원형 블로커) */
var BLOCKERS = [
  { x: -0.6, z: -2.7, r: 1.95 }, // 소파
  { x: 2.7, z: -2.9, r: 1.55 }, // 가스 카운터
];
/** 벽·가구를 통과하지 않게 위치를 보정한다. */
function collide(px, pz) {
  px = clamp(px, -BOUND, BOUND);
  pz = clamp(pz, -BOUND, BOUND);
  for (var i = 0; i < BLOCKERS.length; i++) {
    var b = BLOCKERS[i];
    var dx = px - b.x,
      dz = pz - b.z;
    var dist = Math.hypot(dx, dz);
    if (dist < b.r && dist > 1e-4) {
      px = b.x + (dx / dist) * b.r;
      pz = b.z + (dz / dist) * b.r;
    }
  }
  return [px, pz];
}

renderer.domElement.addEventListener("mousedown", function (e) {
  if (!locked) {
    renderer.domElement.requestPointerLock();
    return;
  }
  if (e.button === 0) interactTarget();
});
document.addEventListener("pointerlockchange", function () {
  locked = document.pointerLockElement === renderer.domElement;
  document.body.classList.toggle("is-locked", locked);
  $("lockHint").hidden = locked;
  if (!locked) setTargeted(null);
});
document.addEventListener("mousemove", function (e) {
  if (!locked) return;
  yaw -= e.movementX * 0.0022;
  pitch -= e.movementY * 0.0022;
  pitch = clamp(pitch, -1.15, 1.15);
  camera.rotation.set(pitch, yaw, 0);
});
var CODE = {
  KeyW: "w",
  ArrowUp: "w",
  KeyS: "s",
  ArrowDown: "s",
  KeyA: "a",
  ArrowLeft: "a",
  KeyD: "d",
  ArrowRight: "d",
};
document.addEventListener("keydown", function (e) {
  if (CODE[e.code]) {
    keys[CODE[e.code]] = true;
    if (locked) e.preventDefault();
  }
  if (!locked) return;
  if (e.code === "KeyE") interactTarget();
  else if (e.code === "KeyQ") run("tick");
  else if (e.code === "KeyF") triggerEvent();
});
document.addEventListener("keyup", function (e) {
  if (CODE[e.code]) keys[CODE[e.code]] = false;
});

/* ===== 조준 상호작용 (중앙 조준점 레이캐스트) ===== */
var ray = new THREE.Raycaster();
var CENTER = new THREE.Vector2(0, 0);
var targetedId = null;
function updateTarget() {
  ray.setFromCamera(CENTER, camera);
  var hits = ray.intersectObjects(scene.children, true);
  var found = null;
  for (var i = 0; i < hits.length; i++) {
    var id = hits[i].object.userData.deviceId;
    if (id === undefined) continue;
    if (devices[id] && devices[id].clickable && hits[i].distance < 5.0)
      found = id;
    break; // 첫 솔리드 히트에서 판정(가림 처리)
  }
  setTargeted(found);
}
function setTargeted(id) {
  if (targetedId === id) return;
  if (targetedId && devices[targetedId])
    devices[targetedId].labelEl.classList.remove("is-target");
  targetedId = id;
  var cross = $("crosshair"),
    prompt = $("lookPrompt");
  if (id && devices[id]) {
    var d = devices[id];
    d.labelEl.classList.add("is-target");
    cross.classList.add("is-active");
    var verb =
      d.id === "gas" || d.id === "smoke" || d.id === "leak"
        ? d.on
          ? "정상으로"
          : "감지 발생"
        : d.on
          ? "끄기"
          : "켜기";
    prompt.innerHTML =
      '<b>E</b> · ' + esc(d.label) + " " + esc(verb) + " <b>클릭</b>";
    prompt.hidden = false;
  } else {
    cross.classList.remove("is-active");
    prompt.hidden = true;
  }
}
function interactTarget() {
  if (targetedId) toggleDevice(targetedId);
}

function toggleDevice(id) {
  var d = devices[id];
  d.on = !d.on;
  d.setter(d.on);
  updateLabelState(d);
  pulse(d);
}
function setDevice(id, on) {
  var d = devices[id];
  if (!d) return;
  d.on = on;
  d.setter(on);
  updateLabelState(d);
}

/* 조작 시 살짝 튀는 피드백 */
var pulses = [];
function pulse(d) {
  pulses.push({ g: d.group, t: 0 });
}

/* ===== 온도 ===== */
var tempVal = 22;
function setTemp(v) {
  tempVal = v;
  $("tempV").textContent = v.toFixed(0) + "°";
  drawThermo(v);
}
setTemp(22);

/* ===== 리사이즈 ===== */
function onResize() {
  var w = mount.clientWidth,
    h = mount.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}
window.addEventListener("resize", onResize);

/* ===== 알람 펄스(가스/연기 EMBER) ===== */
var alarm = { on: false };
function setAlarm(on) {
  alarm.on = on;
}

/* ===== 애니메이션 루프 ===== */
var clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  var dt = Math.min(clock.getDelta(), 0.05);
  var el = clock.getElapsedTime();

  // 낮/밤 이징 + 적용
  stepDaylight(dt);

  // 1인칭 이동
  if (locked) {
    camera.getWorldDirection(fwd);
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-4) fwd.set(0, 0, -1);
    fwd.normalize();
    rightV.crossVectors(fwd, UP).normalize();
    move.set(0, 0, 0);
    if (keys.w) move.add(fwd);
    if (keys.s) move.sub(fwd);
    if (keys.d) move.add(rightV);
    if (keys.a) move.sub(rightV);
    if (move.lengthSq() > 0) {
      move.normalize().multiplyScalar(SPEED * dt);
      var c = collide(camera.position.x + move.x, camera.position.z + move.z);
      camera.position.x = c[0];
      camera.position.z = c[1];
    }
    camera.position.y = EYE;
    updateTarget();
  }

  // 연기 상승
  smokePuffs.forEach(function (p) {
    if (p.visible) {
      p.position.y += dt * 0.3;
      if (p.position.y > 4.6) p.position.y = 1.4;
      p.material.opacity = 0.28 * (1 - (p.position.y - 1.4) / 3.2);
    }
  });
  // 알람 시 방 조명 붉게 명멸
  if (alarm.on) {
    var f = 0.5 + 0.5 * Math.sin(el * 6);
    roomLight.color.setRGB(1, 0.3 + 0.2 * f, 0.2);
    roomLight.intensity = 1.2 + f;
  }
  // 조작 펄스
  for (var i = pulses.length - 1; i >= 0; i--) {
    var pz = pulses[i];
    pz.t += dt * 4;
    var s = 1 + Math.sin(Math.min(pz.t, Math.PI)) * 0.06;
    pz.g.scale.setScalar(s);
    if (pz.t >= Math.PI) {
      pz.g.scale.setScalar(1);
      pulses.splice(i, 1);
    }
  }
  positionLabels();
  renderer.render(scene, camera);
}

/* ===== 파이프라인 연동 ===== */
function seg(id) {
  var b = $(id);
  b.addEventListener("click", function (e) {
    var t = e.target.closest("button");
    if (!t) return;
    b.querySelectorAll("button").forEach(function (x) {
      x.classList.remove("on");
    });
    t.classList.add("on");
  });
  return function () {
    var o = b.querySelector("button.on");
    return o ? o.getAttribute("data-v") : null;
  };
}
var getCond = seg("cond");
var getTime = seg("time");
/* 시간대 버튼 → 낮/밤 시각 전환 */
$("time").addEventListener("click", function (e) {
  var t = e.target.closest("button");
  if (t) setDaylight(t.getAttribute("data-v"));
});

/**
 * 3D 기기 상태를 /api/simulate 요청 본문으로 직렬화한다.
 * @param {"tick"|"event"} mode 실행 모드
 * @param {{entity:string,value:*}} [event] 이벤트 명세
 * @returns {object} 요청 본문
 */
function readBody(mode, event) {
  var body = {
    mode: mode,
    sensors: {
      "binary_sensor.living_motion": devices.person.on,
      "light.living_room": devices.light.on,
      "binary_sensor.kitchen_gas": devices.gas.on,
      "binary_sensor.hall_smoke": devices.smoke.on,
      "binary_sensor.bath_leak": devices.leak.on,
      "sensor.living_temperature": tempVal,
      "sensor.living_humidity": 45,
    },
    config: {
      time: getTime(),
      condition: getCond(),
      llm_enabled: $("llm_on").checked,
      llm_model: "gemma3:4b",
      execution_enabled: false,
    },
  };
  if (event) body.event = event;
  return body;
}

/* --- 트레이스 렌더(컴팩트) --- */
function tierPill(t) {
  if (!t) return "";
  return (
    '<span class="tierpill is-' +
    esc(t.name) +
    '">' +
    esc(t.glyph) +
    " " +
    esc(t.ko) +
    "</span>"
  );
}
function tstage(layer, title, inner) {
  return (
    '<div class="tstage"><div class="tstage-head"><span class="tstage-layer">' +
    esc(layer) +
    "</span><h4>" +
    esc(title) +
    '</h4></div><div class="tstage-body">' +
    inner +
    "</div></div>"
  );
}
function chip(t, c) {
  return '<span class="tchip ' + (c || "") + '">' + esc(t) + "</span>";
}

/**
 * 서버의 실제 파이프라인 응답을 트레이스 드로어에 단계별로 그린다.
 * @param {object} d /api/simulate 응답 JSON
 */
function renderTrace(d) {
  var body = $("traceBody");
  if (!d.ok) {
    body.innerHTML = '<div class="trace-err">오류: ' + esc(d.error) + "</div>";
    return;
  }
  var html = "";
  var inp = d.input || {};
  html +=
    '<div class="d3-badges">' +
    chip(
      (inp.time_bucket || "") +
        " · " +
        (inp.occupancy === "vacant" ? "비어있음" : "재실"),
    ) +
    chip("조건 " + inp.condition) +
    (inp.llm && inp.llm.enabled
      ? chip(
          "LLM " +
            inp.llm.model +
            " · " +
            (d.timing_ms ? d.timing_ms.pipeline_and_llm + "ms" : ""),
          "llm",
        )
      : chip("LLM off", "muted")) +
    "</div>";
  if (d.candidates && d.candidates.length) {
    var rows = d.candidates
      .map(function (c) {
        var w = Math.max(0, Math.min(1, c.final_score)) * 100;
        var nm = c.is_noop ? "아무것도 안 함" : c.capability + "." + c.action;
        return (
          '<div class="cand-row ' +
          (c.is_selected ? "is-picked" : c.is_noop ? "is-noop" : "") +
          '">' +
          '<div class="cand-head"><span class="cand-name">' +
          esc(nm) +
          '</span><span class="cand-origin">' +
          esc(c.origin || "") +
          "</span></div>" +
          '<div class="cand-track"><i class="cand-fill" style="width:' +
          w.toFixed(1) +
          '%"></i><b class="cand-score">' +
          c.final_score.toFixed(2) +
          "</b></div></div>"
        );
      })
      .join("");
    html += tstage(
      "L2 · 후보 평가",
      "여러 행동을 점수로 겨룸",
      '<div class="cand-wrap">' +
        rows +
        "</div>" +
        (d.evaluation
          ? '<p class="tsub">' + esc(d.evaluation.rationale) + "</p>"
          : ""),
    );
  }
  if (d.gate1)
    html += tstage(
      "L3 · Gate 1",
      "결정론 안전 게이트",
      tierPill(d.gate1.floor_tier) +
        " 하한 " +
        (d.gate1.safety_critical ? chip("SAFETY", "danger") : ""),
    );
  if (d.gate2) {
    var vmap = {
      concur: "동의(승격 없음)",
      injection_suspect: "인젝션 의심·무시",
      degraded_fail_safe: "LLM 비가용→보수적",
      escalated: "승격",
    };
    html += tstage(
      "L3 · Gate 2",
      "로컬 LLM 교차 검증",
      chip(
        vmap[d.gate2.verdict] || d.gate2.verdict,
        d.gate2.degraded ? "warn" : "ok",
      ) +
        chip("conf " + d.gate2.confidence) +
        '<div class="tsub mono">' +
        esc(d.gate2.model_id) +
        "</div>",
    );
  }
  if (d.judgment)
    html += tstage(
      "L3 · 판정",
      "최종 = max(G1,G2)",
      tierPill(d.judgment.risk_tier) +
        '<p class="tsub">' +
        esc(d.judgment.rationale) +
        "</p>",
    );
  html += tstage(
    "L4 · 개입",
    d.silence ? "침묵" : "제안 도착",
    d.silence
      ? '<p class="tsub">' + esc(d.silence_reason) + "</p>"
      : tierPill(d.proposal.tier) +
          '<p class="tsub">' +
          esc(d.proposal.capability + "." + d.proposal.action) +
          " → " +
          esc(d.proposal.entity_id) +
          "</p>",
  );
  body.innerHTML = html;
  body.querySelectorAll(".cand-fill").forEach(function (el) {
    var w = el.style.width;
    el.style.width = "0";
    requestAnimationFrame(function () {
      el.style.width = w;
    });
  });
}

/* --- 휴대폰 알림 + 액추에이션 --- */
var CONF = {
  none: "확인 없이",
  light: "가벼운 확인",
  explicit: "명시적 확인",
  double: "이중 확인",
};
/**
 * 파이프라인 결과를 휴대폰 알림 카드로 띄운다(수락/거절 배선 포함).
 * 카드가 뜨면 포인터락을 풀어 마우스로 결정할 수 있게 한다.
 * @param {object} d /api/simulate 응답 JSON
 */
function showPhone(d) {
  var phone = $("phone"),
    inner = $("phoneInner");
  if (d.silence) {
    inner.innerHTML =
      '<div class="dp-silent"><p>… 아무것도 도착하지 않았습니다.</p><span>' +
      esc(d.silence_reason || "침묵") +
      "</span></div>";
    phone.hidden = false;
    setAlarm(false);
    setTimeout(function () {
      phone.hidden = true;
    }, 3200);
    return;
  }
  if (document.exitPointerLock) document.exitPointerLock();
  var p = d.proposal,
    tier = p.tier.name;
  var isSafety = tier === "ember";
  setAlarm(isSafety);
  var actLabel = isSafety ? "확인했어요" : "좋아요";
  inner.innerHTML =
    '<div class="dp-card tone-' +
    esc(tier) +
    '">' +
    '<div class="dp-head"><span class="dp-app">LIOT</span><span class="dp-tier">' +
    esc(p.tier.glyph + " " + p.tier.ko) +
    "</span></div>" +
    '<p class="dp-title">' +
    esc(phoneTitle(d)) +
    "</p>" +
    '<p class="dp-body">' +
    esc(p.rationale) +
    "</p>" +
    '<p class="dp-meta">' +
    esc(
      (CONF[p.required_confirmation] || "") +
        " · 개입 " +
        (d.intervention ? d.intervention.condition : ""),
    ) +
    "</p>" +
    '<div class="dp-actions"><button class="dp-accept">' +
    actLabel +
    '</button><button class="dp-dismiss">괜찮아요</button></div></div>';
  phone.hidden = false;
  inner.querySelector(".dp-accept").addEventListener("click", function () {
    applyActuation(d);
    closePhone();
  });
  inner.querySelector(".dp-dismiss").addEventListener("click", function () {
    setAlarm(false);
    resetRoomLight();
    closePhone();
  });
}
function phoneTitle(d) {
  var a = d.proposal.action,
    cap = d.proposal.capability;
  if (d.proposal.tier.name === "ember") return "● 즉시 확인이 필요해요";
  if (cap === "light" && a === "turn_off")
    return "거실이 비어 있어요. 조명을 꺼 둘까요?";
  if (cap === "light" && a === "turn_on") return "어두워요. 조명을 켜 둘까요?";
  if (cap === "climate") return "쌀쌀해요. 온도를 맞춰 둘까요?";
  return "제안이 하나 있어요";
}
function closePhone() {
  $("phone").hidden = true;
}
function resetRoomLight() {
  roomLight.color.setHex(0xffc98a);
  roomLight.intensity = devices.light.on ? 1.5 : 0;
}
/**
 * 수락된 제안을 3D 집에 실제로 반영한다(예: 조명 끄기 → 3D 조명 off).
 * @param {object} d /api/simulate 응답 JSON
 */
function applyActuation(d) {
  var p = d.proposal;
  setAlarm(false);
  resetRoomLight();
  if (p.capability === "light" && p.action === "turn_off")
    setDevice("light", false);
  else if (p.capability === "light" && p.action === "turn_on")
    setDevice("light", true);
  // 안전 경보(alert)는 상태 변경 없음 — 사람이 확인만.
}

/* --- 실행 --- */
var busy = false;
/**
 * 현재 3D 상태를 실제 파이프라인에 태우고 트레이스+폰을 갱신한다(중복 방지).
 * @param {"tick"|"event"} mode 실행 모드
 * @param {object} [event] 이벤트 명세
 */
function run(mode, event) {
  if (busy) return;
  busy = true;
  $("liveTag").hidden = false;
  document.querySelectorAll(".d3-run").forEach(function (b) {
    b.disabled = true;
  });
  fetch("/api/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(readBody(mode, event)),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (d) {
      renderTrace(d);
      showPhone(d);
    })
    .catch(function (e) {
      $("traceBody").innerHTML =
        '<div class="trace-err">요청 실패: ' + esc(e.message) + "</div>";
    })
    .finally(function () {
      busy = false;
      $("liveTag").hidden = true;
      document.querySelectorAll(".d3-run").forEach(function (b) {
        b.disabled = false;
      });
    });
}
/** 도크의 선택된 센서 이벤트를 발생시키고 파이프라인을 태운다. */
function triggerEvent() {
  var eid = $("eventSel").value;
  setDevice(
    eid === "binary_sensor.kitchen_gas"
      ? "gas"
      : eid === "binary_sensor.hall_smoke"
        ? "smoke"
        : "leak",
    true,
  );
  run("event", { entity: eid, value: true });
}

/* ===== 컨트롤 배선 ===== */
$("runTick").addEventListener("click", function () {
  run("tick");
});
$("runEvent").addEventListener("click", triggerEvent);
$("temp").addEventListener("input", function () {
  setTemp(parseFloat(this.value));
});
$("traceToggle").addEventListener("click", function () {
  var dr = $("traceDrawer");
  var open = dr.classList.toggle("is-collapsed");
  this.setAttribute("aria-expanded", String(!open));
});
$("reset").addEventListener("click", function () {
  setDevice("light", true);
  setDevice("person", false);
  setDevice("gas", false);
  setDevice("smoke", false);
  setDevice("leak", false);
  setTemp(22);
  setAlarm(false);
  resetRoomLight();
  closePhone();
  $("traceBody").innerHTML =
    '<div class="trace-idle"><p>기기를 겨눠 조작하고 <b>실행</b>하면<br />여기에 실제 판단 과정이 내려옵니다.</p></div>';
});

/* ===== 시작 ===== */
setDaylight("evening", true);
onResize();
animate();
$("loading").style.display = "none";
