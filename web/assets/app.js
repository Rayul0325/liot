"use strict";

// LIOT 관제실 셸.
// 보드(센서·기기)와 위험 등급 레전드는 전부 GET /api/catalog 응답만으로 렌더한다.
// — 엔티티 ID·표시 이름·등급 한글 라벨을 이 파일에 하드코딩하지 않는다.
// 「능동 점검 실행」은 렌더된 보드 값을 읽어 POST /api/simulate 로 보낸다.

// 파이프라인은 고정 순서(엔티티가 아니라 파이프라인 정의) → 번호 있는 시퀀스가 맞다.
// key = 트레이스 필드 매핑용, name = 발표장 표기. 데이터가 좌→우로 흐르며 채워진다.
const STAGES = [
  { key: "trigger", name: "트리거" },
  { key: "collect", name: "수집·임베딩" },
  { key: "lines", name: "제안 라인 A/B" },
  { key: "arbiter", name: "아비터" },
  { key: "gate1", name: "Gate 1 · 결정론" },
  { key: "gate2", name: "Gate 2 · LLM" },
  { key: "l4", name: "L4 개입" },
  { key: "notify", name: "알림" },
];

// 스테이지 순차 점등 간격(ms). prefers-reduced-motion 이면 0(즉시 전부 표시).
const STAGE_STAGGER_MS = 150;

// 판정/결정 값 → 한글 라벨(발표장 가독성). 미상 값은 원문 폴백.
const CONFIRM_LABELS = { none: "확인 불필요", explicit: "명시 확인", implicit: "암묵 확인" };
const EXEC_LABELS = { notify_only: "알림만", auto: "자동 실행", blocked: "차단", suppressed: "억제" };
const GATE2_LABELS = { concur: "동의", override: "기각", abstain: "판단 보류", degrade: "성능 저하" };
// 지시(directive) 제약 → 한글 라벨.
const CONSTRAINT_LABELS = { keep_on: "계속 켜 둠", keep_off: "계속 꺼 둠", forbid: "금지", prefer: "선호" };
// 피드백 신호 → 한글 라벨.
const SIGNAL_LABELS = { accepted: "승인", dismissed: "거절", ignored: "무시" };

/** 마지막 실행 트레이스(스테이지 상세 클릭·폰 피드백이 참조). @type {object|null} */
let lastRun = null;
/** 현재 펼친 스테이지 key(토글용). @type {string|null} */
let openStage = null;

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

// ── 파이프라인 시각화 (센터피스) ──────────────────────────────────────
/** prefers-reduced-motion 이면 애니메이션 없이 즉시 표시한다. */
function prefersReducedMotion() {
  return (
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * 8개 스테이지 카드를 렌더한다(클릭 시 상세 드로어 토글). 데이터는 실행할 때
 * fillStage 가 채운다(초기엔 미점등).
 * @returns {void}
 */
function renderPipeline() {
  const ol = document.getElementById("pipeline");
  ol.replaceChildren();
  STAGES.forEach((stage, i) => {
    const li = el("li", "stage");
    li.dataset.stage = stage.key;
    li.dataset.lit = "0";
    li.setAttribute("role", "button");
    li.setAttribute("tabindex", "0");
    li.setAttribute("aria-expanded", "false");
    li.setAttribute("aria-label", stage.name + " 단계 상세 보기");

    const head = el("div", "stage-head");
    head.append(el("span", "stage-num tabnum", String(i + 1)));
    head.append(el("span", "stage-name", stage.name));
    head.append(el("span", "stage-chip"));
    li.append(head);
    li.append(el("div", "stage-body"));

    const activate = () => toggleStageDetail(stage.key);
    li.addEventListener("click", activate);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate();
      }
    });
    ol.append(li);
  });
}

