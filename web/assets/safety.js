"use strict";

// 우리 집 · 보호자 설정 화면.
// 모든 값은 /api/safety/* 로 이 기기의 DB(데몬과 같은 파일)에만 읽고 쓴다.
// - 거주자 답(75세 이상·혼자 대피·인지 저하)은 이 화면에만 보이고 다른 곳으로 보내지 않는다.
// - 보호자 비밀 주소(알림 토픽)는 등록 직후 한 번만 보여 준다. 변수·저장소에 남기지 않는다.
// - 생년월일·진단명을 묻는 칸은 없다. 질문은 예/아니오뿐이다.

const ENTERED_BY = [
  ["resident", "거주자 본인"],
  ["guardian", "보호자"],
];

// 질문(답 키) — 서버 응답의 answers[key].question 이 화면 문구의 원천이다.
const ANSWER_KEYS = ["age_75_plus", "can_self_evacuate", "cognitive_decline"];
const GUARDIAN_ONLY = new Set(["cognitive_decline"]);

const CONSENT_NOTICE =
  "이 질문의 답은 민감한 정보입니다. 이 기기 안에만 저장되고 밖으로 보내지 않습니다. " +
  "위급한 상황에서 알림을 더 세게 보낼지 정하는 데에만 쓰입니다.";

/** 마지막으로 불러온 거주자 목록. @type {object[]} */
let residents = [];

/**
 * 요소 생성 헬퍼.
 * @param {string} tag 태그명
 * @param {string} [cls] class
 * @param {string} [text] textContent
 * @returns {HTMLElement}
 */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * 헤더 상태 표시.
 * @param {"loading"|"ready"|"error"} state
 * @param {string} text
 */
function setStatus(state, text) {
  document.getElementById("status").dataset.state = state;
  document.getElementById("statusText").textContent = text;
}

/**
 * ISO 시각 → "2026-10-19 14:03"(이 기기 시간대).
 * @param {string|null} iso
 * @returns {string}
 */
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return (
    d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
    p(d.getHours()) + ":" + p(d.getMinutes())
  );
}

/**
 * GET JSON.
 * @param {string} url
 * @returns {Promise<{status:number, data:any}>}
 */
async function getJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  let data;
  try {
    data = await res.json();
  } catch (_) {
    data = { ok: false, error: "응답을 읽지 못했습니다" };
  }
  return { status: res.status, data };
}

/**
 * POST JSON.
 * @param {string} url
 * @param {object} payload
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
    data = { ok: false, error: "응답을 읽지 못했습니다" };
  }
  return { status: res.status, data };
}

// ── 모달 ──────────────────────────────────────────────────────────────
function closeModal() {
  document.getElementById("modalRoot").replaceChildren();
  document.removeEventListener("keydown", onModalKey);
}

function onModalKey(e) {
  if (e.key === "Escape") closeModal();
}

/**
 * 모달을 띄운다.
 * @param {string} title
 * @param {HTMLElement} body form 또는 div
 * @param {{noOverlayClose?: boolean}} [opts]
 */
function mountModal(title, body, opts) {
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
  modal.append(head, body);
  overlay.append(modal);
  if (!(opts && opts.noOverlayClose)) {
    overlay.addEventListener("mousedown", (e) => {
      if (e.target === overlay) closeModal();
    });
  }
  document.getElementById("modalRoot").replaceChildren(overlay);
  document.addEventListener("keydown", onModalKey);
  const first = body.querySelector("input, select, button.btn-primary");
  if (first) first.focus();
}

/**
 * 폼 하단(에러 + 취소/확인).
 * @param {string} submitText
 * @param {boolean} [danger] 삭제처럼 되돌릴 수 없는 동작이면 true
 */
function formFooter(submitText, danger) {
  const error = el("p", "modal-error");
  error.hidden = true;
  error.setAttribute("role", "alert");
  const actions = el("div", "modal-actions");
  const cancel = el("button", "btn-secondary", "취소");
  cancel.type = "button";
  cancel.addEventListener("click", closeModal);
  const submit = el("button", danger ? "btn-primary btn-danger" : "btn-primary", submitText);
  submit.type = "submit";
  actions.append(cancel, submit);
  return { error, actions, submit };
}

