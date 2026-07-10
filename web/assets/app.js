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
  catalogTiers = catalog.tiers || {};

  tierByName.clear();
  for (const [value, t] of Object.entries(catalogTiers)) {
    tierByName.set(t.name, { value: Number(value), labelKo: t.label_ko });
  }

  document
    .getElementById("sensorBoard")
    .replaceChildren(...(catalog.sensors || []).map(sensorCard));
  document
    .getElementById("deviceBoard")
    .replaceChildren(...(catalog.devices || []).map(deviceCard));
  renderLegend(catalogTiers);

  const n = (catalog.sensors || []).length;
  const d = (catalog.devices || []).length;
  setStatus("ready", "대기 · 센서 " + n + " · 기기 " + d);
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

  const { error, actions } = formFooter("센서 등록");
  form.append(
    field("이름", name),
    field("entity_id", eid, "비우면 카테고리로 자동 생성됩니다"),
    field("종류", kind),
    field("카테고리", category),
    numFields,
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

  const { error, actions } = formFooter("기기 등록");
  form.append(
    field("이름", name),
    field("entity_id", eid, "비우면 capability 로 자동 생성됩니다"),
    field("capability", capability),
    field("카테고리", category),
    fieldGroup("행동 · 위험 등급", actionsWrap),
    addRow,
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