/** 모든 스테이지를 소등·비우고 상세 드로어를 닫는다(새 실행 시작 시). */
function resetPipeline() {
  document.querySelectorAll("#pipeline .stage").forEach((li) => {
    li.dataset.lit = "0";
    delete li.dataset.outcome;
    li.classList.remove("is-open");
    li.setAttribute("aria-expanded", "false");
    li.querySelector(".stage-body").replaceChildren();
    li.querySelector(".stage-chip").replaceChildren();
  });
  const detail = document.getElementById("stageDetail");
  detail.hidden = true;
  detail.replaceChildren();
  openStage = null;
}

/**
 * 한 스테이지를 실제 데이터로 채우고 점등한다(head 등급칩 + body 내용).
 * @param {string} key 스테이지 key
 * @param {object} data /api/simulate 응답
 * @returns {void}
 */
function fillStage(key, data) {
  const li = document.querySelector('#pipeline [data-stage="' + key + '"]');
  if (!li) return;
  li.querySelector(".stage-body").replaceChildren(stageBody(key, data));
  const chipSlot = li.querySelector(".stage-chip");
  chipSlot.replaceChildren();
  const chip = stageChip(key, data);
  if (chip) chipSlot.append(chip);
  li.dataset.lit = "1";
  if (key === "notify") li.dataset.outcome = data.silence ? "silence" : "proposal";
}

/**
 * 스테이지를 순차 점등(좌→우)하며 데이터를 흘려보낸다. reduced-motion 이면 즉시.
 * @param {object} data /api/simulate 응답
 * @returns {Promise<void>} 마지막 스테이지까지 점등되면 resolve.
 */
function animateRun(data) {
  return new Promise((resolve) => {
    const step = prefersReducedMotion() ? 0 : STAGE_STAGGER_MS;
    let remaining = STAGES.length;
    STAGES.forEach((stage, i) => {
      const reveal = () => {
        fillStage(stage.key, data);
        remaining -= 1;
        if (remaining === 0) resolve();
      };
      if (step === 0) reveal();
      else window.setTimeout(reveal, i * step);
    });
  });
}

/**
 * 트레이스 등급 객체 → 등급 칩(색은 level, 라벨은 API 의 ko).
 * @param {{level:number, ko:string}|null|undefined} t
 * @returns {HTMLElement|null}
 */
function traceTier(t) {
  if (!t) return null;
  const chip = el("span", "chip", t.ko);
  chip.dataset.tier = String(t.level);
  return chip;
}

/** 스테이지 head 에 붙는 등급 칩(Gate1 하한·L4 판정·알림 제안 등급). */
function stageChip(key, data) {
  if (key === "gate1" && data.gate1) return traceTier(data.gate1.floor_tier);
  if (key === "l4" && data.judgment) return traceTier(data.judgment.risk_tier);
  if (key === "notify" && data.proposal) return traceTier(data.proposal.tier);
  return null;
}

/** 라벨+값 마이크로 행. 빈 값은 "—". */
function metric(label, value) {
  const row = el("div", "metric");
  row.append(el("span", "metric-k", label));
  const v = value == null || value === "" ? "—" : String(value);
  row.append(el("span", "metric-v", v));
  return row;
}

/** 데이터 미도달 스테이지용 흐린 안내(침묵 흐름). */
function stageMuted(text) {
  return el("p", "stage-muted", text);
}

