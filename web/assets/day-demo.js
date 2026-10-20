/* LIOT 하루 시뮬레이션 — 시연 모드(전체 화면 덮개 + 단계 그래프).
 *
 * 서버 trace 사건(nodes[].id·label_ko·detail_ko·real_ms·model·state)을 노드·엣지 그래프로 그린다.
 * 노드 id·문구·시간·상태는 서버 값 그대로이고, 이 파일은 종류별 고정 좌표표로 위치만 정한다.
 * DOM 은 createElementNS(SVG) · createElement(HTML) + textContent 로만 만든다(HTML 문자열을 넣지 않는다).
 * 화살표 머리는 작은 삼각형 path 로 그린다(SVG 화살표 정의 요소·주소 참조 없음).
 * 2단계: 받은 trace 하나를 정적으로 그린다. 재생기(딜레이·대기열)는 3단계.
 */
"use strict";

(function () {
  /** SVG 이름 공간 식별자(불러오는 주소가 아니다). */
  const SVG_NS = "http://www.w3.org/2000/svg";
  const VIEW_W = 1400;
  const VIEW_H = 600;
  const NODE_W = 150;
  const NODE_H = 58;
  /** 노드 아래 설명 칸 높이(foreignObject). */
  const DETAIL_H = 84;
  /** 오른쪽 폰 알림창 자리(그래프와 같은 SVG 안, foreignObject). */
  const PANE = { x: 1040, y: 40, w: 330, h: 520 };

  /**
   * 종류별 노드 좌표(가운데 점). 노드 id 는 서버가 준다 — 표에 없는 id 는 그리지 않고 세지 않는다.
   * @type {Record<string, Record<string, [number, number]>>}
   */
  const LAYOUTS = {
    comfort: {
      sense: [100, 110], rules: [280, 110], score: [460, 110], context: [640, 110], gate1: [820, 110],
      judge: [100, 360], route: [280, 360], gate: [460, 360], phone: [640, 360], reason: [820, 360],
    },
    safety: {
      sense: [100, 250], classify: [280, 250], floor: [460, 250], first_alert: [640, 250],
      phone: [820, 250],
    },
    followup: { model: [100, 250], severity: [280, 250], followup: [460, 250], ladder: [640, 250] },
    feedback: { phone: [820, 250], feedback: [640, 250], prefs: [460, 250], ack: [640, 250] },
  };
  /** 폰 알림창으로 이어지는 노드(피드백은 폰 쪽에서 들어온다). */
  const PANE_NODE = { comfort: "phone", safety: "phone", followup: "followup", feedback: "phone" };

  /** @type {{open: boolean, traces: Object[], shown: Object|null}} */
  const D = { open: false, traces: [], shown: null };

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
      // 줄바꿈(오른쪽 끝 → 다음 줄 왼쪽 끝): 설명 글을 가로지르지 않게 바깥 통로로 돈다.
      const lane = ay + NODE_H / 2 + 4 + DETAIL_H + 14;
      const outX = ax + NODE_W / 2 + 28;
      const inX = bx - NODE_W / 2 - 22;
      return [[ax + NODE_W / 2, ay], [outX, ay], [outX, lane], [inX, lane], [inX, by],
        [bx - NODE_W / 2, by]];
    }
    const mid = (ay + by) / 2;
    const dir = by > ay ? 1 : -1;
    return [[ax, ay + dir * NODE_H / 2], [ax, mid], [bx, mid], [bx, by - dir * NODE_H / 2]];
  }

  /**
   * 엣지(선 + 끝의 작은 삼각형).
   * @param {[number, number][]} pts
   * @param {string} state
   * @returns {SVGElement}
   */
  function edge(pts, state) {
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
    return g;
  }

  /**
   * 노드 하나: 둥근 사각형 + 이름 + 실제 시간(잰 단계만) + 아래 설명(서버 detail_ko).
   * @param {Object} n
   * @param {[number, number]} at
   * @returns {SVGElement}
   */
  function nodeEl(n, at) {
    const [cx, cy] = at;
    const g = svg("g", { class: "dm-node", "data-id": n.id, "data-state": n.state });
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
    const fo = svg("foreignObject", {
      x: cx - 85, y: cy + NODE_H / 2 + 4, width: 170, height: DETAIL_H,
    });
    const detail = el("div", "dm-node-detail", n.detail_ko);
    if (n.model) detail.appendChild(el("span", "dm-node-model", n.model));
    fo.appendChild(detail);
    g.appendChild(fo);
    return g;
  }

  /**
   * 오른쪽 폰 알림창(그래프와 같은 SVG 안). 카드는 일반 화면의 카드를 그대로 빌린다.
   * @param {Object} t
   * @returns {SVGElement}
   */
  function pane(t) {
    const fo = svg("foreignObject", {
      x: PANE.x, y: PANE.y, width: PANE.w, height: PANE.h, class: "dm-pane",
    });
    const screen = el("div", "dm-screen");
    screen.appendChild(el("p", "dm-screen-bar", "거주자 폰"));
    const api = /** @type {any} */ (window).LiotDay;
    const card = api && typeof api.cardFor === "function" ? api.cardFor(t) : null;
    if (card) screen.appendChild(card);
    else screen.appendChild(el("p", "dm-screen-empty", t.outcome_ko || ""));
    fo.appendChild(screen);
    return fo;
  }

  /**
   * trace 하나를 정적으로 그린다(노드·엣지·폰 알림창 연결).
   * @param {Object} t
   */
  function render(t) {
    D.shown = t;
    const box = $("demoSvg");
    clear(box);
    box.setAttribute("viewBox", "0 0 " + VIEW_W + " " + VIEW_H);
    const kind = layoutOf(t);
    const table = LAYOUTS[kind];
    box.dataset.kind = t.kind;
    const drawn = (t.nodes || []).filter((n) => table[n.id]);
    const edges = svg("g", { class: "dm-edges" });
    const nodes = svg("g", { class: "dm-nodes" });
    for (let i = 0; i < drawn.length; i += 1) {
      const n = drawn[i];
      nodes.appendChild(nodeEl(n, table[n.id]));
      if (i > 0) edges.appendChild(edge(route(table[drawn[i - 1].id], table[n.id]), n.state));
    }
    const target = drawn.find((n) => n.id === PANE_NODE[kind]);
    if (target) {
      const [x, y] = table[target.id];
      // 같은 줄 오른쪽에 다른 노드가 있으면(이유 작성) 위로 빠져나가 그 노드를 넘는다.
      const blocked = drawn.some((n) => n !== target && table[n.id][1] === y && table[n.id][0] > x);
      const pts = /** @type {[number, number][]} */ (
        blocked ? [[x, y - NODE_H / 2], [x, y - NODE_H / 2 - 26], [PANE.x, y - NODE_H / 2 - 26]]
          : [[x + NODE_W / 2, y], [PANE.x, y]]);
      const link = edge(t.kind === "feedback" ? pts.slice().reverse() : pts, target.state);
      link.classList.add("dm-edge-pane");
      edges.appendChild(link);
    }
    box.appendChild(edges);
    box.appendChild(nodes);
    box.appendChild(pane(t));
    $("demoOutcome").textContent = t.outcome_ko || "";
    const now = drawn.find((n) => n.state === "end") || drawn[drawn.length - 1];
    $("demoCaption").textContent = now ? now.label_ko + " — " + now.detail_ko : "";
    $("demoWaiting").hidden = true;
  }

  /** 덮개를 연다. 이미 받은 trace 가 있으면 마지막 것을 그린다. */
  function open() {
    D.open = true;
    $("demo").hidden = false;
    document.body.classList.add("dm-lock");
    const last = D.traces[D.traces.length - 1];
    if (last) render(last);
    else $("demoWaiting").hidden = false;
    $("demoClose").focus();
  }

  /** 덮개를 닫는다(실행은 계속 돈다). */
  function close() {
    D.open = false;
    $("demo").hidden = true;
    document.body.classList.remove("dm-lock");
  }

  /** 새 실행: 받은 trace 를 비운다. */
  function reset() {
    D.traces = [];
    D.shown = null;
    clear($("demoSvg"));
    $("demoOutcome").textContent = "";
    $("demoCaption").textContent = "";
    $("demoWaiting").hidden = false;
  }

  /**
   * day.js 가 trace 사건마다 부른다. 2단계는 받은 것을 쌓고, 덮개가 열려 있으면 마지막 것을 그린다.
   * @param {Object} t
   */
  function push(t) {
    D.traces.push(t);
    if (D.open) render(t);
  }

  function init() {
    $("demoClose").addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (D.open && e.key === "Escape") close();
    });
  }

  /** @type {any} */ (window).LiotDemo = { open: open, close: close, push: push, reset: reset, render: render,
    state: D };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
