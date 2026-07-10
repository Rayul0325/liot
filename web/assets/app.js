"use strict";

// LIOT 관제실 셸.
// 보드(센서·기기)와 위험 등급 레전드는 전부 GET /api/catalog 응답만으로 렌더한다.
// — 엔티티 ID·표시 이름·등급 한글 라벨을 이 파일에 하드코딩하지 않는다.
// 「능동 점검 실행」은 렌더된 보드 값을 읽어 POST /api/simulate 로 보낸다.

// 파이프라인은 고정 순서(엔티티가 아니라 파이프라인 정의) → 번호 있는 시퀀스가 맞다.
const PIPELINE_STAGES = [
  "트리거",
  "수집·임베딩",
  "제안 라인 A/B",
  "아비터",
  "Gate 1",
  "Gate 2",
  "L4 개입",
  "알림",
];

/**
 * 등급 name(예: "breath") → {value:number, labelKo:string}.
 * 카탈로그 응답으로 채운다.
 * @type {Map<string, {value: number, labelKo: string}>}
 */
const tierByName = new Map();

/**
 * entity_id → 표시 이름(규칙 요약에서 한글 이름을 쓰기 위한 조회표).
 * 카탈로그 렌더 시 센서 + 기기 이름으로 채운다.
 * @type {Map<string, string>}
 */
const entityNames = new Map();

/**
 * 센서로도 표현되는 entity_id 집합. 기기 카드는 여기 없는 기기에만 상태 토글을
 * 붙인다(중복 엔티티는 센서 카드가 상태를 소유 — 이중 토글 충돌 방지).
 * @type {Set<string>}
 */
const sensorEntityIds = new Set();

// 상태 술어 연산자 → 한글 라벨(규칙 빌더·요약의 단일 원천).
const OP_LABELS = { lt: "미만", le: "이하", gt: "초과", ge: "이상", eq: "같음", ne: "다름" };
// 시간대(match.time_bucket)·재실(match.occupancy) 한글 라벨.
const BUCKET_LABELS = { morning: "아침", day: "낮", evening: "저녁", night: "밤" };
const OCC_LABELS = { occupied: "재실", vacant: "부재", unknown: "미상" };

/** 마지막 카탈로그 응답(규칙 모달이 센서·기기 목록을 재사용). @type {object} */
let lastCatalog = {};

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
 * 헤더 상태 표시(색 상태 + 텍스트).
 * @param {"loading"|"ready"|"running"|"silent"|"proposal"|"error"} state
 * @param {string} text 표시 문구
 * @returns {void}
 */
function setStatus(state, text) {
  const box = document.getElementById("status");
  box.dataset.state = state;
  document.getElementById("statusText").textContent = text;
}

// ── 센서 보드 ─────────────────────────────────────────────────────────
/**
 * 센서 1개 → 카드 요소. kind 로 컨트롤(토글/슬라이더)을 고른다.
 * @param {{entity_id:string, name:string, category:string, kind:"bool"|"number", unit?:string|null, min?:number|null, max?:number|null}} sensor
 * @returns {HTMLElement}
 */
function sensorCard(sensor) {
  const card = el("article", "card card-sensor");
  card.dataset.entity = sensor.entity_id;
  card.dataset.kind = sensor.kind;

  const top = el("header", "card-top");
  top.append(el("span", "card-name", sensor.name));
  top.append(el("span", "card-cat", sensor.category));
  card.append(top);

  if (sensor.kind === "number") {
    card.append(numberControl(sensor));
  } else {
    card.append(boolControl());
  }
  return card;
}

/**
 * 수치 센서용 슬라이더 + 값 표시(단위 포함).
 * @param {{name:string, unit?:string|null, min?:number|null, max?:number|null}} sensor
 * @returns {HTMLElement}
 */
function numberControl(sensor) {
  const row = el("div", "slider-row");
  const min = Number(sensor.min ?? 0);
  const max = Number(sensor.max ?? 100);
  const start = Math.round((min + max) / 2);

  const range = el("input");
  range.type = "range";
  range.min = String(min);
  range.max = String(max);
  range.step = "1";
  range.value = String(start);
  range.setAttribute("aria-label", sensor.name);

  const out = el("output", "slider-val");
  const num = el("span", "num tabnum", String(start));
  out.append(num);
  if (sensor.unit) out.append(el("span", "unit", sensor.unit));

  range.addEventListener("input", () => {
    num.textContent = range.value;
  });

  row.append(range, out);
  return row;
}