/** 긴 문자열 축약(발표장 카드 밀도 관리). */
function abbrev(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** "proactive:rule:vacant-light-off@hash" → "vacant-light-off". */
function shortenOrigin(origin) {
  const s = String(origin).split("@")[0];
  const parts = s.split(":");
  return parts[parts.length - 1] || s;
}

// ── 스테이지 body 빌더 (트레이스 필드 → 카드 내용) ────────────────────
/** 스테이지 key → body 노드 디스패처. */
function stageBody(key, data) {
  switch (key) {
    case "trigger":
      return bodyTrigger(data);
    case "collect":
      return bodyCollect(data);
    case "lines":
      return bodyLines(data);
    case "arbiter":
      return bodyArbiter(data);
    case "gate1":
      return bodyGate1(data);
    case "gate2":
      return bodyGate2(data);
    case "l4":
      return bodyL4(data);
    case "notify":
      return bodyNotify(data);
    default:
      return el("span");
  }
}

function bodyTrigger(data) {
  const t = data.trigger || {};
  const wrap = el("div", "stage-fields");
  wrap.append(metric("종류", t.kind === "event" ? "이벤트" : "능동 틱"));
  wrap.append(metric("근원", t.origin ? shortenOrigin(t.origin) : "—"));
  return wrap;
}

function bodyCollect(data) {
  const wrap = el("div", "stage-fields");
  const input = data.input || {};
  wrap.append(metric("시간대", BUCKET_LABELS[input.time_bucket] || input.time_bucket));
  wrap.append(metric("재실", OCC_LABELS[input.occupancy] || input.occupancy));
  const emb = data.embedding;
  if (emb) {
    const row = el("div", "metric");
    row.append(el("span", "metric-k", "선호 셀"));
    const v = el("span", "metric-v tabnum", Number(emb.preference).toFixed(2));
    if (emb.preference > 0.5) v.classList.add("pref-up");
    else if (emb.preference < 0.5) v.classList.add("pref-down");
    row.append(v);
    wrap.append(row);
  }
  const snap = data.snapshot;
  if (snap && snap.nl_summary) {
    wrap.append(el("p", "stage-summary", abbrev(snap.nl_summary, 68)));
  }
  const dirs = data.directives && data.directives.active;
  if (dirs && dirs.length) {
    wrap.append(el("p", "stage-note", "지시 " + dirs.length + "건 반영"));
  }
  return wrap;
}

function bodyLines(data) {
  const lines = data.lines || { line_a: [], line_b: [] };
  const wrap = el("div", "lines-mini");
  wrap.append(lineColumn("A · 규칙/ML", lines.line_a || []));
  wrap.append(lineColumn("B · LLM", lines.line_b || []));
  return wrap;
}

function lineColumn(title, items) {
  const col = el("div", "line-col");
  col.append(el("span", "line-title", title));
  if (!items.length) {
    col.append(el("span", "line-empty", "—"));
    return col;
  }
  for (const it of items) {
    const row = el("div", "line-item");
    row.append(el("span", "line-label", it.label));
    row.append(el("span", "line-conf tabnum", Number(it.line_confidence).toFixed(2)));
    col.append(row);
  }
  return col;
}

function bodyArbiter(data) {
  if (!data.arbiter) return stageMuted("아비터 미도달");
  const cands = [...(data.candidates || [])]
    .sort((a, b) => b.final_score - a.final_score)
    .slice(0, 3);
  const max = Math.max(0.0001, ...cands.map((c) => Math.abs(c.final_score)));
  const wrap = el("div", "arb-mini");
  for (const c of cands) {
    const row = el("div", "arb-row");
    if (c.is_selected) row.classList.add("is-selected");
    row.append(el("span", "arb-label", c.is_noop ? "무행동" : c.capability + "." + c.action));
    const barwrap = el("span", "arb-barwrap");
    const bar = el("span", "arb-bar");
    bar.style.width = Math.max(4, (c.final_score / max) * 100) + "%";
    barwrap.append(bar);
    row.append(barwrap);
    row.append(el("span", "arb-val tabnum", Number(c.final_score).toFixed(2)));
    wrap.append(row);
  }
  return wrap;
}

function bodyGate1(data) {
  const g = data.gate1;
  if (!g) return stageMuted("Gate 1 미도달");
  const wrap = el("div", "stage-fields");
  const row = el("div", "metric");
  row.append(el("span", "metric-k", "판정"));
  const tag = el("span", "verdict-tag", g.blocked ? "차단" : "허용");
  tag.dataset.kind = g.blocked ? "block" : "allow";
  row.append(tag);
  wrap.append(row);
  if (g.safety_critical) wrap.append(el("p", "stage-note safety", "안전 임계 카테고리"));
  const rules = g.matched_rules || [];
  wrap.append(metric("매칭 규칙", rules.length ? rules.join(", ") : "없음"));
  return wrap;
}

function bodyGate2(data) {
  const g = data.gate2;
  if (!g) return stageMuted("Gate 2 미도달 (침묵)");
  const wrap = el("div", "stage-fields");
  const row = el("div", "metric");
  row.append(el("span", "metric-k", "판정"));
  const tag = el("span", "verdict-tag", GATE2_LABELS[g.verdict] || g.verdict);
  tag.dataset.kind = g.verdict === "concur" ? "allow" : "warn";
  row.append(tag);
  wrap.append(row);
  wrap.append(metric("모델", g.model_id));
  wrap.append(metric("신뢰도", Number(g.confidence).toFixed(2)));
  if (g.degraded) wrap.append(el("p", "stage-note", "성능 저하 모드"));
  return wrap;
}

function bodyL4(data) {
  const iv = data.intervention;
  if (!iv) return stageMuted("개입 없음 (침묵)");
  const wrap = el("div", "stage-fields");
  wrap.append(metric("조건", iv.condition));
  wrap.append(metric("확인", CONFIRM_LABELS[iv.required_confirmation] || iv.required_confirmation));
  wrap.append(metric("실행", EXEC_LABELS[iv.execution_decision] || iv.execution_decision));
  return wrap;
}

function bodyNotify(data) {
  if (data.silence) {
    const wrap = el("div", "silence-card");
    wrap.append(el("span", "silence-badge", "침묵"));
    wrap.append(el("p", "silence-text", "제안 없음 (fail-closed)"));
    wrap.append(el("p", "silence-reason", abbrev(data.silence_reason, 64)));
    return wrap;
  }
  const p = data.proposal;
  if (!p) return stageMuted("—");
  const wrap = el("div", "notify-card");
  wrap.append(el("span", "notify-dev", nameFor(p.entity_id)));
  wrap.append(el("span", "notify-act", p.capability + "." + p.action));
  return wrap;
}

// ── 스테이지 상세 드로어 ──────────────────────────────────────────────
/** 스테이지 클릭 → 상세 드로어 토글(같은 스테이지 재클릭이면 닫기). */
function toggleStageDetail(key) {
  const detail = document.getElementById("stageDetail");
  if (openStage === key) {
    detail.hidden = true;
    detail.replaceChildren();
    markOpen(null);
    openStage = null;
    return;
  }
  if (!lastRun || lastRun.ok === false) return;
  detail.replaceChildren(renderStageDetail(key, lastRun));
  detail.hidden = false;
  markOpen(key);
  openStage = key;
}

/** 열린 스테이지 카드에 is-open 표시(단일 선택). */
function markOpen(key) {
  document.querySelectorAll("#pipeline .stage").forEach((li) => {
    const on = li.dataset.stage === key;
    li.classList.toggle("is-open", on);
    li.setAttribute("aria-expanded", on ? "true" : "false");
  });
}

/** 스테이지 상세 노드(아비터=후보 점수표, 나머지=사실 목록). */
function renderStageDetail(key, data) {
  const wrap = el("div", "detail-inner");
  const stage = STAGES.find((s) => s.key === key);
  const head = el("div", "detail-head");
  head.append(el("span", "detail-title", (stage ? stage.name : key) + " 상세"));
  const close = el("button", "detail-close", "닫기");
  close.type = "button";
  close.addEventListener("click", () => toggleStageDetail(key));
  head.append(close);
  wrap.append(head);
  wrap.append(key === "arbiter" ? candidateTable(data) : detailFacts(key, data));
  return wrap;
}

/** 아비터 상세: 후보별 점수 분해표(기대효과·선호·에너지·방해·안전·최종). */
function candidateTable(data) {
  const cands = data.candidates || [];
  if (!cands.length) return el("p", "detail-empty", "후보가 없습니다 (침묵).");
  const table = el("table", "cand-table");
  const thead = el("thead");
  const htr = el("tr");
  ["후보", "소스", "기대효과", "선호", "에너지", "방해", "안전", "최종"].forEach((h) => {
    htr.append(el("th", null, h));
  });
  thead.append(htr);
  table.append(thead);
  const tbody = el("tbody");
  for (const c of cands) {
    const tr = el("tr");
    if (c.is_selected) tr.classList.add("is-selected");
    tr.append(el("td", "cand-name", c.is_noop ? "무행동" : c.capability + "." + c.action));
    tr.append(el("td", "cand-origin", c.origin));
    tr.append(numCell(c.expected_effect));
    tr.append(numCell(c.user_preference_fit));
    tr.append(numCell(c.energy_cost));
    tr.append(numCell(c.disturbance));
    tr.append(numCell(c.safety_risk));
    const fin = numCell(c.final_score);
    fin.classList.add("cand-final");
    tr.append(fin);
    tbody.append(tr);
  }
  table.append(tbody);
  const scroll = el("div", "cand-scroll");
  scroll.append(table);
  return scroll;
}

function numCell(v) {
  return el("td", "num tabnum", Number(v).toFixed(2));
}

/** 아비터 외 스테이지 상세: 사실 목록(dt/dd). */
function detailFacts(key, data) {
  const rows = factRows(key, data);
  const dl = el("dl", "detail-facts");
  for (const [k, v] of rows) {
    dl.append(el("dt", null, k));
    dl.append(el("dd", null, v == null || v === "" ? "—" : String(v)));
  }
  return dl;
}

/** 스테이지별 상세 사실 [라벨, 값] 목록. */
function factRows(key, data) {
  if (key === "trigger") {
    const t = data.trigger || {};
    return [["종류", t.kind], ["소스", t.source], ["근원", t.origin]];
  }
  if (key === "collect") {
    const s = data.snapshot || {};
    const e = data.embedding || {};
    return [
      ["맥락 요약", s.nl_summary],
      ["스냅샷 해시", s.snapshot_hash],
      ["선호 셀", e.cell],
      ["선호값", e.preference != null ? e.preference : null],
      ["비고", e.note],
    ];
  }
  if (key === "lines") {
    const l = data.lines || {};
    return [
      ["라인 A (규칙/ML)", (l.line_a || []).map((x) => x.label).join(", ") || "—"],
      ["라인 B (LLM)", (l.line_b || []).map((x) => x.label).join(", ") || "—"],
    ];
  }
  if (key === "gate1") {
    const g = data.gate1 || {};
    return [
      ["판정", g.verdict],
      ["차단", g.blocked ? "예" : "아니오"],
      ["안전 임계", g.safety_critical ? "예" : "아니오"],
      ["하한 등급", g.floor_tier ? g.floor_tier.ko : null],
      ["매칭 규칙", (g.matched_rules || []).join(", ") || "없음"],
    ];
  }
  if (key === "gate2") {
    const g = data.gate2 || {};
    const j = data.judgment || {};
    return [
      ["판정", GATE2_LABELS[g.verdict] || g.verdict],
      ["모델", g.model_id],
      ["신뢰도", g.confidence],
      ["성능 저하", g.degraded ? "예" : "아니오"],
      ["사유", j.rationale],
    ];
  }
  if (key === "l4") {
    const iv = data.intervention || {};
    return [
      ["조건", iv.condition],
      ["확인 요구", CONFIRM_LABELS[iv.required_confirmation] || iv.required_confirmation],
      ["실행 결정", EXEC_LABELS[iv.execution_decision] || iv.execution_decision],
      ["실행 활성", iv.execution_enabled ? "예" : "아니오"],
    ];
  }
  if (key === "notify") {
    if (data.silence) return [["결과", "침묵"], ["사유", data.silence_reason]];
    const p = data.proposal || {};
    return [
      ["기기", nameFor(p.entity_id)],
      ["행동", p.capability + "." + p.action],
      ["등급", p.tier ? p.tier.ko : null],
      ["카테고리", p.category],
      ["확인 요구", CONFIRM_LABELS[p.required_confirmation] || p.required_confirmation],
      ["사유", p.rationale],
      ["만료", p.expires_at],
    ];
  }
  return [];
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
 * 능동 점검 1틱 실행: 보드 값 → POST /api/simulate → 파이프라인 애니메이션.
 * 실행 중엔 버튼을 잠그고, 스테이지가 좌→우로 점등되며 데이터가 흐른다.
 * @returns {Promise<void>}
 */
async function runTick() {
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  setStatus("running", "실행 중…");
  document.getElementById("runHint").textContent = "파이프라인 실행 중…";
  resetPipeline();
  renderPhoneRunning();

  let data;
  try {
    const res = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readBody()),
    });
    data = await res.json();
  } catch (err) {
    setStatus("error", "요청 실패");
    document.getElementById("runHint").textContent = String(err);
    renderPhoneError(String(err));
    btn.disabled = false;
    return;
  }

  lastRun = data;
  document.getElementById("rawJson").textContent = JSON.stringify(data, null, 2);
  document.getElementById("rawPanel").hidden = false;

  if (data.ok === false) {
    setStatus("error", "오류 · " + (data.error || "실패"));
    document.getElementById("runHint").textContent = data.error || "실행 실패";
    renderPhoneError(data.error || "실행 실패");
    btn.disabled = false;
    return;
  }

  await animateRun(data);
  finalizeRun(data);
  btn.disabled = false;
}

