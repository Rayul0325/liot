/* LIOT 하루 시뮬레이션 — 시연 모드(전체 화면 덮개 + 단계 그래프).
 *
 * 서버 trace 사건(nodes[].id·label_ko·detail_ko·real_ms·model·state·branches·edge_ko)을 분기 그래프로
 * 그린다: 판단 노드 아래에 갈 수 있던 길(갈림길 칩)을 모두 쌓고 간 길만 강조하며, 간 엣지 위에 서버의
 * 판단 문구(edge_ko) 알약을 얹는다. 노드 id·문구·시간·상태·갈림길은 서버 값 그대로이고, 이 파일은
 * 종류별 고정 좌표표로 위치만 정한다. 노드 설명(detail_ko)은 아래 자막과 노드의 SVG 제목(title)에 둔다.
 * DOM 은 createElementNS(SVG) · createElement(HTML) + textContent 로만 만든다(HTML 문자열을 넣지 않는다).
 * 화살표 머리는 작은 삼각형 path 로 그린다(SVG 화살표 정의 요소·주소 참조 없음).
 * 재생기: 받은 순서대로 trace 를 재생한다. 노드마다 max(실제 시간, 속도의 최소)만큼 강조하고(서버는
 * 늦추지 않는다 — 딜레이는 이 화면만 넣는다), 폰 노드에 닿으면 오른쪽 폰에 카드가 뜬다.
 */
"use strict";