function showError(errorEl, msg) {
  errorEl.textContent = msg || "처리하지 못했습니다";
  errorEl.hidden = false;
}

/**
 * 이름 하나만 받는 모달(사람 추가·보호자 추가).
 * @param {string} title
 * @param {string} label
 * @param {string} placeholder
 * @param {(alias:string) => Promise<{status:number, data:any}>} send
 * @param {(data:any) => void} onDone
 */
function openAliasModal(title, label, placeholder, send, onDone) {
  const form = el("form", "modal-form");
  const input = el("input");
  input.type = "text";
  input.maxLength = 40;
  input.placeholder = placeholder;
  const f = el("label", "field");
  f.append(el("span", "field-label", label), input);
  form.append(f);
  const { error, actions } = formFooter("추가");
  form.append(error, actions);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { data } = await send(input.value);
    if (!data.ok) return showError(error, data.error);
    onDone(data);
  });
  mountModal(title, form);
}

// ── 우리 집 ───────────────────────────────────────────────────────────
async function loadHome() {
  const { data } = await getJson("/api/safety/home");
  if (!data.ok) throw new Error(data.error);
  document.getElementById("homeAlias").value = data.home.home_alias;
  document.getElementById("livesAlone").checked = !!data.home.lives_alone;
}

async function saveHome(e) {
  e.preventDefault();
  const error = document.getElementById("homeError");
  error.hidden = true;
  const { data } = await postJson("/api/safety/home", {
    home_alias: document.getElementById("homeAlias").value,
    lives_alone: document.getElementById("livesAlone").checked,
  });
  if (!data.ok) return showError(error, data.error);
  setStatus("ready", "저장했습니다");
}

// ── 거주자 ────────────────────────────────────────────────────────────
/**
 * 답 → "예"/"아니요"/"아직 안 정함".
 * @param {boolean|null} v
 */
function answerText(v) {
  if (v === true) return "예";
  if (v === false) return "아니요";
  return "아직 안 정함";
}

function renderResidents() {
  const list = document.getElementById("residentList");
  list.replaceChildren();
  if (!residents.length) {
    list.append(el("li", "rule-empty", "아직 등록한 사람이 없습니다"));
  }
  for (const r of residents) {
    const li = el("li", "sf-item");
    const top = el("div", "sf-item-top");
    top.append(el("span", "card-name", r.alias));
    const btns = el("div", "sf-btns");
    const edit = el("button", "btn-secondary", "답 입력·수정");
    edit.type = "button";
    edit.addEventListener("click", () => openTraitModal(r));
    const del = el("button", "btn-secondary btn-danger-ghost", "삭제");
    del.type = "button";
    del.addEventListener("click", () => openDeleteModal(r));
    btns.append(edit, del);
    top.append(btns);
    li.append(top);
    const dl = el("dl", "detail-facts");
    for (const key of ANSWER_KEYS) {
      const a = r.answers[key];
      dl.append(el("dt", null, a.question));
      const by = a.entered_by_ko ? " (" + a.entered_by_ko + " 입력)" : "";
      dl.append(el("dd", null, answerText(a.answer) + by));
    }
    li.append(dl);
    list.append(li);
  }
  renderActingSelect();
}

async function loadResidents() {
  const { data } = await getJson("/api/safety/residents");
  if (!data.ok) throw new Error(data.error);
  residents = data.residents;
  renderResidents();
}

/**
 * 예/아니요/그대로 라디오 묶음.
 * @param {string} key 답 키
 * @param {boolean|null} current 현재 답
 */
function yesNoGroup(key, current) {
  const group = el("div", "sf-radios");
  group.setAttribute("role", "radiogroup");
  const opts = [
    ["yes", "예"],
    ["no", "아니요"],
    ["keep", "그대로 두기"],
  ];
  for (const [v, label] of opts) {
    const lab = el("label", "sf-radio");
    const input = el("input");
    input.type = "radio";
    input.name = key;
    input.value = v;
    if ((v === "yes" && current === true) || (v === "no" && current === false)) input.checked = true;
    if (v === "keep" && current == null) input.checked = true;
    lab.append(input, el("span", null, label));
    group.append(lab);
  }
  return group;
}