/**
 * 애니메이션 종료 시점의 마무리: 상태 배지·힌트·폰 알림(제안 또는 침묵).
 * @param {object} data /api/simulate 응답
 * @returns {void}
 */
function finalizeRun(data) {
  if (data.silence) {
    setStatus("silent", "완료 · 침묵");
    document.getElementById("runHint").textContent =
      data.silence_reason || "행동 없음(침묵)";
    renderPhoneSilence(data);
    return;
  }
  const p = data.proposal;
  setStatus("proposal", "완료 · 제안");
  document.getElementById("runHint").textContent = p
    ? "제안: " + p.capability + "." + p.action + " (" + (p.tier ? p.tier.ko : "") + ")"
    : "제안 생성됨";
  renderPhoneProposal(data);
}

// ── 폰 알림 + 피드백 루프 ─────────────────────────────────────────────
/** 대기 상태(실행 전). */
function renderPhoneIdle() {
  document
    .getElementById("phoneScreen")
    .replaceChildren(el("p", "phone-idle", "능동 점검을 실행하면 제안 알림이 여기에 뜹니다"));
}

/** 실행 중(판단 진행). */
function renderPhoneRunning() {
  document
    .getElementById("phoneScreen")
    .replaceChildren(el("p", "phone-idle", "판단 중…"));
}