/**
 * 불리언 센서용 토글. 켜짐/꺼짐 텍스트를 스스로 갱신한다.
 * @returns {HTMLElement}
 */
function boolControl() {
  const label = el("label", "toggle");
  const input = el("input");
  input.type = "checkbox";
  const track = el("span", "toggle-track");
  track.append(el("span", "toggle-thumb"));
  const stateText = el("span", "toggle-state", "꺼짐");

  input.addEventListener("change", () => {
    stateText.textContent = input.checked ? "켜짐" : "꺼짐";
  });

  label.append(input, track, stateText);
  return label;
}

// ── 기기·행동 보드 ────────────────────────────────────────────────────
/**
 * 기기 1개 → 카드 요소. 행동마다 등급 칩을 붙인다.
 * @param {{entity_id:string, name:string, category?:string, capability?:string, actions:{action:string, tier:string, tier_ko:string}[]}} device
 * @returns {HTMLElement}
 */
function deviceCard(device) {
  const card = el("article", "card card-device");
  card.dataset.entity = device.entity_id;

  const top = el("header", "card-top");
  top.append(el("span", "card-name", device.name));
  top.append(el("span", "card-cat", device.category ?? device.capability ?? ""));
  card.append(top);

  // 센서로도 표현되지 않는 기기는 상태(on/off) 토글을 붙인다 — 빈집→off류
  // 일반화가 이 상태를 읽어 후보를 만든다. simulate 페이로드에 포함된다.
  if (!sensorEntityIds.has(device.entity_id)) {
    card.dataset.stateful = "true";
    const row = el("div", "device-state");
    row.append(el("span", "device-state-label", "상태"));
    row.append(boolControl());
    card.append(row);
  }

  const list = el("ul", "actions");
  for (const act of device.actions) {
    const li = el("li", "action");
    li.append(el("span", "action-name", act.action));
    li.append(tierChip(act.tier, act.tier_ko));
    list.append(li);
  }
  card.append(list);
  return card;
}

/**
 * 위험 등급 칩. 색은 data-tier(value)로, 라벨은 API 가 준 tier_ko 로.
 * @param {string} tierName 등급 name(예: "breath")
 * @param {string} tierKo 등급 한글 라벨(API 제공)
 * @returns {HTMLElement}
 */
function tierChip(tierName, tierKo) {
  const meta = tierByName.get(tierName);
  const chip = el("span", "chip", tierKo);
  chip.dataset.tier = meta ? String(meta.value) : "3"; // 미상 → 최고 위험색(안전 기본)
  return chip;
}

// ── 위험 등급 레전드 ──────────────────────────────────────────────────
/**
 * 등급 레전드 렌더(value 오름차순). 라벨은 API 의 label_ko.
 * @param {Record<string, {name:string, label_ko:string}>} tiers
 * @returns {void}
 */
function renderLegend(tiers) {
  const wrap = document.getElementById("legend");
  wrap.replaceChildren();
  Object.keys(tiers)
    .sort((a, b) => Number(a) - Number(b))
    .forEach((value) => {
      const t = tiers[value];
      const chip = el("span", "chip", t.label_ko);
      chip.dataset.tier = value;
      wrap.append(chip);
    });
}

// ── 파이프라인 스트립 ─────────────────────────────────────────────────
/**
 * 고정 파이프라인 단계 스트립을 렌더한다(번호는 CSS counter).
 * @returns {void}
 */
function renderPipeline() {
  const ol = document.getElementById("pipeline");
  ol.replaceChildren();
  for (const name of PIPELINE_STAGES) {
    const li = el("li", "stage");
    li.append(el("span", "stage-name", name));
    ol.append(li);
  }
}

// ── 실행 ──────────────────────────────────────────────────────────────
/**
 * 렌더된 센서 보드 상태에서 simulate 요청 body 를 만든다
 * (하드코딩 ID 목록이 아니라 DOM 이 원천).
 * @returns {{sensors: Record<string, number|boolean>, config: {time: string}, mode: string}}
 */