/**
 * 답 입력 모달 — 안내문을 먼저 보여 주고, 읽었다고 확인해야 저장된다.
 * @param {object} r 거주자 뷰
 */
function openTraitModal(r) {
  const form = el("form", "modal-form");

  const notice = el("div", "sf-notice");
  notice.append(el("p", "sf-notice-title", "입력 전에 꼭 읽어 주세요"));
  notice.append(el("p", null, CONSENT_NOTICE));
  form.append(notice);

  const agree = el("label", "safety-field");
  const agreeBox = el("input", "safety-check");
  agreeBox.type = "checkbox";
  agree.append(agreeBox, el("span", "safety-label", "안내를 읽었고, 이 기기에 저장하는 데 동의합니다"));
  form.append(agree);

  const who = el("select", "field-select");
  for (const [v, label] of ENTERED_BY) {
    const o = el("option", null, label);
    o.value = v;
    who.append(o);
  }
  const whoField = el("label", "field");
  whoField.append(el("span", "field-label", "지금 입력하는 사람"), who);
  form.append(whoField);

  const groups = {};
  for (const key of ANSWER_KEYS) {
    const a = r.answers[key];
    const wrap = el("div", "field");
    wrap.append(el("span", "field-label", a.question));
    const g = yesNoGroup(key, a.answer);
    wrap.append(g);
    if (GUARDIAN_ONLY.has(key)) {
      wrap.append(el("span", "field-hint", "보호자만 입력할 수 있습니다. 위에서 「보호자」를 고르세요."));
    }
    groups[key] = { wrap, g };
    form.append(wrap);
  }

  /** 거주자 본인이 입력할 때는 보호자 전용 질문을 잠근다. */
  function syncGuardianOnly() {
    const isGuardian = who.value === "guardian";
    for (const key of GUARDIAN_ONLY) {
      groups[key].wrap.classList.toggle("sf-disabled", !isGuardian);
      groups[key].g.querySelectorAll("input").forEach((i) => (i.disabled = !isGuardian));
    }
  }
  who.addEventListener("change", syncGuardianOnly);
  syncGuardianOnly();

  const { error, actions } = formFooter("저장");
  form.append(error, actions);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!agreeBox.checked) return showError(error, "안내를 읽고 동의해야 저장할 수 있습니다.");
    const answers = {};
    for (const key of ANSWER_KEYS) {
      if (GUARDIAN_ONLY.has(key) && who.value !== "guardian") continue;
      const picked = groups[key].g.querySelector("input:checked");
      if (!picked || picked.value === "keep") continue;
      answers[key] = picked.value === "yes";
    }
    const { data } = await postJson("/api/safety/residents/traits", {
      resident_id: r.resident_id,
      entered_by: who.value,
      answers,
    });
    if (!data.ok) return showError(error, data.error);
    closeModal();
    await loadResidents();
    setStatus("ready", "저장했습니다");
  });
  mountModal(r.alias + " — 답 입력", form);
}

/** 삭제 확인 모달. 확인하면 이 사람의 답이 이 기기에서 지워진다. */
function openDeleteModal(r) {
  const form = el("form", "modal-form");
  form.append(
    el(
      "p",
      "sf-help",
      "「" + r.alias + "」을(를) 삭제할까요? 이 사람의 답이 이 기기에서 완전히 지워지며 되돌릴 수 없습니다.",
    ),
  );
  const { error, actions } = formFooter("삭제", true);
  form.append(error, actions);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { data } = await postJson("/api/safety/residents/delete", {
      resident_id: r.resident_id,
      confirm: true,
    });
    if (!data.ok) return showError(error, data.error);
    closeModal();
    await refreshAll();
    setStatus("ready", "삭제했습니다");
  });
  mountModal("사람 삭제", form);
}