/** 오류 상태. */
function renderPhoneError(msg) {
  const w = el("div", "phone-note");
  w.append(el("p", "phone-note-title", "오류"));
  w.append(el("p", "phone-note-body", abbrev(msg, 90)));
  document.getElementById("phoneScreen").replaceChildren(w);
}

/** 침묵 결과 — 안전 fail-closed 를 일급 상태로 명시. */
function renderPhoneSilence(data) {
  const w = el("div", "phone-silence");
  w.append(el("span", "phone-silence-badge", "침묵"));
  w.append(el("p", "phone-silence-title", "보낼 알림이 없습니다"));
  w.append(el("p", "phone-silence-reason", abbrev(data.silence_reason, 80)));
  document.getElementById("phoneScreen").replaceChildren(w);
}

/**
 * 제안 알림 카드 + 승인/거절/무시 버튼. 제목은 기기명+행동+등급으로 일반 생성
 * (기기별 하드코딩 문구 없음). 버튼 → POST /api/feedback → 선호값 갱신 표시.
 * @param {object} data /api/simulate 응답(proposal + embedding 포함)
 * @returns {void}
 */
function renderPhoneProposal(data) {
  const p = data.proposal;
  const emb = data.embedding;
  const cell = emb ? emb.cell : null;

  const card = el("div", "notif");
  const head = el("div", "notif-head");
  head.append(el("span", "notif-app", "LIOT 제안"));
  head.append(el("span", "notif-time", "지금"));
  card.append(head);

  card.append(el("p", "notif-title", nameFor(p.entity_id)));
  const sub = el("p", "notif-sub");
  sub.append(el("span", "notif-act", p.capability + "." + p.action));
  const chip = traceTier(p.tier);
  if (chip) sub.append(chip);
  card.append(sub);
  if (p.rationale) card.append(el("p", "notif-reason", abbrev(p.rationale, 88)));

  const actions = el("div", "notif-actions");
  const buttons = [];
  [
    ["accepted", "승인"],
    ["dismissed", "거절"],
    ["ignored", "무시"],
  ].forEach(([signal, label]) => {
    const b = el("button", "notif-btn", label);
    b.type = "button";
    b.dataset.signal = signal;
    if (!cell) b.disabled = true;
    b.addEventListener("click", () => sendFeedback(signal, cell, card, buttons));
    buttons.push(b);
    actions.append(b);
  });
  card.append(actions);

  const result = el("p", "notif-result");
  result.hidden = true;
  card.append(result);
  if (!cell) {
    result.textContent = "선호 셀 정보 없음 — 피드백 비활성";
    result.hidden = false;
  }
  document.getElementById("phoneScreen").replaceChildren(card);
}