function readBody() {
  const sensors = {};
  document.querySelectorAll("#sensorBoard [data-entity]").forEach((card) => {
    const eid = card.dataset.entity;
    if (card.dataset.kind === "bool") {
      sensors[eid] = card.querySelector("input[type=checkbox]").checked;
    } else {
      sensors[eid] = Number(card.querySelector("input[type=range]").value);
    }
  });
  // 상태 토글이 있는 기기(센서로 표현되지 않는 런타임 기기)의 on/off 도 포함한다.
  document.querySelectorAll('#deviceBoard [data-stateful="true"]').forEach((card) => {
    const cb = card.querySelector("input[type=checkbox]");
    if (cb) sensors[card.dataset.entity] = cb.checked;
  });
  return { sensors, config: { time: "evening" }, mode: "tick" };
}

/**
 * 능동 점검 1틱 실행: 보드 값 → POST /api/simulate → 트레이스 표시.
 * @returns {Promise<void>}
 */
async function runTick() {
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  setStatus("running", "실행 중…");
  try {
    const res = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readBody()),
    });
    const data = await res.json();
    showTrace(data);
    if (data.ok === false) {
      setStatus("error", "오류 · " + (data.error || "실패"));
      document.getElementById("runHint").textContent = data.error || "실행 실패";
    } else if (data.silence) {
      setStatus("silent", "완료 · 침묵");
      document.getElementById("runHint").textContent =
        data.silence_reason || "행동 없음(침묵)";
    } else {
      const p = data.proposal;
      setStatus("proposal", "완료 · 제안");
      document.getElementById("runHint").textContent = p
        ? "제안: " + p.capability + "." + p.action + " (" + (p.tier?.ko ?? "") + ")"
        : "제안 생성됨";
    }
  } catch (err) {
    setStatus("error", "요청 실패");
    document.getElementById("runHint").textContent = String(err);
  } finally {
    btn.disabled = false;
  }
}

/**
 * 응답 트레이스를 접이식 원시 패널에 덤프하고 펼친다(실제 시각화는 P6).
 * @param {object} data /api/simulate 응답
 * @returns {void}
 */
function showTrace(data) {
  document.getElementById("rawJson").textContent = JSON.stringify(data, null, 2);
  const panel = document.getElementById("rawPanel");
  panel.hidden = false;
  panel.open = true;
}

// ── 카탈로그 적용/리로드 ──────────────────────────────────────────────
/**
 * 등급 name→value·labelKo 사전(모달 select 옵션에도 재사용).
 * @type {Record<string, {name:string, label_ko:string}>}
 */
let catalogTiers = {};

/**
 * 카탈로그 응답을 받아 등급 사전·보드·레전드를 렌더하고 상태를 갱신한다.
 * @param {{sensors?:object[], devices?:object[], tiers?:object}} catalog
 * @returns {void}
 */
function applyCatalog(catalog) {
  lastCatalog = catalog;
  catalogTiers = catalog.tiers || {};

  tierByName.clear();
  for (const [value, t] of Object.entries(catalogTiers)) {
    tierByName.set(t.name, { value: Number(value), labelKo: t.label_ko });
  }

  entityNames.clear();
  sensorEntityIds.clear();
  for (const s of catalog.sensors || []) {
    entityNames.set(s.entity_id, s.name);
    sensorEntityIds.add(s.entity_id);
  }
  for (const dev of catalog.devices || []) entityNames.set(dev.entity_id, dev.name);

  document
    .getElementById("sensorBoard")
    .replaceChildren(...(catalog.sensors || []).map(sensorCard));
  document
    .getElementById("deviceBoard")
    .replaceChildren(...(catalog.devices || []).map(deviceCard));
  renderRules(catalog.rules || []);
  renderLegend(catalogTiers);

  const n = (catalog.sensors || []).length;
  const d = (catalog.devices || []).length;
  setStatus("ready", "대기 · 센서 " + n + " · 기기 " + d);
}

/**
 * entity_id → 표시 이름(없으면 entity_id 그대로).
 * @param {string} eid
 * @returns {string}
 */
function nameFor(eid) {
  return entityNames.get(eid) || eid;
}

/**
 * 규칙 1개 → 한글 요약 문자열.
 * 예: "토양 수분 미만 30 · 부재 → 로봇청소기 start 제안".
 * @param {{match?:object, state?:{entity:string,op:string,value:any}[], target:string, action:string}} rule
 * @returns {string}
 */