(function () {
  /** SVG 이름 공간 식별자(불러오는 주소가 아니다). */
  const SVG_NS = "http://www.w3.org/2000/svg";
  const VIEW_W = 1600;
  const VIEW_H = 760;
  const NODE_W = 130;
  const NODE_H = 58;
  /** 갈림길 칩 묶음: 노드 아래 y+40 ~ y+240, 칩 하나 22. 노드 왼쪽 끝에 맞추고 폭은 열 간격(240)보다 좁게
   * — 다음 열 칩과 24 띄운다(왼쪽 바깥 8 은 간 칩 연결선 통로). */
  const CHIP = { top: 40, bottom: 240, h: 22, w: 216 };
  /** 엣지 문구 알약: 아래 끝이 엣지 위 34, 폭은 열 간격(240) 안. 줄바꿈하고 줄이지 않는다. */
  const PILL = { gap: 34, w: 230, h: 90 };
  /** 오른쪽 폰 알림창 자리(그래프와 같은 SVG 안, foreignObject). */
  const PANE = { x: 1240, y: 40, w: 340, h: 680 };
  /** 좁은 화면(day.css 와 같은 640px): 노드를 한 줄로 세우고 설명은 오른쪽, 폰은 그래프 아래. */
  const NARROW_MQ = "(max-width: 640px)";
  const NARROW = { w: 370, x: 85, top: 40, row: 100 };

  /**
   * 종류별 노드 좌표(가운데 점). 노드 id 는 서버가 준다 — 표에 없는 id 는 그리지 않고 세지 않는다.
   * @type {Record<string, Record<string, [number, number]>>}
   */
  // 그래프 영역 x 20~1220: 노드 폭 130, 열 간격 240 → 열 가운데 85·325·565·805·1045.
  const LAYOUTS = {
    comfort: {
      sense: [85, 130], rules: [325, 130], score: [565, 130], context: [805, 130], gate1: [1045, 130],
      judge: [85, 450], route: [325, 450], gate: [565, 450], phone: [805, 450], reason: [1045, 450],
    },
    safety: {
      sense: [85, 300], classify: [325, 300], floor: [565, 300], first_alert: [805, 300],
      phone: [1045, 300],
    },
    followup: { model: [85, 300], severity: [325, 300], followup: [565, 300], ladder: [805, 300] },
    feedback: { phone: [1045, 300], feedback: [805, 300], prefs: [565, 300], ack: [805, 300] },
  };
  /** 폰 알림창으로 이어지는 노드(피드백은 폰 쪽에서 들어온다). */
  const PANE_NODE = { comfort: "phone", safety: "phone", followup: "followup", feedback: "phone" };

  /** 속도별 노드 최소 강조 시간(ms) — 화면 전용. */
  const SPEEDS = { slow: 1500, normal: 800, fast: 300 };

  /**
   * 시연 상태.
   * @type {{open: boolean, traces: Object[], queue: Object[], shown: Object|null, playing: boolean,
   *   skip: boolean, wake: (function(): void)|null, speed: "slow"|"normal"|"fast", played: number,
   *   cards: Map<string, HTMLElement>, screen: HTMLElement|null, feed: HTMLElement|null,
   *   nodeEls: Map<string, Element>, edgeEls: Map<string, Element>, drawn: Object[],
   *   paneNode: string|null, marks: Object[], feedDay: number|null, gen: number}}
   */
  const D = {
    open: false, traces: [], queue: [], shown: null, playing: false, skip: false, wake: null,
    speed: "normal", played: 0, cards: new Map(), screen: null, feed: null, nodeEls: new Map(),
    edgeEls: new Map(), drawn: [], paneNode: null, marks: [], feedDay: null,
    // 재생 세대: 새 실행·처음부터 다시 보기마다 1 오른다. 진행 중이던 play() 는 세대가 바뀌면 손을 뗀다.
    gen: 0,
  };

  /**
   * @param {string} tag
   * @param {Record<string, string|number>} [attrs]
   * @returns {SVGElement}
   */
  function svg(tag, attrs) {
    const node = /** @type {SVGElement} */ (document.createElementNS(SVG_NS, tag));
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }

  /**
   * @param {string} tag
   * @param {string} [cls]
   * @param {string|null} [text]
   * @returns {HTMLElement}
   */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /**
   * @param {string} id
   * @returns {HTMLElement}
   */
  function $(id) {
    const node = document.getElementById(id);
    if (!node) throw new Error("missing #" + id);
    return node;
  }

  /** @param {Element} node */
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  /**
   * trace 의 좌표표 이름. 안전 followup 은 따로, 침묵은 편의 표를 쓴다.
   * @param {Object} t
   * @returns {string}
   */
  function layoutOf(t) {
    if (t.kind === "safety") return t.part === "followup" ? "followup" : "safety";
    if (t.kind === "feedback") return "feedback";
    return "comfort";
  }

  /**
   * 꺾인 엣지의 점들(노드 가장자리에서 가장자리로). 같은 줄이면 직선, 다르면 아래로 → 옆 → 아래.
   * @param {[number, number]} a
   * @param {[number, number]} b
   * @returns {[number, number][]}
   */
  function route(a, b) {
    const [ax, ay] = a;
    const [bx, by] = b;
    if (ay === by) {
      const dir = bx > ax ? 1 : -1;
      return [[ax + dir * NODE_W / 2, ay], [bx - dir * NODE_W / 2, by]];
    }
    if (ax === bx) {
      const dir = by > ay ? 1 : -1;
      return [[ax, ay + dir * NODE_H / 2], [bx, by - dir * NODE_H / 2]];
    }
    if (bx < ax && by > ay) {
      // 줄바꿈(오른쪽 끝 → 다음 줄 왼쪽 끝): 칩 묶음을 가로지르지 않게 바깥 통로로 돈다.
      const lane = laneY(ay);
      const outX = ax - NODE_W / 2 + CHIP.w + 10; // 윗줄 마지막 열의 칩 묶음 오른쪽 바깥
      const inX = bx - NODE_W / 2 - 10;
      return [[ax + NODE_W / 2, ay], [outX, ay], [outX, lane], [inX, lane], [inX, by],
        [bx - NODE_W / 2, by]];
    }
    const mid = (ay + by) / 2;
    const dir = by > ay ? 1 : -1;
    return [[ax, ay + dir * NODE_H / 2], [ax, mid], [bx, mid], [bx, by - dir * NODE_H / 2]];
  }

  /**
   * 줄바꿈 엣지가 지나는 통로의 높이: 윗줄 칩 묶음 아래 끝과 아랫줄 노드 사이.
   * @param {number} ay
   * @returns {number}
   */
  function laneY(ay) {
    return ay + CHIP.bottom + 10;
  }

  /**
   * 엣지 문구 알약(서버 edge_ko 그대로). 아래 끝이 bottom 이고 위로 자란다 — 줄바꿈하며 줄이지 않는다.
   * @param {string} text
   * @param {number} cx 가운데 x
   * @param {number} bottom 알약 아래 끝 y
   * @returns {SVGElement}
   */
  function pillEl(text, cx, bottom) {
    const fo = svg("foreignObject", {
      x: cx - PILL.w / 2, y: bottom - PILL.h, width: PILL.w, height: PILL.h, class: "dm-pill-box",
    });
    const wrap = el("div", "dm-pill-wrap");
    wrap.appendChild(el("span", "dm-pill", text));
    fo.appendChild(wrap);
    return fo;
  }

  /**
   * 엣지(선 + 끝의 작은 삼각형).
   * @param {[number, number][]} pts
   * @param {string} state
   * @returns {SVGElement}
   */
  function edge(pts, state, text) {
    const g = svg("g", { class: "dm-edge", "data-state": state });
    const d = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0] + "," + p[1]).join(" ");
    g.appendChild(svg("path", { d: d, class: "dm-edge-line" }));
    const [px, py] = pts[pts.length - 2];
    const [qx, qy] = pts[pts.length - 1];
    const len = Math.hypot(qx - px, qy - py) || 1;
    const ux = (qx - px) / len;
    const uy = (qy - py) / len;
    const s = 7;
    const tri = [
      [qx, qy],
      [qx - ux * s * 1.6 - uy * s, qy - uy * s * 1.6 + ux * s],
      [qx - ux * s * 1.6 + uy * s, qy - uy * s * 1.6 - ux * s],
    ];
    g.appendChild(svg("path", {
      d: "M" + tri.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" L") + " Z",
      class: "dm-edge-head",
    }));
    if (text) {
      if (pts.length > 2) {
        // 줄바꿈 엣지: 통로 오른쪽 끝(윗줄 마지막 열 아래) 위에 둔다 — 통로 가운데는 아랫줄 엣지
        // 문구 알약과 겹친다(아랫줄 알약은 그 줄 위 34 에서 위로 자란다).
        const [x0, y0] = pts[2];
        g.appendChild(pillEl(text, x0 - PILL.w / 2, y0 - 4));
      } else {
        g.appendChild(pillEl(text, (pts[0][0] + pts[1][0]) / 2, pts[0][1] - PILL.gap));
      }
    }
    return g;
  }

  /**
   * 그릴 갈림길: 서버 목록 그대로, 단 '기록 없음'(unknown)·'반영되지 않음'(refused)은 간 경우에만.
   * @param {Object} n
   * @returns {Object[]}
   */
  function visibleBranches(n) {
    return (n.branches || []).filter((b) => b.taken || (b.key !== "unknown" && b.key !== "refused"));
  }

  /**
   * 갈림길 칩 한 줄의 글: 이름 + 값(있으면).
   * @param {Object} b
   * @returns {string}
   */
  function chipText(b) {
    return b.value_ko ? b.label_ko + " · " + b.value_ko : b.label_ko;
  }

  /**
   * 노드 아래 갈림길 칩 묶음 + 노드에서 간 칩으로 가는 짧은 연결선.
   * 칩은 한 줄(22) — 넘치는 글은 칩 제목(마우스오버)과 자막으로 본다.
   * @param {Object} n
   * @param {[number, number]} at
   * @returns {SVGElement|null}
   */
  function chipsEl(n, at) {
    const shown = visibleBranches(n);
    if (!shown.length) return null;
    const [cx, cy] = at;
    const g = svg("g", { class: "dm-chips", "data-id": n.id });
    const top = cy + CHIP.top;
    const left = cx - NODE_W / 2;
    const fo = svg("foreignObject", {
      x: left, y: top, width: CHIP.w, height: CHIP.bottom - CHIP.top, class: "dm-chips-box",
    });
    const list = el("div", "dm-chip-list");
    shown.forEach((b) => {
      const chip = el("div", "dm-chip" + (b.taken ? " dm-chip-taken" : "") + (b.exit ? " dm-chip-exit" : ""),
        chipText(b));
      chip.dataset.key = b.key;
      chip.title = chipText(b);
      list.appendChild(chip);
    });
    fo.appendChild(list);
    const i = shown.findIndex((b) => b.taken);
    if (i >= 0) {
      // 노드 아래 → 칩 묶음 왼쪽 바깥 통로 → 간 칩 가운데(다른 칩을 가로지르지 않는다).
      const lane = left - 8;
      const mid = top + i * CHIP.h + CHIP.h / 2;
      const startX = left + 10;
      g.appendChild(svg("path", {
        d: "M" + startX + "," + (cy + NODE_H / 2) + " L" + startX + "," + (top - 6) + " L" + lane + ","
          + (top - 6) + " L" + lane + "," + mid + " L" + left + "," + mid,
        class: "dm-chip-link",
      }));
    }
    g.appendChild(fo);
    return g;
  }

  /**
   * 노드 하나: 둥근 사각형 + 이름 + 실제 시간(잰 단계만). 설명은 SVG 제목(마우스오버)과 자막.
   * @param {Object} n
   * @param {[number, number]} at
   * @param {boolean} [thin] 좁은 화면(칩은 아직 그리지 않는다)
   * @returns {SVGElement}
   */
  function nodeEl(n, at, thin) {
    const [cx, cy] = at;
    const g = svg("g", { class: "dm-node", "data-id": n.id, "data-state": n.state });
    const title = svg("title");
    title.textContent = n.label_ko + " — " + n.detail_ko + (n.model ? " · " + n.model : "");
    g.appendChild(title);
    g.appendChild(svg("rect", {
      x: cx - NODE_W / 2, y: cy - NODE_H / 2, width: NODE_W, height: NODE_H, rx: 12,
      class: "dm-node-box",
    }));
    const label = svg("text", { x: cx, y: cy - 4, class: "dm-node-label", "text-anchor": "middle" });
    label.textContent = n.label_ko;
    g.appendChild(label);
    const meta = svg("text", { x: cx, y: cy + 16, class: "dm-node-time", "text-anchor": "middle" });
    meta.textContent = typeof n.real_ms === "number" ? "실제 " + n.real_ms + "ms" : "";
    g.appendChild(meta);
    if (!thin) {
      const chips = chipsEl(n, at);
      if (chips) g.appendChild(chips);
    }
    return g;
  }

  /**
   * 자막 한 줄: 노드 이름 — 설명 (· 간 갈림길) (→ 엣지 문구). 모두 서버 글 그대로.
   * @param {Object} n
   * @returns {string}
   */
  function captionOf(n) {
    const taken = (n.branches || []).find((b) => b.taken);
    let line = n.label_ko + " — " + n.detail_ko;
    if (taken && taken.value_ko) line += " · " + chipText(taken);
    if (n.edge_ko) line += " → " + n.edge_ko;
    return line;
  }

  // ── 폰 알림창(그날 카드만 — 가장 최근에 다룬 카드가 맨 위) ─────────────────────

  /** @returns {HTMLElement} */
  function screenEl() {
    if (!D.screen) {
      D.screen = el("div", "dm-screen");
      D.screen.appendChild(el("p", "dm-screen-bar", "거주자 폰"));
      D.feed = el("div", "dm-feed");
      D.screen.appendChild(D.feed);
    }
    return D.screen;
  }

  /** @returns {SVGElement} */
  function pane() {
    const fo = svg("foreignObject", {
      x: PANE.x, y: PANE.y, width: PANE.w, height: PANE.h, class: "dm-pane",
    });
    fo.appendChild(screenEl());
    return fo;
  }

  /** @returns {boolean} */
  function narrow() {
    return Boolean(window.matchMedia && window.matchMedia(NARROW_MQ).matches);
  }

  /**
   * 폰 알림창을 붙인다: 넓은 화면은 그래프 SVG 안(오른쪽), 좁은 화면은 그래프 아래 칸.
   * @param {Element} box
   * @param {boolean} thin
   */
  function placePane(box, thin) {
    const below = $("demoPane");
    below.hidden = !thin;
    if (thin) below.appendChild(screenEl());
    else box.appendChild(pane());
  }

  /** @returns {any} */
  function day() {
    return /** @type {any} */ (window).LiotDay || {};
  }

  /**
   * 새 날의 trace 를 재생하면 알림창을 비우고 맨 위에 'n일차' 구분선을 둔다.
   * @param {Object} t
   */
  function enterDay(t) {
    const n = typeof t.day === "number" ? t.day : null;
    if (n === null || n === D.feedDay) return;
    D.feedDay = n;
    screenEl();
    clear(D.feed);
    D.cards = new Map();
    D.feed.appendChild(el("p", "dm-day-divider", n + "일차"));
  }

  /**
   * 카드를 맨 위(구분선 바로 아래)로 올리고 알림창을 맨 위로 되돌린다.
   * @param {HTMLElement} card
   */
  function toTop(card) {
    screenEl();
    const divider = D.feed.querySelector(".dm-day-divider");
    const first = divider ? divider.nextSibling : D.feed.firstChild;
    if (first !== card) D.feed.insertBefore(card, first);
    D.feed.scrollTop = 0;
  }

  /**
   * 폰 카드 하나를 맨 위에 둔다(같은 제안이면 그 카드를 올린다). 문구는 서버 기록 그대로.
   * @param {Object} t
   * @returns {HTMLElement|null}
   */
  function ensureCard(t) {
    const id = t.proposal_id;
    if (!id) return null;
    const have = D.cards.get(id);
    if (have) {
      toTop(have);
      follow($("demoPane"));
      return have;
    }
    const safety = t.kind === "safety";
    const rec = safety ? (day().alert ? day().alert(id) : null) : (day().proposal ? day().proposal(id) : null);
    const card = el("article", safety ? "alarm dm-card" : "notif dm-card");
    card.dataset.proposalId = id;
    const head = el("div", "notif-head");
    head.appendChild(el("span", "notif-app", safety && rec && rec.ntfy ? rec.ntfy.title : "LIOT"));
    head.appendChild(el("span", "tabnum", rec ? rec.hhmm : ""));
    card.appendChild(head);
    if (safety) {
      const title = rec ? rec.situation_ko + (rec.severity_ko ? " · " + rec.severity_ko : "") : "";
      card.appendChild(el("p", "alarm-title", title));
      if (rec && rec.ntfy) card.appendChild(el("p", "alarm-body", rec.ntfy.body));
    } else {
      card.appendChild(el("p", "notif-title", rec ? rec.title_ko : ""));
      if (rec && rec.destination === "tray") card.appendChild(el("p", "dm-card-tag", "모아보기"));
      card.appendChild(el("p", "notif-body dm-card-reason", ""));
      card.appendChild(el("p", "notif-state dm-card-state", ""));
    }
    D.cards.set(id, card);
    toTop(card);
    follow($("demoPane"));
    return card;
  }

  /**
   * 카드의 이유 문장을 채운다(서버의 reason_ko).
   * @param {string} id
   */
  function fillReason(id) {
    const card = D.cards.get(id);
    const rec = day().proposal ? day().proposal(id) : null;
    const slot = card ? card.querySelector(".dm-card-reason") : null;
    if (slot && rec) slot.textContent = rec.reason_ko || "";
  }

  /**
   * 카드 아래에 한 줄을 더한다(안전 followup·확인, 피드백 상태).
   * @param {string} id
   * @param {string} line
   * @param {string} [state]
   */
  function noteCard(id, line, state) {
    const card = D.cards.get(id);
    if (!card) return;
    toTop(card);
    const slot = card.querySelector(".dm-card-state");
    if (slot) {
      slot.textContent = line;
      if (state) /** @type {HTMLElement} */ (slot).dataset.state = state;
    } else {
      card.appendChild(el("p", "alarm-meta", line));
    }
  }

  /**
   * 노드에 들어설 때 폰에 생기는 일.
   * @param {Object} t
   * @param {Object} n
   */
  function onEnter(t, n) {
    if (n.id !== "phone" || n.state === "skipped") return;
    if (t.kind === "comfort") {
      ensureCard(t);
      // 이유 노드가 없으면(가짜 모델) 이유 문장도 지금 보인다. 있으면 그 노드가 끝날 때.
      if (!(t.nodes || []).some((x) => x.id === "reason")) fillReason(t.proposal_id);
    } else if (t.kind === "safety") {
      ensureCard(t);
    } else if (t.kind === "feedback") {
      const card = ensureCard(t);
      if (card) card.classList.add("dm-card-touched");
    }
  }

  /**
   * 노드를 마칠 때 폰에 생기는 일.
   * @param {Object} t
   * @param {Object} n
   */
  function onLeave(t, n) {
    if (n.id === "reason" && n.state !== "skipped") fillReason(t.proposal_id);
    if (t.kind === "safety" && n.id === "followup") noteCard(t.proposal_id, n.detail_ko);
    if (t.kind === "feedback" && (n.id === "feedback" || n.id === "ack")) {
      const rec = day().proposal ? day().proposal(t.proposal_id) : null;
      if (n.id === "ack") noteCard(t.proposal_id, t.outcome_ko + " · " + n.detail_ko);
      else noteCard(t.proposal_id, rec && rec.state_ko ? rec.state_ko : n.detail_ko,
        rec ? rec.state : undefined);
      const card = D.cards.get(t.proposal_id);
      if (card) card.classList.remove("dm-card-touched");
    }
  }

  // ── 그래프 ────────────────────────────────────────────────────────────

  /**
   * trace 하나의 그래프를 그린다. 재생 전 노드는 모두 '대기'(pending) 상태다.
   * @param {Object} t
   * @param {boolean} [final] true 면 서버 상태(done·end·skipped) 그대로 정적으로 그린다.
   */
  function render(t, final) {
    D.shown = t;
    D.nodeEls = new Map();
    D.edgeEls = new Map();
    const box = $("demoSvg");
    clear(box);
    const thin = narrow();
    const kind = layoutOf(t);
    const wide = LAYOUTS[kind];
    box.dataset.kind = t.kind;
    const drawn = (t.nodes || []).filter((n) => wide[n.id]);
    D.drawn = drawn;
    /** @type {Record<string, [number, number]>} */
    let table = wide;
    if (thin) {
      // 좁은 화면: 서버가 준 순서대로 위에서 아래로 한 줄.
      table = {};
      drawn.forEach((n, i) => { table[n.id] = [NARROW.x, NARROW.top + i * NARROW.row]; });
    }
    const height = thin ? NARROW.top + Math.max(drawn.length, 1) * NARROW.row - NODE_H / 2 : VIEW_H;
    box.setAttribute("viewBox", "0 0 " + (thin ? NARROW.w : VIEW_W) + " " + height);
    box.classList.toggle("dm-svg-thin", thin);
    const edges = svg("g", { class: "dm-edges" });
    const nodes = svg("g", { class: "dm-nodes" });
    for (let i = 0; i < drawn.length; i += 1) {
      const n = drawn[i];
      const g = nodeEl(n, table[n.id], thin);
      if (!final) {
        g.setAttribute("data-state", "pending");
        const meta = g.querySelector(".dm-node-time");
        if (meta) meta.textContent = "";
      }
      D.nodeEls.set(n.id, g);
      nodes.appendChild(g);
      if (i > 0) {
        const e = edge(route(table[drawn[i - 1].id], table[n.id]), final ? n.state : "pending",
          thin ? null : drawn[i - 1].edge_ko);
        D.edgeEls.set(n.id, e);
        edges.appendChild(e);
      }
    }
    const target = drawn.find((n) => n.id === PANE_NODE[kind]);
    if (target && !thin) {
      const [x, y] = table[target.id];
      // 같은 줄 오른쪽에 다른 노드가 있으면(이유 작성) 위로 빠져나가 그 노드를 넘는다.
      const blocked = drawn.some((n) => n !== target && table[n.id][1] === y && table[n.id][0] > x);
      const pts = /** @type {[number, number][]} */ (
        blocked ? [[x, y - NODE_H / 2], [x, y - NODE_H / 2 - 26], [PANE.x, y - NODE_H / 2 - 26]]
          : [[x + NODE_W / 2, y], [PANE.x, y]]);
      const link = edge(t.kind === "feedback" ? pts.slice().reverse() : pts,
        final ? target.state : "pending");
      link.classList.add("dm-edge-pane");
      D.edgeEls.set("__pane__", link);
      edges.appendChild(link);
    }
    D.paneNode = target ? target.id : null;
    box.appendChild(edges);
    box.appendChild(nodes);
    placePane(box, thin);
    $("demoOutcome").textContent = t.outcome_ko || "";
    $("demoWaiting").hidden = true;
    if (final) {
      const now = drawn.find((n) => n.state === "end") || drawn[drawn.length - 1];
      $("demoCaption").textContent = now ? captionOf(now) : "";
    }
  }

  // ── 재생기 ────────────────────────────────────────────────────────────

  /** @returns {boolean} */
  function reducedMotion() {
    return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /**
   * 좁은 화면에서 덮개 본문을 그 요소가 보이게 넘긴다(부드러운 스크롤 없이 바로).
   * 동작 줄이기 설정이면 넘기지 않는다.
   * @param {Element|null|undefined} target
   */
  function follow(target) {
    if (!target || !narrow() || reducedMotion()) return;
    target.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
  }

  /**
   * 모델 단계(시간을 그대로 보여 주는 노드): 모델이 있는 위험 판정, 2단계 판정, 이유 작성.
   * @param {Object} n
   * @returns {boolean}
   */
  function isModelNode(n) {
    return n.id === "reason" || n.id === "model" || (n.id === "judge" && Boolean(n.model));
  }

  /**
   * @param {number} ms
   * @returns {string}
   */
  function fmtMs(ms) {
    return ms >= 1000 ? (ms / 1000).toFixed(2) + "초" : ms + "ms";
  }

  /**
   * 노드의 강조 시간(ms) = max(실제, 속도의 최소).
   * @param {Object} n
   * @returns {number}
   */
  function holdOf(n) {
    const real = typeof n.real_ms === "number" ? n.real_ms : 0;
    return Math.max(real, SPEEDS[D.speed]);
  }

  /**
   * 노드 아래 시간 글: 코드 단계는 '실제 Nms · 시연 X초', 모델 단계는 실제 시간만.
   * @param {Object} n
   * @returns {string}
   */
  function timeLabel(n) {
    const real = typeof n.real_ms === "number" ? n.real_ms : null;
    if (isModelNode(n)) return real === null ? "실제 시간 없음" : "실제 " + fmtMs(real);
    const demo = "시연 " + (holdOf(n) / 1000).toFixed(1) + "초";
    return real === null ? demo : "실제 " + fmtMs(real) + " · " + demo;
  }

  /**
   * 기다린다. [건너뛰기]면 바로 끝난다.
   * @param {number} ms
   * @returns {Promise<void>}
   */
  function wait(ms) {
    if (D.skip) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        D.wake = null;
        resolve();
      }, ms);
      D.wake = () => {
        window.clearTimeout(timer);
        D.wake = null;
        resolve();
      };
    });
  }

  /**
   * 노드·엣지 상태를 바꾸고 바뀐 때를 남긴다(시험·확인용).
   * @param {string} id
   * @param {string} state
   */
  function mark(id, state) {
    const g = D.nodeEls && D.nodeEls.get(id);
    if (g) g.setAttribute("data-state", state);
    const e = D.edgeEls && D.edgeEls.get(id);
    if (e) e.setAttribute("data-state", state);
    D.marks.push({ trace: D.shown ? D.shown.trace_id : null, id: id, state: state, at: performance.now() });
    if (D.marks.length > 2000) D.marks.splice(0, 1000);
  }

  /**
   * trace 하나를 재생한다: 노드마다 강조 → 완료(또는 끝·건너뜀), 폰 연결.
   * @param {Object} t
   */
  async function play(t) {
    const gen = D.gen;
    D.skip = false;
    enterDay(t);
    render(t, false);
    $("demoSvg").classList.toggle("dm-anim", !reducedMotion());
    for (const n of D.drawn) {
      if (n.state === "skipped") {
        mark(n.id, "skipped");
        continue;
      }
      mark(n.id, "current");
      follow(D.nodeEls.get(n.id));
      const meta = D.nodeEls.get(n.id).querySelector(".dm-node-time");
      if (meta) meta.textContent = timeLabel(n);
      $("demoCaption").textContent = captionOf(n);
      onEnter(t, n);
      if (n.id === D.paneNode && D.edgeEls.get("__pane__")) {
        D.edgeEls.get("__pane__").setAttribute("data-state", "current");
      }
      await wait(holdOf(n));
      if (gen !== D.gen) return; // 새 실행·다시 보기가 시작됐다 — 낡은 카드를 새 창에 넣지 않는다
      mark(n.id, n.state === "end" ? "end" : "done");
      if (n.id === D.paneNode && D.edgeEls.get("__pane__")) {
        D.edgeEls.get("__pane__").setAttribute("data-state", "done");
      }
      onLeave(t, n);
    }
    if (gen !== D.gen) return;
    await wait(SPEEDS[D.speed] / 2);
  }

  /** 대기열을 도착 순서대로 재생한다(덮개가 열려 있는 동안). */
  async function loop() {
    if (D.playing) return;
    D.playing = true;
    try {
      while (D.open && D.queue.length) {
        const t = D.queue.shift();
        showWaiting();
        await play(t);
        D.played += 1;
      }
    } finally {
      D.playing = false;
      showWaiting();
    }
  }

  /** '대기 n건'(지금 재생 중인 것은 빼고). */
  function showWaiting() {
    $("demoQueue").textContent = "대기 " + D.queue.length + "건";
    /** @type {HTMLButtonElement} */ ($("demoSkip")).disabled = !D.playing;
    /** @type {HTMLButtonElement} */ ($("demoReplay")).disabled = !D.traces.length;
  }

  /** 재생 중인 것을 멈추고(세대 올림) 알림창·그래프를 비운다. 받아 둔 trace 목록은 부르는 쪽이 정한다. */
  function restart() {
    D.gen += 1;
    D.queue = [];
    D.shown = null;
    D.cards = new Map();
    D.feedDay = null;
    D.played = 0;
    D.skip = true;
    if (D.wake) D.wake();
    if (D.feed) clear(D.feed);
    clear($("demoSvg"));
    $("demoOutcome").textContent = "";
    $("demoCaption").textContent = "";
    $("demoWaiting").hidden = false;
  }

  /** [처음부터 다시 보기]: 이번 실행에서 받은 trace 를 처음부터 다시 재생한다(새 실행은 시작하지 않는다). */
  function replay() {
    if (!D.traces.length) return;
    restart();
    D.queue = D.traces.slice();
    showWaiting();
    if (D.open) loop();
  }

  /** [건너뛰기]: 지금 trace 를 바로 끝까지(상태는 차례대로 바뀐다) 보내고 다음으로. */
  function skip() {
    if (!D.playing) return;
    D.skip = true;
    if (D.wake) D.wake();
  }

  /**
   * 덮개를 연다. 닫기 전에 남은 대기열을 이어서 재생한다(처음부터는 [처음부터 다시 보기]).
   * @param {string} [note] 대기 문구(시연 기록이 아직 없을 때 day.js 가 준다)
   */
  function open(note) {
    $("demoWaiting").textContent = note || "첫 사건을 기다려요.";
    D.open = true;
    $("demo").hidden = false;
    document.body.classList.add("dm-lock");
    if (!D.queue.length && !D.playing && !D.shown) $("demoWaiting").hidden = false;
    showWaiting();
    $("demoClose").focus();
    loop();
  }

  /** 덮개를 닫는다(실행은 계속 돈다. 재생은 다음에 열 때 이어 간다). */
  function close() {
    D.open = false;
    $("demo").hidden = true;
    document.body.classList.remove("dm-lock");
  }

  /** 새 실행: 받아 둔 trace·대기열·카드를 비운다(진행 중이던 재생은 세대가 바뀌어 손을 뗀다). */
  function reset() {
    restart();
    D.traces = [];
    showWaiting();
  }

  /**
   * day.js 가 trace 사건마다 부른다. 대기열에 넣고, 덮개가 열려 있으면 재생을 이어 간다.
   * @param {Object} t
   */
  function push(t) {
    D.traces.push(t);
    D.queue.push(t);
    showWaiting();
    if (D.open) loop();
  }

  /** 속도 세그먼트(화면 전용 — 서버 실행 속도와 무관). */
  function setupSpeed() {
    const seg = $("demoSpeed");
    for (const b of seg.querySelectorAll("button")) {
      b.addEventListener("click", () => {
        for (const o of seg.querySelectorAll("button")) o.setAttribute("aria-checked", "false");
        b.setAttribute("aria-checked", "true");
        const v = b.getAttribute("data-speed");
        D.speed = v === "slow" || v === "fast" ? v : "normal";
      });
    }
  }

  function init() {
    $("demoClose").addEventListener("click", close);
    $("demoSkip").addEventListener("click", skip);
    $("demoReplay").addEventListener("click", replay);
    setupSpeed();
    document.addEventListener("keydown", (e) => {
      if (D.open && e.key === "Escape") close();
    });
    // 화면 폭이 640px 를 넘나들면 쉬는 동안의 그래프를 새 배치로 다시 그린다(재생 중이면 다음 trace 부터).
    const mq = window.matchMedia ? window.matchMedia(NARROW_MQ) : null;
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", () => {
        if (D.shown && !D.playing) render(D.shown, true);
      });
    }
    showWaiting();
  }

  /** @type {any} */ (window).LiotDemo = { open: open, close: close, push: push, reset: reset, render: render,
    skip: skip, replay: replay, captionOf: captionOf, state: D };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
