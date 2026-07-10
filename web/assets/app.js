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

// ── 부트스트랩 ────────────────────────────────────────────────────────
/**
 * 카탈로그를 불러와 보드·레전드·파이프라인을 렌더하고 실행 버튼을 활성화한다.
 * @returns {Promise<void>}
 */
async function init() {
  renderPipeline();
  try {
    const res = await fetch("/api/catalog");
    const catalog = await res.json();

    tierByName.clear();
    for (const [value, t] of Object.entries(catalog.tiers || {})) {
      tierByName.set(t.name, { value: Number(value), labelKo: t.label_ko });
    }

    const sensorBoard = document.getElementById("sensorBoard");
    sensorBoard.replaceChildren(...(catalog.sensors || []).map(sensorCard));

    const deviceBoard = document.getElementById("deviceBoard");
    deviceBoard.replaceChildren(...(catalog.devices || []).map(deviceCard));

    renderLegend(catalog.tiers || {});

    const btn = document.getElementById("runBtn");
    btn.disabled = false;
    btn.addEventListener("click", runTick);

    const n = (catalog.sensors || []).length;
    const d = (catalog.devices || []).length;
    setStatus("ready", "대기 · 센서 " + n + " · 기기 " + d);
  } catch (err) {
    setStatus("error", "카탈로그 로드 실패");
    document.getElementById("runHint").textContent = String(err);
  }
}

document.addEventListener("DOMContentLoaded", init);