function ruleSummary(rule) {
  const conds = [];
  for (const [k, v] of Object.entries(rule.match || {})) {
    if (k === "time_bucket") conds.push(BUCKET_LABELS[v] || v);
    else if (k === "occupancy") conds.push(OCC_LABELS[v] || v);
    else conds.push(k + "=" + v);
  }
  for (const p of rule.state || []) {
    conds.push(nameFor(p.entity) + " " + (OP_LABELS[p.op] || p.op) + " " + p.value);
  }
  const cond = conds.length ? conds.join(" · ") : "무조건";
  return cond + " → " + nameFor(rule.target) + " " + rule.action + " 제안";
}

/**
 * 등록된 규칙 패널을 렌더한다(비면 안내 문구).
 * @param {object[]} rules
 * @returns {void}
 */
function renderRules(rules) {
  const wrap = document.getElementById("ruleBoard");
  if (!wrap) return;
  if (!rules.length) {
    wrap.replaceChildren(el("li", "rule-empty", "등록된 규칙이 없습니다"));
    return;
  }
  wrap.replaceChildren(
    ...rules.map((rule) => {
      const li = el("li", "rule-item");
      li.append(el("span", "rule-text", ruleSummary(rule)));
      return li;
    }),
  );
}

/**
 * 카탈로그를 다시 불러와 보드를 재렌더한다(등록 성공 후 호출).
 * @returns {Promise<void>}
 */
async function reloadCatalog() {
  const res = await fetch("/api/catalog");
  applyCatalog(await res.json());
}

// ── 부트스트랩 ────────────────────────────────────────────────────────
/**
 * 카탈로그를 불러와 보드·레전드·파이프라인을 렌더하고 버튼을 활성화한다.
 * @returns {Promise<void>}
 */
async function init() {
  renderPipeline();
  try {
    const res = await fetch("/api/catalog");
    applyCatalog(await res.json());

    const btn = document.getElementById("runBtn");
    btn.disabled = false;
    btn.addEventListener("click", runTick);

    document
      .getElementById("addSensorBtn")
      .addEventListener("click", openSensorModal);
    document
      .getElementById("addDeviceBtn")
      .addEventListener("click", openDeviceModal);
    document
      .getElementById("addRuleBtn")
      .addEventListener("click", openRuleModal);
  } catch (err) {
    setStatus("error", "카탈로그 로드 실패");
    document.getElementById("runHint").textContent = String(err);
  }
}

document.addEventListener("DOMContentLoaded", init);

// ── 등록 모달 (센서 · 기기) ────────────────────────────────────────────
/**
 * JSON POST 헬퍼. 상태코드와 파싱된 응답을 함께 돌려준다.
 * @param {string} url 엔드포인트
 * @param {object} payload 본문
 * @returns {Promise<{status:number, data:any}>}
 */
async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data;
  try {
    data = await res.json();
  } catch (_) {
    data = { ok: false, error: "응답 파싱 실패" };
  }
  return { status: res.status, data };
}

/** 열린 모달을 닫는다. */
function closeModal() {
  document.getElementById("modalRoot").replaceChildren();
  document.removeEventListener("keydown", onModalKey);
}

/**
 * Esc 로 모달 닫기.
 * @param {KeyboardEvent} e
 */
function onModalKey(e) {
  if (e.key === "Escape") closeModal();
}

/**
 * 모달 셸을 만들어 modalRoot 에 마운트한다(오버레이 클릭·× 로 닫힘).
 * @param {string} title 제목
 * @param {HTMLFormElement} form 폼 요소
 * @returns {void}
 */
function mountModal(title, form) {
  const overlay = el("div", "modal-overlay");
  const modal = el("div", "modal");
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");

  const head = el("div", "modal-head");
  head.append(el("h3", "modal-title", title));
  const x = el("button", "modal-x", "×");
  x.type = "button";
  x.setAttribute("aria-label", "닫기");
  x.addEventListener("click", closeModal);
  head.append(x);

  modal.append(head, form);
  overlay.append(modal);
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeModal();
  });

  document.getElementById("modalRoot").replaceChildren(overlay);
  document.addEventListener("keydown", onModalKey);
  const first = form.querySelector("input, select");
  if (first) first.focus();
}