/** 피드백 기록(최근이 위). @type {{cell:string, signal:string, before:number, after:number}[]} */
const feedbackLog = [];

/**
 * 알림 응답을 서버에 보내 선호 셀을 갱신하고 "선호 0.50 → 0.65" 를 표시한다.
 * @returns {Promise<void>}
 */
async function sendFeedback(signal, cell, card, buttons) {
  if (!cell) return;
  buttons.forEach((b) => (b.disabled = true));
  const result = card.querySelector(".notif-result");
  const { status, data } = await postJson("/api/feedback", { cell, signal });
  if (status === 200 && data.ok !== false) {
    result.hidden = false;
    result.textContent =
      SIGNAL_LABELS[signal] +
      " · 선호 " +
      data.before.toFixed(2) +
      " → " +
      data.after.toFixed(2);
    result.dataset.dir =
      data.after > data.before ? "up" : data.after < data.before ? "down" : "flat";
    buttons.forEach((b) => b.classList.toggle("is-chosen", b.dataset.signal === signal));
    appendFeedbackHistory(cell, signal, data);
  } else {
    result.hidden = false;
    result.textContent = (data && data.error) || "피드백 실패";
    buttons.forEach((b) => (b.disabled = false));
  }
}

/** 폰 아래 피드백 기록 라인(최근 5건)을 갱신한다(다음 실행이 이 값을 반영). */
function appendFeedbackHistory(cell, signal, data) {
  feedbackLog.unshift({ cell, signal, before: data.before, after: data.after });
  const host = ensurePhoneLog();
  const rows = [el("span", "phone-log-title", "피드백 기록")];
  feedbackLog.slice(0, 5).forEach((f) => {
    const row = el("div", "phone-log-row");
    row.append(el("span", "phone-log-sig", SIGNAL_LABELS[f.signal] || f.signal));
    row.append(el("span", "phone-log-cell", shortCell(f.cell)));
    const d = el("span", "phone-log-delta tabnum", f.before.toFixed(2) + "→" + f.after.toFixed(2));
    d.dataset.dir = f.after > f.before ? "up" : f.after < f.before ? "down" : "flat";
    row.append(d);
    rows.push(row);
  });
  host.replaceChildren(...rows);
  host.hidden = false;
}