// ── 보호자 · 동의 ─────────────────────────────────────────────────────
function renderActingSelect() {
  const sel = document.getElementById("actingResident");
  const prev = sel.value;
  sel.replaceChildren();
  if (!residents.length) {
    const o = el("option", null, "먼저 함께 사는 사람을 추가하세요");
    o.value = "";
    sel.append(o);
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  for (const r of residents) {
    const o = el("option", null, r.alias);
    o.value = r.resident_id;
    sel.append(o);
  }
  if (residents.some((r) => r.resident_id === prev)) sel.value = prev;
}

function renderGuardians(guardians) {
  const list = document.getElementById("guardianList");
  list.replaceChildren();
  if (!guardians.length) list.append(el("li", "rule-empty", "아직 등록한 보호자가 없습니다"));
  for (const g of guardians) {
    const li = el("li", "sf-item");
    const top = el("div", "sf-item-top");
    const name = el("span", "card-name", g.alias);
    top.append(name);
    const tags = el("span", "sf-tags");
    const act = el("span", "verdict-tag", g.active ? "사용 중" : "사용 중지");
    act.dataset.kind = g.active ? "allow" : "warn";
    const con = el("span", "verdict-tag", g.consented ? "전달 동의함" : "전달 안 함");
    con.dataset.kind = g.consented ? "allow" : "warn";
    tags.append(act, con);
    top.append(tags);
    li.append(top);

    const btns = el("div", "sf-btns");
    if (g.active) {
      const toggle = el("button", "btn-secondary", g.consented ? "동의 철회" : "전달 동의");
      toggle.type = "button";
      toggle.addEventListener("click", () => changeConsent(g, g.consented ? "revoke" : "grant"));
      const off = el("button", "btn-secondary btn-danger-ghost", "사용 중지");
      off.type = "button";
      off.addEventListener("click", () => openDeactivateModal(g));
      btns.append(toggle, off);
    } else if (g.consented) {
      const revoke = el("button", "btn-secondary", "동의 철회");
      revoke.type = "button";
      revoke.addEventListener("click", () => changeConsent(g, "revoke"));
      btns.append(revoke);
    }
    li.append(btns);
    const err = el("p", "modal-error");
    err.hidden = true;
    err.setAttribute("role", "alert");
    li.append(err);
    g._err = err;
    list.append(li);
  }
}

async function loadGuardians() {
  const { data } = await getJson("/api/safety/guardians");
  if (!data.ok) throw new Error(data.error);
  renderGuardians(data.guardians);
}

async function changeConsent(g, action) {
  const by = document.getElementById("actingResident").value;
  if (!by) return showError(g._err, "먼저 함께 사는 사람을 추가하고, 누가 정하는지 고르세요.");
  const { data } = await postJson("/api/safety/consent", {
    guardian_id: g.guardian_id,
    by_resident_id: by,
    action,
  });
  if (!data.ok) return showError(g._err, data.error);
  renderGuardians(data.guardians);
  await loadConsentHistory();
  setStatus("ready", action === "grant" ? "전달 동의를 저장했습니다" : "동의를 철회했습니다");
}

function openDeactivateModal(g) {
  const form = el("form", "modal-form");
  form.append(
    el("p", "sf-help", "「" + g.alias + "」 보호자를 사용 중지할까요? 더 이상 알림이 전달되지 않습니다."),
  );
  const { error, actions } = formFooter("사용 중지", true);
  form.append(error, actions);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { data } = await postJson("/api/safety/guardians/deactivate", { guardian_id: g.guardian_id });
    if (!data.ok) return showError(error, data.error);
    closeModal();
    renderGuardians(data.guardians);
    setStatus("ready", "보호자를 사용 중지했습니다");
  });
  mountModal("보호자 사용 중지", form);
}

/**
 * 등록 직후 비밀 주소(알림 토픽)를 딱 한 번 보여 준다. 닫으면 다시 볼 수 없다.
 * topic 은 이 함수의 지역 변수와 화면 요소에만 있고, 닫을 때 요소째 사라진다.
 * @param {string} alias
 * @param {string} topic
 */