/**
 * 라벨 + 단일 컨트롤 필드(label 요소 — 클릭 시 포커스).
 * @param {string} labelText 라벨
 * @param {HTMLElement} control input/select
 * @param {string} [hint] 보조 설명
 * @returns {HTMLElement}
 */
function field(labelText, control, hint) {
  const wrap = el("label", "field");
  wrap.append(el("span", "field-label", labelText));
  wrap.append(control);
  if (hint) wrap.append(el("span", "field-hint", hint));
  return wrap;
}

/**
 * 라벨 + 복합 컨트롤 그룹(div — label 중첩 회피).
 * @param {string} labelText 라벨
 * @param {HTMLElement} node 그룹 노드
 * @returns {HTMLElement}
 */
function fieldGroup(labelText, node) {
  const wrap = el("div", "field");
  wrap.append(el("span", "field-label", labelText));
  wrap.append(node);
  return wrap;
}

/**
 * input 요소 생성.
 * @param {string} name name 속성
 * @param {Record<string,string>} [attrs] 추가 속성
 * @returns {HTMLInputElement}
 */
function makeInput(name, attrs) {
  const i = el("input");
  i.name = name;
  for (const [k, v] of Object.entries(attrs || {})) i.setAttribute(k, v);
  return i;
}

/**
 * 폼 하단(에러 라인 + 취소/등록 버튼).
 * @param {string} submitText 등록 버튼 문구
 * @returns {{error: HTMLElement, actions: HTMLElement}}
 */
function formFooter(submitText) {
  const error = el("p", "modal-error");
  error.hidden = true;
  error.setAttribute("role", "alert");
  const actions = el("div", "modal-actions");
  const cancel = el("button", "btn-secondary", "취소");
  cancel.type = "button";
  cancel.addEventListener("click", closeModal);
  const submit = el("button", "btn-primary", submitText);
  submit.type = "submit";
  actions.append(cancel, submit);
  return { error, actions };
}

/**
 * 인라인 에러를 표시한다(서버 400 사유 그대로).
 * @param {HTMLElement} errorEl 에러 요소
 * @param {string} msg 문구
 * @returns {void}
 */
function showError(errorEl, msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

/**
 * 「안전 임계 카테고리로 표시」 체크박스 필드.
 * 체크 시 등록 요청 본문에 safety_critical:true 가 실려, 서버가 이 카테고리를
 * 프로세스 전역 안전 집합에 확장 등록한다(항상 EMBER, 해제 불가).
 * @returns {{wrap: HTMLElement, input: HTMLInputElement}}
 */
function safetyField() {
  const input = el("input", "safety-check");
  input.type = "checkbox";
  input.name = "safety_critical";
  const label = el("label", "safety-field");
  label.append(input, el("span", "safety-label", "안전 임계 카테고리로 표시"));
  const wrap = el("div", "field");
  wrap.append(label);
  wrap.append(
    el(
      "span",
      "field-hint",
      "안전 카테고리는 항상 불씨(EMBER) 등급으로 취급되며 해제할 수 없습니다",
    ),
  );
  return { wrap, input };
}

/** 「센서 추가」 모달을 연다. */
function openSensorModal() {
  const form = el("form", "modal-form");

  const name = makeInput("name", { type: "text", placeholder: "예: 토양 수분" });
  const eid = makeInput("entity_id", {
    type: "text",
    placeholder: "예: sensor.soil_moisture",
  });
  const category = makeInput("category", { type: "text", placeholder: "예: moisture" });

  const kind = el("select", "field-select");
  for (const [v, label] of [
    ["bool", "켜짐/꺼짐 (bool)"],
    ["number", "수치 (number)"],
  ]) {
    const o = el("option", null, label);
    o.value = v;
    kind.append(o);
  }

  const unit = makeInput("unit", { type: "text", placeholder: "예: %" });
  const min = makeInput("min", { type: "number", value: "0" });
  const max = makeInput("max", { type: "number", value: "100" });
  const numFields = el("div", "field-inline");
  numFields.append(field("단위", unit), field("최소", min), field("최대", max));
  numFields.hidden = true; // 기본 kind = bool

  kind.addEventListener("change", () => {
    numFields.hidden = kind.value !== "number";
  });

  const safety = safetyField();

  const { error, actions } = formFooter("센서 등록");
  form.append(
    field("이름", name),
    field("entity_id", eid, "비우면 카테고리로 자동 생성됩니다"),
    field("종류", kind),
    field("카테고리", category),
    numFields,
    safety.wrap,
    error,
    actions,
  );

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      name: name.value.trim(),
      entity_id: eid.value.trim(),
      category: category.value.trim(),
      kind: kind.value,
      safety_critical: safety.input.checked,
    };
    if (kind.value === "number") {
      payload.unit = unit.value.trim();
      payload.min = min.value === "" ? null : Number(min.value);
      payload.max = max.value === "" ? null : Number(max.value);
    }
    const { status, data } = await postJson("/api/registry/sensor", payload);
    if (status === 200 && data.ok !== false) {
      closeModal();
      await reloadCatalog();
    } else {
      showError(error, data.error || "등록 실패");
    }
  });

  mountModal("센서 추가", form);
}

