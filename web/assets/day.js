/* LIOT 하루 시뮬레이션 화면.
 *
 * 서버(/api/day/*)가 실제 LiotService 로 며칠을 돌리고 사건을 SSE 로 흘린다. 이 화면은 받은
 * 사건과 runs/<id> 를 그대로 그린다 — 숫자·문구·상태는 하나도 계산하지 않는다(web/AGENTS.md).
 * DOM 은 el() + textContent 로만 만든다(서버 값을 HTML 문자열로 넣지 않는다).
 */
"use strict";

(function () {
  /** 서버가 내보내는 사건 단계 이름(SSE event 필드). */
  const STAGES = [
    "day.start",
    "step",
    "proposal",
    "digest",
    "feedback",
    "tray.review",
    "safety",
    "day.end",
    "judge",
    "memory",
    "models.status",
    "models.fallback",
    "error",
    "done",
  ];
  const DAYS = 3;

  /**
   * 화면 상태. 사건을 받는 대로 쌓고, 날짜별 보기는 매번 이 상태로 다시 그린다.
   * @type {{
   *   runId: string|null, lastSeq: number, live: boolean, done: boolean,
   *   es: EventSource|null, retry: number|null, pace: number,
   *   day: number, followDay: boolean, selected: string|null,
   *   proposals: Map<string, Object>, alerts: Map<string, Object>,
   *   timeline: Object[], metrics: Map<number, Object>,
   *   clock: string, weather: string, queued: Set<string>, ignored: Set<string>,
   *   models: "fake"|"cloud", cloudRun: boolean, memory: Object[],
   *   dayUsage: Map<number, Object>, fallback: string
   * }}
   */
  const S = {
    runId: null,
    lastSeq: 0,
    live: false,
    done: false,
    es: null,
    retry: null,
    pace: 0,
    day: 1,
    followDay: true,
    selected: null,
    proposals: new Map(),
    alerts: new Map(),
    timeline: [],
    metrics: new Map(),
    clock: "",
    weather: "",
    queued: new Set(),
    ignored: new Set(),
    models: "fake",
    cloudRun: false,
    memory: [],
    dayUsage: new Map(),
    fallback: "",
  };

  /**
   * 요소 하나를 만든다. 글자는 textContent 로만 넣는다.
   * @param {string} tag
   * @param {string} [cls]
   * @param {string|number|null} [text]
   * @returns {HTMLElement}
   */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /**
   * id 로 요소를 찾는다.
   * @param {string} id
   * @returns {HTMLElement}
   */
  function $(id) {
    const node = document.getElementById(id);
    if (!node) throw new Error("missing #" + id);
    return node;
  }

  /**
   * 자식을 모두 지운다.
   * @param {HTMLElement} node
   */
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  /**
   * 머리의 상태 표시를 바꾼다.
   * @param {"idle"|"running"|"done"|"error"} state
   * @param {string} text
   */
  function setStatus(state, text) {
    $("status").dataset.state = state;
    $("statusText").textContent = text;
  }

  /**
   * 서버의 한 줄 오류를 보여 준다(없으면 숨긴다).
   * @param {string|null} message
   */
  function showError(message) {
    const box = $("error");
    box.hidden = !message;
    box.textContent = message || "";
  }

  /**
   * JSON 요청. 응답은 늘 {ok, ...} 이고, 실패면 서버의 한국어 error 를 던진다.
   * @param {string} url
   * @param {Object|null} body
   * @returns {Promise<Object>}
   */
  async function api(url, body) {
    const init = { cache: "no-store" };
    if (body !== null) {
      init.method = "POST";
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const resp = await fetch(url, init);
    const data = await resp.json().catch(() => ({ ok: false, error: "응답을 읽지 못했어요." }));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || "요청이 실패했어요.");
    }
    return data;
  }

  // ── 실행·스트림 ─────────────────────────────────────────────────────

  /** 새 실행 전에 화면 상태를 비운다. */
  function reset() {
    S.lastSeq = 0;
    S.done = false;
    S.day = 1;
    S.followDay = true;
    S.selected = null;
    S.proposals.clear();
    S.alerts.clear();
    S.timeline = [];
    S.metrics.clear();
    S.clock = "";
    S.weather = "";
    S.queued.clear();
    S.ignored.clear();
    S.memory = [];
    S.dayUsage.clear();
    S.fallback = "";
  }

  /**
   * [3일 돌리기]: 실행을 시작하고 스트림에 붙는다.
   * @param {Event} ev
   */
  async function onRun(ev) {
    ev.preventDefault();
    showError(null);
    const seed = Number.parseInt(/** @type {HTMLInputElement} */ ($("seed")).value, 10);
    try {
      const out = await api("/api/day/run", {
        days: DAYS,
        seed: Number.isFinite(seed) ? seed : 7,
        persona: "default",
        pace_ms: S.pace,
        models: S.models,
      });
      closeStream();
      reset();
      S.runId = out.run_id;
      S.cloudRun = out.models === "cloud";
      S.live = true;
      setStatus("running", "도는 중");
      setButtons();
      render();
      openStream();
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
      setStatus("error", "시작하지 못했어요");
    }
  }

  /** [멈추기]: 지금 단계를 마치고 멈추게 한다. */
  async function onStop() {
    try {
      await api("/api/day/stop", {});
      setStatus("running", "멈추는 중");
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  }

  /** 스트림을 닫는다(다시 붙기 예약도 취소). */
  function closeStream() {
    if (S.es) S.es.close();
    S.es = null;
    if (S.retry !== null) window.clearTimeout(S.retry);
    S.retry = null;
  }

  /** 마지막으로 받은 사건 다음부터 스트림을 연다(늦게 붙어도·다시 붙어도 빠짐이 없다). */
  function openStream() {
    if (!S.runId) return;
    const url = "/api/day/stream?run=" + encodeURIComponent(S.runId) + "&from=" + (S.lastSeq + 1);
    const es = new EventSource(url);
    S.es = es;
    for (const stage of STAGES) {
      es.addEventListener(stage, (msg) => onMessage(/** @type {MessageEvent} */ (msg)));
    }
    es.onerror = () => {
      if (S.done) return;
      // 서버가 연결을 닫았거나 끊겼다 — 받은 곳 다음부터 다시 붙는다.
      closeStream();
      S.retry = window.setTimeout(openStream, 1000);
    };
  }

  /**
   * SSE 메시지 하나. seq 가 이미 받은 것이면 버린다(중복 없음).
   * @param {MessageEvent} msg
   */
  function onMessage(msg) {
    let ev;
    try {
      ev = JSON.parse(msg.data);
    } catch (_err) {
      return;
    }
    if (typeof ev.seq !== "number" || ev.seq <= S.lastSeq) return;
    S.lastSeq = ev.seq;
    apply(ev);
    render();
  }

  /**
   * 사건 하나를 상태에 반영한다(계산 없음 — 서버 값을 옮겨 담기만 한다).
   * @param {Object} ev
   */
  function apply(ev) {
    if (ev.clock_ko) S.clock = ev.clock_ko;
    if (ev.day && S.followDay) S.day = ev.day;
    if (ev.metrics && ev.metrics.day) S.metrics.set(ev.metrics.day, ev.metrics);
    switch (ev.stage) {
      case "step":
        if (ev.weather_ko) S.weather = ev.weather_ko;
        if (ev.line_ko) pushLine(ev, "quiet");
        break;
      case "proposal":
        S.proposals.set(ev.id, Object.assign({}, ev));
        pushLine(ev, "proposal");
        break;
      case "feedback": {
        const p = S.proposals.get(ev.id);
        if (p && ev.kind === "comfort") {
          p.state = ev.state;
          p.state_ko = ev.state_ko;
          S.queued.delete(ev.id);
        }
        pushLine(ev, ev.source === "timeout" ? "quiet" : "feedback");
        break;
      }
      case "safety":
        applySafety(ev);
        pushLine(ev, "safety");
        break;
      case "tray.review":
      case "day.start":
      case "models.status":
        pushLine(ev, "quiet");
        break;
      case "day.end":
        if (ev.usage_day) S.dayUsage.set(ev.day, ev.usage_day);
        pushLine(ev, "quiet");
        break;
      case "models.fallback":
        S.fallback = ev.line_ko || "";
        pushLine(ev, "safety");
        break;
      case "judge": {
        const p = S.proposals.get(ev.id);
        if (p) p.judge = ev.judge;
        break;
      }
      case "memory":
        S.memory.push(ev);
        break;
      case "error":
        showError(ev.error || "실행 중 오류가 났어요.");
        break;
      case "done":
        finish(ev);
        break;
      default:
        break;
    }
  }

  /**
   * 안전 사건: 첫 경보는 카드로, 뒤따르는 사건은 그 카드의 상태로.
   * @param {Object} ev
   */
  function applySafety(ev) {
    if (ev.kind === "first_alert") {
      S.alerts.set(ev.id, Object.assign({ followups: [] }, ev));
      return;
    }
    const card = ev.id ? S.alerts.get(ev.id) : null;
    if (!card) return;
    card.followups.push(ev);
    if (ev.kind === "forward") card.guardian_forwarded = true;
    if (ev.kind === "normal") card.closed_ko = ev.line_ko;
  }

  /**
   * 타임라인에 한 줄을 더한다(서버의 line_ko 그대로).
   * @param {Object} ev
   * @param {string} kind
   */
  function pushLine(ev, kind) {
    if (!ev.line_ko) return;
    const hhmm = ev.hhmm || (ev.clock_ko ? ev.clock_ko.slice(-5) : "");
    S.timeline.push({ day: ev.day, hhmm: hhmm, line: ev.line_ko, kind: kind, id: ev.id || null });
  }

  /**
   * done: 스트림을 닫고 서버 집계(runs/<id>)로 지표를 맞춘다.
   * @param {Object} ev
   */
  async function finish(ev) {
    S.done = true;
    S.live = false;
    closeStream();
    setStatus("done", ev.stopped ? "멈췄어요" : "끝났어요");
    setButtons();
    try {
      const view = await api("/api/day/runs/" + encodeURIComponent(S.runId || ""), null);
      for (const m of (view.summary && view.summary.metrics) || []) S.metrics.set(m.day, m);
      for (const p of (view.summary && view.summary.proposals) || []) {
        const cur = S.proposals.get(p.id);
        if (cur) Object.assign(cur, p);
      }
      const models = view.summary && view.summary.models;
      if (models) {
        for (const d of models.days || []) S.dayUsage.set(d.day, d);
        if (models.fallback_ko) S.fallback = models.fallback_ko;
      }
      if (view.summary && Array.isArray(view.summary.memory)) S.memory = view.summary.memory;
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
    render();
  }

  // ── 보는 사람 응답 ─────────────────────────────────────────────────

  /**
   * 폰 카드의 버튼. 도움됐어요·그만해는 서버로 보내고, 다음 단계에 실제 피드백 경로로 반영된다.
   * 무시는 아무것도 보내지 않는다(그대로 두면 만료된다).
   * @param {string} id
   * @param {"accepted"|"dismissed"|"ignore"} signal
   */
  async function tap(id, signal) {
    if (signal === "ignore") {
      S.ignored.add(id);
      render();
      return;
    }
    if (!S.runId || !S.live) return;
    S.queued.add(id);
    render();
    try {
      await api("/api/day/feedback", { run: S.runId, proposal_id: id, signal: signal });
    } catch (err) {
      S.queued.delete(id);
      showError(err instanceof Error ? err.message : String(err));
      render();
    }
  }

  // ── 그리기 ─────────────────────────────────────────────────────────

  /** 버튼 사용 가능 상태. */
  function setButtons() {
    /** @type {HTMLButtonElement} */ ($("runBtn")).disabled = S.live;
    /** @type {HTMLButtonElement} */ ($("stopBtn")).disabled = !S.live;
  }

  /** 지금 상태로 화면 전체를 다시 그린다. */
  function render() {
    renderTabs();
    $("clock").textContent = S.clock || "아직 시작하지 않았어요";
    $("weather").textContent = S.weather;
    $("phoneClock").textContent = S.clock ? S.clock.slice(-5) : "";
    renderTimeline();
    renderPhone();
    renderMetrics();
    renderCompare();
    renderInspect();
    renderMemory();
  }

  /** 날짜 탭. 누르면 그날을 고정해서 본다. */
  function renderTabs() {
    const box = $("dayTabs");
    clear(box);
    for (let d = 1; d <= DAYS; d += 1) {
      const btn = el("button", "dy-tab", d + "일차");
      btn.type = "button";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(d === S.day));
      btn.addEventListener("click", () => {
        S.day = d;
        S.followDay = false;
        render();
      });
      box.appendChild(btn);
    }
  }

  /** 그날의 타임라인(최근이 위). */
  function renderTimeline() {
    const box = $("timeline");
    clear(box);
    const rows = S.timeline.filter((r) => r.day === S.day).slice().reverse();
    for (const r of rows) {
      const li = el("li", "dy-tl-item");
      li.dataset.kind = r.kind;
      li.appendChild(el("span", "dy-tl-time tabnum", r.hhmm));
      li.appendChild(el("span", "dy-tl-line", r.line));
      box.appendChild(li);
    }
  }

  /** 거주자 폰: 위에 안전 경보, 그 아래 폰 알림(최근이 위), 맨 아래 모아보기. */
  function renderPhone() {
    const feed = $("feed");
    const tray = $("tray");
    clear(feed);
    clear(tray);
    const alerts = [...S.alerts.values()].filter((a) => a.day === S.day).reverse();
    for (const a of alerts) feed.appendChild(alarmCard(a));
    const all = [...S.proposals.values()].filter((p) => p.day === S.day).reverse();
    for (const p of all.filter((x) => x.destination === "phone")) feed.appendChild(card(p));
    const trayItems = all.filter((x) => x.destination === "tray");
    for (const p of trayItems) tray.appendChild(card(p));
    $("traySection").hidden = trayItems.length === 0;
    $("feedEmpty").hidden = feed.childElementCount > 0 || trayItems.length > 0;
  }

  /**
   * 안전 경보 카드. 제목은 상황 이름, 본문은 실제 송신 문구(서버가 실제 transport 로 펼친 것).
   * @param {Object} a
   * @returns {HTMLElement}
   */
  function alarmCard(a) {
    const node = el("article", "alarm");
    node.dataset.closed = String(Boolean(a.closed_ko));
    const head = el("div", "notif-head");
    head.appendChild(el("span", "notif-app", a.ntfy ? a.ntfy.title : "LIOT"));
    head.appendChild(el("span", "tabnum", a.hhmm));
    node.appendChild(head);
    node.appendChild(el("p", "alarm-title", a.situation_ko + (a.severity_ko ? " · " + a.severity_ko : "")));
    if (a.ntfy) node.appendChild(el("p", "alarm-body", a.ntfy.body));
    const lines = [a.line_ko];
    for (const f of a.followups) if (f.kind !== "normal") lines.push(f.hhmm + " " + f.line_ko);
    if (a.guardian_forwarded) lines.push("보호자에게도 전달됨");
    if (a.closed_ko) lines.push(a.closed_ko);
    for (const line of lines) node.appendChild(el("p", "alarm-meta", line));
    return node;
  }

  /**
   * 제안 카드(폰 알림·모아보기 공통). 누르면 '속 보기'가 그 제안을 보여 준다.
   * @param {Object} p
   * @returns {HTMLElement}
   */
  function card(p) {
    const node = el("article", "notif");
    node.tabIndex = 0;
    node.setAttribute("aria-current", String(S.selected === p.id));
    node.addEventListener("click", () => {
      S.selected = p.id;
      render();
    });
    const head = el("div", "notif-head");
    head.appendChild(el("span", "notif-app", "LIOT"));
    head.appendChild(el("span", "tabnum", p.hhmm));
    node.appendChild(head);
    node.appendChild(el("p", "notif-title", p.title_ko));
    if (p.reason_ko) node.appendChild(el("p", "notif-body", p.reason_ko));
    const writer = writerLine(p);
    if (writer) node.appendChild(el("p", "notif-writer", writer));

    const pending = p.state === "pending";
    let stateText = p.state_ko;
    if (pending && S.queued.has(p.id)) stateText = "다음 단계에 반영돼요";
    else if (pending && S.ignored.has(p.id)) stateText = "무시함 — 그대로 두면 만료돼요";
    const state = el("p", "notif-state", stateText);
    state.dataset.state = p.state;
    node.appendChild(state);

    const actions = el("div", "notif-actions");
    const canTap = pending && S.live && !S.queued.has(p.id) && !S.ignored.has(p.id);
    /** @type {[string, "accepted"|"ignore"|"dismissed"][]} */
    const buttons = [
      ["도움됐어요", "accepted"],
      ["무시", "ignore"],
      ["그만해", "dismissed"],
    ];
    for (const [label, signal] of buttons) {
      const b = el("button", "notif-btn", label);
      b.type = "button";
      b.disabled = !canTap;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        tap(p.id, signal);
      });
      actions.appendChild(b);
    }
    node.appendChild(actions);
    return node;
  }

  /**
   * 이유를 쓴 쪽(cloud 실행만): 모델 id, 또는 '문장 틀 · 대체 사유'(서버의 한국어 그대로).
   * @param {Object} p
   * @returns {string|null}
   */
  function writerLine(p) {
    if (p.reason_source === "model" && p.reason_model) return "작성 " + p.reason_model;
    if (p.reason_source === "template") {
      return "문장 틀" + (p.reason_fallback_ko ? " · " + p.reason_fallback_ko : "");
    }
    return null;
  }

  /** 그날 지표(큰 숫자 넷 + 작은 줄). 값은 서버의 metrics 그대로. */
  function renderMetrics() {
    const box = $("metrics");
    clear(box);
    const m = S.metrics.get(S.day);
    /** @type {[string, string|number][]} */
    const items = [
      ["수락률", m ? m.accept_rate_ko : "—"],
      ["폰 알림", m ? m.pushes : "—"],
      ["모아보기", m ? m.digests : "—"],
      ["그만해", m ? m.dismissed : "—"],
    ];
    for (const [label, value] of items) {
      const cell = el("div", "dy-metric");
      cell.appendChild(el("p", "dy-metric-num tabnum", value));
      cell.appendChild(el("p", "dy-metric-label", label));
      box.appendChild(cell);
    }
    if (m) {
      const foot =
        S.day + "일차 · 모아보기에서 수락 " + m.tray_accepted + " · 만료 " + m.expired +
        " · 안전 경보 " + m.safety_alerts;
      box.appendChild(el("p", "dy-metric-foot tabnum", foot));
    }
    const u = S.dayUsage.get(S.day);
    if (u) {
      const row = el("div", "dy-usage tabnum");
      /** @type {[string, string][]} */
      const cells = [
        ["외부 호출", u.calls_ko],
        ["평균 지연", u.avg_ko],
        ["추정 비용", u.usd_ko],
      ];
      for (const [label, value] of cells) {
        const cell = el("div", "dy-usage-cell");
        cell.appendChild(el("p", "dy-usage-num", value));
        cell.appendChild(el("p", "dy-metric-label", label));
        row.appendChild(cell);
      }
      box.appendChild(row);
    }
    if (S.fallback) box.appendChild(el("p", "dy-fallback", S.fallback));
  }

  /** 날짜별 비교 표. */
  function renderCompare() {
    const body = $("compareBody");
    clear(body);
    for (let d = 1; d <= DAYS; d += 1) {
      const m = S.metrics.get(d);
      const tr = el("tr");
      tr.setAttribute("aria-current", String(d === S.day));
      tr.appendChild(el("td", "", d + "일차"));
      tr.appendChild(el("td", "", m ? m.accept_rate_ko : "—"));
      tr.appendChild(el("td", "", m ? m.pushes : "—"));
      tr.appendChild(el("td", "", m ? m.digests : "—"));
      tr.appendChild(el("td", "", m ? m.dismissed : "—"));
      body.appendChild(tr);
    }
  }

  /**
   * dl 에 이름·값 한 쌍을 더한다.
   * @param {HTMLElement} dl
   * @param {string} name
   * @param {string|Node} value
   */
  function kv(dl, name, value) {
    dl.appendChild(el("dt", "", name));
    const dd = el("dd");
    if (typeof value === "string") dd.textContent = value;
    else dd.appendChild(value);
    dl.appendChild(dd);
  }

  /** '속 보기': 고른 제안이 왜 그렇게 판단됐는지(서버 기록 그대로). */
  function renderInspect() {
    const box = $("inspect");
    clear(box);
    const p = S.selected ? S.proposals.get(S.selected) : null;
    if (!p) {
      box.appendChild(
        el("p", "dy-muted", "폰 알림이나 모아보기 항목을 누르면 왜 그렇게 판단했는지 보여 드려요."),
      );
      return;
    }
    box.appendChild(el("p", "dy-inspect-title", p.title_ko));
    const dl = el("dl", "dy-kv");
    kv(dl, "규칙", p.rule_id || "—");
    kv(dl, "성립한 규칙", (p.matched_rules || []).join(", ") || "—");
    if (p.skipped && p.skipped.length) {
      kv(dl, "건너뛴 규칙", p.skipped.map((s) => s.title_ko + " — " + s.reason_ko).join(" / "));
    }
    kv(dl, "점수", String(p.score) + " (아무것도 안 하기 " + String(p.noop_score) + ")");
    const list = el("ul", "dy-cands tabnum");
    for (const c of p.candidates || []) {
      const li = el("li");
      li.dataset.selected = String(Boolean(c.selected));
      li.appendChild(el("span", "", c.label_ko));
      li.appendChild(el("span", "", c.score));
      list.appendChild(li);
    }
    kv(dl, "후보", list);
    kv(dl, "확인 강도", p.tier_ko);
    kv(dl, "알림 게이트", p.decision_ko);
    kv(dl, "상태", p.state_ko);
    if (p.reason_source) kv(dl, "이유 작성", writerLine(p) || "—");
    if (p.decided_by_ko) {
      kv(dl, "거주자 결정", p.decided_by_ko + (p.reaction_fallback_ko ? " · " + p.reaction_fallback_ko : ""));
    }
    if (p.memory_used && p.memory_used.length) kv(dl, "참고한 기억", p.memory_used.join(" / "));
    box.appendChild(dl);
    if (p.judge) box.appendChild(judgeBars(p.judge));
  }

  /**
   * Jev 와 luna 의 선택지별 확률 막대(질문마다). 퍼센트 글자·일치 여부는 서버 값 그대로.
   * @param {Object} j
   * @returns {HTMLElement}
   */
  function judgeBars(j) {
    const wrap = el("div", "dy-judge");
    const head = "Jev " + (j.served_model || j.model_id) + " · luna " +
      (j.luna_status === "ok" ? "비교함" : (j.luna_reason_ko || "미접근"));
    wrap.appendChild(el("p", "dy-judge-head", head));
    for (const q of j.questions || []) {
      const block = el("div", "dy-judge-q");
      const title = el("p", "dy-judge-title");
      title.appendChild(el("span", "", q.question_ko));
      const badge = el("span", "dy-badge", q.agree_ko);
      badge.dataset.agree = String(q.agree);
      title.appendChild(badge);
      block.appendChild(title);
      for (const o of q.options || []) {
        const row = el("div", "dy-bar-row tabnum");
        row.appendChild(el("span", "dy-bar-label", o.label_ko));
        row.appendChild(bar("jev", o.jev, "Jev " + o.jev_pct_ko));
        row.appendChild(bar("luna", o.luna, "luna " + o.luna_pct_ko));
        block.appendChild(row);
      }
      wrap.appendChild(block);
    }
    return wrap;
  }

  /**
   * 막대 하나(너비 = 서버 확률). 확률이 없으면 빈 막대.
   * @param {"jev"|"luna"} who
   * @param {number|null} prob
   * @param {string} text
   * @returns {HTMLElement}
   */
  function bar(who, prob, text) {
    const cell = el("span", "dy-bar");
    cell.dataset.who = who;
    const fill = el("span", "dy-bar-fill");
    fill.style.width = typeof prob === "number" ? String(Math.max(0, Math.min(1, prob)) * 100) + "%" : "0%";
    cell.appendChild(fill);
    cell.appendChild(el("span", "dy-bar-text", text));
    return cell;
  }

  /** '기억' 패널: memgineering 기억 사건을 받은 순서대로(cloud 실행만). */
  function renderMemory() {
    const block = $("memoryBlock");
    block.hidden = !S.cloudRun;
    const list = $("memory");
    clear(list);
    for (const m of S.memory) {
      const li = el("li", "dy-mem-item");
      li.dataset.ok = String(m.ok !== false);
      li.appendChild(el("span", "dy-mem-time tabnum", m.clock_ko || ""));
      const what = el("span", "dy-mem-what");
      what.appendChild(el("span", "dy-mem-verb", m.who_ko + " · " + m.verb_ko));
      if (m.rule) what.appendChild(el("span", "dy-badge", "규칙"));
      if (m.ok === false) what.appendChild(el("span", "dy-badge", "실패"));
      li.appendChild(what);
      li.appendChild(el("span", "dy-mem-title", m.title_ko));
      list.appendChild(li);
    }
    if (S.cloudRun && S.memory.length === 0) {
      list.appendChild(el("li", "dy-muted", "아직 기억을 찾거나 남기지 않았어요."));
    }
  }

  // ── 시작 ───────────────────────────────────────────────────────────

  /** 속도 세그먼트: 빠르게 0 · 보통 60 · 천천히 200(ms, 사건마다). */
  function setupSpeed() {
    const seg = $("speed");
    for (const b of seg.querySelectorAll("button")) {
      b.addEventListener("click", () => {
        for (const o of seg.querySelectorAll("button")) o.setAttribute("aria-checked", "false");
        b.setAttribute("aria-checked", "true");
        S.pace = Number(b.getAttribute("data-pace")) || 0;
      });
    }
  }

  /** 모델 세그먼트: 가짜 모델(결정적) · 실제 모델(키·데모 브레인을 쓴다). */
  function setupModels() {
    const seg = $("models");
    for (const b of seg.querySelectorAll("button")) {
      b.addEventListener("click", () => {
        for (const o of seg.querySelectorAll("button")) o.setAttribute("aria-checked", "false");
        b.setAttribute("aria-checked", "true");
        S.models = b.getAttribute("data-models") === "cloud" ? "cloud" : "fake";
        $("cloudNote").hidden = S.models !== "cloud";
        if (S.models === "cloud") loadKeys();
      });
    }
  }

  /** 키 상태 — 이름과 있음/없음만(서버가 값은 주지 않는다). */
  async function loadKeys() {
    const box = $("keys");
    try {
      const k = await api("/api/day/keys", null);
      box.textContent = "키 " + k.present + "/" + k.total + "개";
      const names = (k.keys || []).map((x) => x.name + (x.present ? " 있음" : " 없음"));
      box.title = (k.reason_ko ? k.reason_ko + " · " : "") + names.join(" · ");
      box.dataset.status = k.status;
    } catch (err) {
      box.textContent = "키 상태를 읽지 못했어요";
      box.title = err instanceof Error ? err.message : String(err);
    }
  }

  /** 화면을 준비한다. */
  function init() {
    $("controls").addEventListener("submit", onRun);
    $("stopBtn").addEventListener("click", onStop);
    setupSpeed();
    setupModels();
    loadKeys();
    setButtons();
    render();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