function showTopicOnce(alias, topic) {
  const box = el("div", "modal-form");
  box.append(
    el(
      "p",
      "sf-help",
      "「" + alias + "」 보호자의 휴대폰 알림 앱(ntfy)에서 아래 주소를 구독하세요. " +
        "이 주소는 지금 한 번만 보여 드립니다. 창을 닫으면 다시 볼 수 없습니다.",
    ),
  );
  const code = el("code", "sf-topic", topic);
  box.append(code);
  const copied = el("p", "field-hint", "");
  const actions = el("div", "modal-actions");
  const copy = el("button", "btn-secondary", "복사");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(code.textContent || "");
      copied.textContent = "복사했습니다. 보호자 휴대폰에 붙여 넣으세요.";
    } catch (_) {
      copied.textContent = "복사하지 못했습니다. 직접 옮겨 적어 주세요.";
    }
  });
  const done = el("button", "btn-primary", "휴대폰에 설정했습니다");
  done.type = "button";
  done.addEventListener("click", closeModal);
  actions.append(copy, done);
  box.append(copied, actions);
  mountModal("보호자 알림 주소 (한 번만 표시)", box, { noOverlayClose: true });
}

// ── 이력 ──────────────────────────────────────────────────────────────
/**
 * 표 본문을 채운다(빈 목록이면 안내 한 줄).
 * @param {string} tableId
 * @param {string[][]} rows
 * @param {string} emptyText
 */
function fillTable(tableId, rows, emptyText) {
  const tbody = document.querySelector("#" + tableId + " tbody");
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "sf-empty", emptyText);
    td.colSpan = 4;
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const cells of rows) {
    const tr = el("tr");
    for (const c of cells) tr.append(el("td", null, c));
    tbody.append(tr);
  }
}

async function loadConsentHistory() {
  const { data } = await getJson("/api/safety/consent-history");
  if (!data.ok) throw new Error(data.error);
  fillTable(
    "consentTable",
    data.history.map((h) => [
      fmtTime(h.at),
      h.event_ko,
      h.guardian_alias || "—",
      h.by_resident_alias || "삭제된 사람",
    ]),
    "아직 동의 기록이 없습니다",
  );
}

async function loadForwards() {
  const { data } = await getJson("/api/safety/forwards");
  if (!data.ok) throw new Error(data.error);
  fillTable(
    "forwardTable",
    data.forwards.map((f) => [
      fmtTime(f.at),
      f.situation_ko,
      f.kind_ko,
      f.guardian_id ? f.guardian_alias || "—" : "거주자 본인",
    ]),
    "아직 전달된 상황이 없습니다",
  );
}

async function refreshAll() {
  await loadHome();
  await loadResidents();
  await loadGuardians();
  await loadConsentHistory();
  await loadForwards();
}

async function init() {
  document.getElementById("homeForm").addEventListener("submit", saveHome);
  document.getElementById("addResidentBtn").addEventListener("click", () =>
    openAliasModal(
      "함께 사는 사람 추가",
      "부르는 이름",
      "예: 할머니",
      (alias) => postJson("/api/safety/residents", { alias }),
      async () => {
        closeModal();
        await loadResidents();
        setStatus("ready", "추가했습니다");
      },
    ),
  );
  document.getElementById("addGuardianBtn").addEventListener("click", () =>
    openAliasModal(
      "보호자 추가",
      "보호자 이름",
      "예: 큰딸",
      (alias) => postJson("/api/safety/guardians", { alias }),
      async (data) => {
        // 비밀 주소는 이 응답에만 있다 — 곧바로 한 번 보여 주고 어디에도 남기지 않는다.
        showTopicOnce(data.guardian.alias, data.topic);
        await loadGuardians();
      },
    ),
  );
  try {
    await refreshAll();
    setStatus("ready", "준비됨");
  } catch (err) {
    setStatus("error", "불러오지 못했습니다");
    document.getElementById("residentList").replaceChildren(
      el("li", "modal-error", String(err && err.message ? err.message : err)),
    );
  }
}

document.addEventListener("DOMContentLoaded", init);