/**
 * 위험 등급 select(옵션 = /api/catalog tiers, value 오름차순).
 * 기본 선택은 breath(능동 제안 가능한 안전한 기본).
 * @returns {HTMLSelectElement}
 */
function tierSelect() {
  const sel = el("select", "field-select tier-select");
  Object.keys(catalogTiers)
    .sort((a, b) => Number(a) - Number(b))
    .forEach((value) => {
      const t = catalogTiers[value];
      const o = el("option", null, t.label_ko + " (" + t.name + ")");
      o.value = t.name;
      sel.append(o);
    });
  if (tierByName.has("breath")) sel.value = "breath";
  return sel;
}

/** 기기 모달의 행동 1행(행동 이름 + 등급 + 삭제). */
function actionRow() {
  const row = el("div", "action-row");
  const action = makeInput("action", { type: "text", placeholder: "행동 (예: start)" });
  action.classList.add("action-input");
  const tier = tierSelect();
  const remove = el("button", "row-x", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "행동 삭제");
  remove.addEventListener("click", () => row.remove());
  row.append(action, tier, remove);
  return row;
}

/** 「기기·행동 추가」 모달을 연다. */
function openDeviceModal() {
  const form = el("form", "modal-form");

  const name = makeInput("name", { type: "text", placeholder: "예: 로봇청소기" });
  const eid = makeInput("entity_id", { type: "text", placeholder: "예: vacuum.robot" });
  const capability = makeInput("capability", { type: "text", placeholder: "예: vacuum" });
  const category = makeInput("category", { type: "text", placeholder: "예: appliance" });

  const actionsWrap = el("div", "actions-wrap");
  actionsWrap.append(actionRow());
  const addRow = el("button", "btn-secondary btn-add-row", "+ 행동 추가");
  addRow.type = "button";
  addRow.addEventListener("click", () => actionsWrap.append(actionRow()));

  const safety = safetyField();

  const { error, actions } = formFooter("기기 등록");
  form.append(
    field("이름", name),
    field("entity_id", eid, "비우면 capability 로 자동 생성됩니다"),
    field("capability", capability),
    field("카테고리", category),
    fieldGroup("행동 · 위험 등급", actionsWrap),
    addRow,
    safety.wrap,
    error,
    actions,
  );

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const acts = [];
    actionsWrap.querySelectorAll(".action-row").forEach((row) => {
      const a = row.querySelector(".action-input").value.trim();
      const t = row.querySelector(".tier-select").value;
      if (a) acts.push({ action: a, tier: t });
    });
    const payload = {
      name: name.value.trim(),
      entity_id: eid.value.trim(),
      capability: capability.value.trim(),
      category: category.value.trim(),
      actions: acts,
      safety_critical: safety.input.checked,
    };
    const { status, data } = await postJson("/api/registry/device", payload);
    if (status === 200 && data.ok !== false) {
      closeModal();
      await reloadCatalog();
    } else {
      showError(error, data.error || "등록 실패");
    }
  });

  mountModal("기기·행동 추가", form);
}

// ── 규칙 추가 모달 ─────────────────────────────────────────────────────
/**
 * value 오름차순 옵션 select 를 만든다([value, label] 배열).
 * @param {[string, string][]} pairs
 * @returns {HTMLSelectElement}
 */
function optionSelect(pairs) {
  const sel = el("select", "field-select");
  for (const [v, label] of pairs) {
    const o = el("option", null, label);
    o.value = v;
    sel.append(o);
  }
  return sel;
}