/** 폰 프레임 바로 아래 피드백 기록 컨테이너(없으면 생성). */
function ensurePhoneLog() {
  let host = document.getElementById("phoneLog");
  if (!host) {
    host = el("div", "phone-log");
    host.id = "phoneLog";
    host.hidden = true;
    document.querySelector(".phone").insertAdjacentElement("afterend", host);
  }
  return host;
}

/** "evening|vacant|light:turn_off" → "light:turn_off". */
function shortCell(cell) {
  const parts = String(cell).split("|");
  return parts[parts.length - 1] || cell;
}

// ── 지시 채팅 ─────────────────────────────────────────────────────────
/** capability → 한글 라벨(지시 칩). 미상은 원문. */
const CAP_LABELS = { light: "조명", climate: "냉난방", cover: "커튼", lock: "잠금" };
function capabilityLabel(cap) {
  return CAP_LABELS[cap] || cap;
}

/** 최근 채팅 메시지(파싱 결과 안내). @type {{text:string, kind:string}|null} */
let chatMsg = null;
/** 활성 지시 목록(서버 응답 active). @type {object[]} */
let chatActive = [];

/** 채팅 본문(메시지 + 활성 지시 칩)을 다시 그린다. */
function paintChat() {
  const body = document.getElementById("chatBody");
  body.replaceChildren();
  if (chatMsg) {
    const m = el("p", "chat-msg", chatMsg.text);
    m.dataset.kind = chatMsg.kind;
    body.append(m);
  }
  if (chatActive.length) {
    const list = el("div", "chat-directives");
    for (const d of chatActive) {
      const chip = el("div", "dir-chip");
      chip.dataset.constraint = d.constraint;
      chip.append(el("span", "dir-constraint", CONSTRAINT_LABELS[d.constraint] || d.constraint));
      chip.append(el("span", "dir-cap", d.capability ? capabilityLabel(d.capability) : "전체"));
      if (d.raw_text) chip.title = d.raw_text;
      list.append(chip);
    }
    body.append(list);
  } else if (!chatMsg) {
    body.append(
      el("p", "chat-placeholder", "자연어로 집에 지시를 내립니다 · 예: “밤에는 거실 불 끄지 마”"),
    );
  }
  document.getElementById("chatClearBtn").hidden = chatActive.length === 0;
}