/**
 * 조건 한 줄(연산자 select + 값 input)을 만든다.
 * @param {HTMLSelectElement} opSel
 * @param {HTMLInputElement} valueInput
 * @returns {HTMLElement}
 */
function opValueRow(opSel, valueInput) {
  const row = el("div", "op-value-row");
  row.append(opSel, valueInput);
  return row;
}

/**
 * 기기 행동 옵션들을 채운다. tier≤breath(잔잔·숨결)만 활성, 나머지는
 * "능동 제안 불가(등급)"로 비활성. value = "entity_id|capability|action".
 * @param {HTMLSelectElement} sel
 * @returns {number} 활성(선택 가능) 옵션 수
 */
function fillActionOptions(sel) {
  let enabledCount = 0;
  for (const dev of lastCatalog.devices || []) {
    for (const act of dev.actions || []) {
      const meta = tierByName.get(act.tier);
      const canPropose = meta ? meta.value <= 1 : false; // ≤ breath
      const o = el(
        "option",
        null,
        dev.name +
          " · " +
          act.action +
          " (" +
          act.tier_ko +
          ")" +
          (canPropose ? "" : " — 능동 제안 불가(등급)"),
      );
      o.value = dev.entity_id + "|" + (dev.capability || "") + "|" + act.action;
      if (!canPropose) o.disabled = true;
      else enabledCount += 1;
      sel.append(o);
    }
  }
  return enabledCount;
}

/** 「규칙 추가」 모달을 연다: "센서 X가 [연산자] V이면 → 행동 Y 제안". */
function openRuleModal() {
  const form = el("form", "modal-form");

  const name = makeInput("name", { type: "text", placeholder: "예: 토양 건조 시 청소" });

  const sensorSel = optionSelect(
    (lastCatalog.sensors || []).map((s) => [s.entity_id, s.name + " (" + s.entity_id + ")"]),
  );

  const opSel = optionSelect(
    Object.entries(OP_LABELS).map(([v, label]) => [v, label + " (" + v + ")"]),
  );
  const valueInput = makeInput("value", { type: "text", placeholder: "예: 30" });

  const actionSel = el("select", "field-select");
  const enabled = fillActionOptions(actionSel);

  const bucketSel = optionSelect([
    ["", "(없음)"],
    ["morning", "아침"],
    ["day", "낮"],
    ["evening", "저녁"],
    ["night", "밤"],
  ]);
  const occSel = optionSelect([
    ["", "(없음)"],
    ["occupied", "재실"],
    ["vacant", "부재"],
  ]);

  const { error, actions } = formFooter("규칙 등록");
  form.append(
    field("규칙 이름", name, "비우면 자동 생성됩니다"),
    field("센서", sensorSel),
    fieldGroup("조건 (연산자 · 값)", opValueRow(opSel, valueInput)),
    field("행동", actionSel, "능동 제안 가능한 등급(잔잔·숨결)만 선택됩니다"),
    field("시간대 (선택)", bucketSel),
    field("재실 (선택)", occSel),
    error,
    actions,
  );

  if (!(lastCatalog.sensors || []).length) {
    showError(error, "먼저 센서를 등록하세요.");
  } else if (!enabled) {
    showError(error, "능동 제안 가능한 행동이 없습니다. 숨결 이하 행동을 먼저 등록하세요.");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const av = actionSel.value;
    if (!av) {
      showError(error, "행동을 선택하세요.");
      return;
    }
    const [entity_id, capability, action] = av.split("|");
    const match = {};
    if (bucketSel.value) match.time_bucket = bucketSel.value;
    if (occSel.value) match.occupancy = occSel.value;
    const raw = valueInput.value.trim();
    const num = Number(raw);
    const value = raw !== "" && !Number.isNaN(num) ? num : raw;
    const payload = {
      name: name.value.trim(),
      match,
      state: [{ entity: sensorSel.value, op: opSel.value, value }],
      propose: { entity_id, capability, action },
    };
    const { status, data } = await postJson("/api/registry/rule", payload);
    if (status === 200 && data.ok !== false) {
      closeModal();
      await reloadCatalog();
    } else {
      showError(error, data.error || "등록 실패");
    }
  });

  mountModal("규칙 추가", form);
}