/** 지시 전송(Enter): POST /api/directive → 파싱 결과·활성 지시 갱신. */
async function sendDirective() {
  const input = document.getElementById("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  const { status, data } = await postJson("/api/directive", { text });
  if (status !== 200 || data.ok === false) {
    chatMsg = { text: "지시 전송에 실패했어요", kind: "error" };
    paintChat();
    return;
  }
  chatActive = data.active || [];
  if (!data.parsed || !data.parsed.length) {
    chatMsg = {
      text: "“" + text + "” — 해석하지 못했어요 (예: “밤에는 거실 불 끄지 마”)",
      kind: "warn",
    };
  } else {
    chatMsg = { text: "“" + text + "” 반영됨 · 다음 실행부터 적용", kind: "ok" };
  }
  paintChat();
}

/** 지시 전체 초기화: POST /api/directive {reset:true}. */
async function clearDirectives() {
  const { status, data } = await postJson("/api/directive", { reset: true });
  if (status === 200 && data.ok !== false) {
    chatActive = [];
    chatMsg = { text: "지시를 모두 초기화했어요", kind: "ok" };
    paintChat();
  }
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

    // 지시 채팅: Enter 전송(전용 전송 버튼 없음) + 초기화.
    document.getElementById("chatForm").addEventListener("submit", (e) => {
      e.preventDefault();
      sendDirective();
    });
    document
      .getElementById("chatClearBtn")
      .addEventListener("click", clearDirectives);
    renderPhoneIdle();
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
