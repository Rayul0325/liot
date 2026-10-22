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
  /** 노드 높이: 이름(18) + 시간 두 줄(15) — '실제 …ms · 시연 …초'는 폭 130 에 한 줄로 들지 않는다. */
  const NODE_H = 74;
  /** 갈림길 칩 묶음: 노드 아래 y+46 ~ y+240, 칩 한 칸 26(칩 24 + 틈 2). 묶음은 노드 왼쪽 끝에서 시작해
   * 폭 216(열 간격 240 — 다음 열과 24 띄운다). 줄기는 노드 왼쪽 끝 +18, 칩은 +34 부터. */
  const CHIP = { top: 46, bottom: 240, h: 26, w: 216, spine: 18, indent: 34 };
  /** 엣지 문구 알약: 아래 끝이 엣지 위 42(노드 위 끝 37 바로 위), 폭은 열 간격(240) 안. 줄바꿈하고 줄이지 않는다. */
  const PILL = { gap: 42, w: 230, h: 160 };
  /** 오른쪽 폰 알림창 자리(그래프와 같은 SVG 안, foreignObject). */
  const PANE = { x: 1240, y: 40, w: 340, h: 680 };
  /** 좁은 화면(day.css 와 같은 640px): 노드를 한 줄로 세우고 설명은 오른쪽, 폰은 그래프 아래. */
  const NARROW_MQ = "(max-width: 640px)";
  /** 좁은 화면 한 줄: 노드 x 20~150, 그 오른쪽 칩(x 170~366), 줄 사이 엣지 문구(x 100~366).
   * 줄 높이는 노드·칩·문구의 실제 높이로 정한다(foreignObject 도 그 높이 — 잘리지 않는다). */
  const NARROW = { w: 370, x: 85, top: 24, chipX: 170, chipW: 196, pillX: 100, pillW: 266, gap: 10 };

  /**
   * 종류별 노드 좌표(가운데 점). 노드 id 는 서버가 준다 — 표에 없는 id 는 그리지 않고 세지 않는다.
   * @type {Record<string, Record<string, [number, number]>>}
   */
  // 그래프 영역 x 20~1220: 노드 폭 130, 열 간격 240 → 열 가운데 85·325·565·805·1045.
  // 편의는 두 줄(y 220·590). 위 '침묵' 길(y 50)은 1줄 출구, 아래 '침묵' 길(y 700)과 알림 게이트 밑
  // '모아보기'·'보류'는 2줄 출구를 받는다. 점수 후보 부채는 1줄 아래(y 272~), 줄바꿈 통로는 y 478.
  const LAYOUTS = {
    comfort: {
      sense: [85, 220], rules: [325, 220], score: [565, 220], context: [805, 220], gate1: [1045, 220],
      judge: [85, 590], route: [325, 590], gate: [565, 590], phone: [805, 590], reason: [1045, 590],
    },
    safety: {
      sense: [85, 300], classify: [325, 300], floor: [565, 300], first_alert: [805, 300],
      phone: [1045, 300],
    },
    followup: { model: [85, 300], severity: [325, 300], followup: [565, 300], ladder: [805, 300] },
    feedback: { phone: [1045, 300], feedback: [805, 300], prefs: [565, 300], ack: [805, 300] },
  };
  /** 출구 길: 위·아래 '침묵' 길의 높이와 끝, 알림 게이트 밑 두 싱크(가운데 x). */
  // 아래 길은 y 690: 그 밑(696~760)에 이유 칩이 노드에서 멀어지는 쪽으로 자란다.
  const LANE = { top: 50, bottom: 690, left: 20, right: 1220, bottomRight: 450 };
  // 보류(끊긴 출구)는 왼쪽, 모아보기(폰으로 가는 두 번째 길)는 오른쪽 — 모아보기에서 폰으로 곧장 이어진다.
  const SINKS = { SUPPRESS: 515, DIGEST: 645 };
  const SINK = { w: 110, h: 28 };
  /** 편의 판단 노드의 출구 모양: 위 길 · 아래 길 · 점수 부채 · 알림 게이트 싱크. */
  const EXIT_OF = { rules: "top", gate1: "top", score: "fan", judge: "bottom", gate: "sinks" };
  /**
   * 한 줄 배치(안전 followup·피드백)의 출구: 노드 아래 길(y 470)로 가는 점선 화살표 하나.
   * main 은 본선으로 가는 갈림길(그 칩은 그리지 않는다 — 본선 알약이 말한다). 나머지가 출구다.
   */
  const ROW_LANE_Y = 470;
  // followup: 2단계 판정의 갈림길이지만 길은 '따라가는 알림' 노드에서 갈린다 — 보냈으면 그 노드에서 폰
  // 창으로(폰 연결선), 아니면 아래 '따라가는 알림 없음' 길로. 파급도·사다리는 어느 쪽이든 그대로 돈다.
  // 어느 갈림길이 출구인지는 서버의 exit 표시가 정한다(여기서 다시 가르지 않는다).
  const ROW_EXITS = {
    followup: { model: { from: "followup", dx: -30, x0: 20, x1: 560, list: [300, 255],
      label: "따라가는 알림 없음 · 사다리는 계속" } },
    feedback: { feedback: { dx: 30, x0: 600, x1: 1060, list: [843, 217], label: "반영되지 않음" } },
  };
  /** followup 의 폰 창 연결선은 노드 아래로 돌아 나간다(위로 돌면 앞 엣지 문구 알약을 가로지른다). */
  const ROW_PANE_Y = 430;
  /** 점수 후보 부채(점수 노드 오른쪽 아래). */
  const FAN = { x: 640, y: 282, w: 260, h: 190, noopLane: 920 };

  /** 폰 알림창으로 이어지는 노드(피드백은 폰 쪽에서 들어온다). */
  const PANE_NODE = { comfort: "phone", safety: "phone", followup: "followup", feedback: "phone" };

  /** 속도별 노드 최소 강조 시간(ms) — 화면 전용. */
  const SPEEDS = { slow: 1500, normal: 800, fast: 300 };
  /** 한 단계씩: 넘길 때 판단 노드의 세 장면(갈림길 → 간 길 → 다음 엣지) 사이 간격(ms). */
  const STEP_PHASE = 280;

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
    // 판단 노드 id → 그 노드의 출구 요소들(출구 엣지·이유 칩·싱크). mark() 가 상태를 같이 바꾼다.
    exitEls: new Map(),
    // 재생 세대: 새 실행·처음부터 다시 보기마다 1 오른다. 진행 중이던 play() 는 세대가 바뀌면 손을 뗀다.
    gen: 0,
    // 한 단계씩: step 이면 노드마다 [다음]을 기다린다(gate). advance 는 기다리지 않고 한 번 넘길 몫.
    step: false, gate: null, advance: 0,
    // 지금 보이는 자리: traces 안 사건 번호(cur)와 그 그래프의 노드 번호(ni). [이전]이 여기서 한 칸 뒤로 간다.
    cur: -1, ni: -1,
    // 대기열을 도는 재생 묶음의 세대(같은 세대는 하나만 돈다).
    looping: -1,
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
      const outX = ax + NODE_W / 2 + 30; // 윗줄 마지막 열 오른쪽 바깥
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
    return ay + 258; // 편의 1줄(220) → 478: 점수 후보 부채 아래, 2줄 문구 알약 위
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
    return b.value_ko && b.value_ko !== b.label_ko ? b.label_ko + " · " + b.value_ko : b.label_ko;
  }

  /**
   * 노드 아래 갈림길 칩 묶음: 노드 밑에서 내려오는 줄기 하나에 칩마다 가지를 달고(나무 모양),
   * 간 칩까지의 줄기·가지만 강조색으로 덧그린다. 칩은 한 줄 — 넘치는 글은 칩 제목(마우스오버)과 자막.
   * @param {Object} n
   * @param {[number, number]} at
   * @returns {SVGElement|null}
   */
  function chipsEl(n, at) {
    const shown = visibleBranches(n);
    if (!shown.length) return null;
    const [cx, cy] = at;
    // 출구처럼 차례를 탄다(markExits): 대기면 숨김 → 나타남 → 간 칩에 불. 정적 그림은 서버 상태 그대로.
    const g = svg("g", { class: "dm-chips", "data-id": n.id, "data-state": n.state });
    const list0 = D.exitEls.get(n.id) || [];
    list0.push(g);
    D.exitEls.set(n.id, list0);
    const top = cy + CHIP.top;
    const left = cx - NODE_W / 2;
    // 줄기는 노드 바닥의 곧은 부분(둥근 모서리 12 안쪽)에서 내려오고, 칩은 그 오른쪽에 붙는다.
    const spine = left + CHIP.spine;
    const chipX = left + CHIP.indent;
    const mid = (i) => top + i * CHIP.h + CHIP.h / 2;
    const from = cy + NODE_H / 2;
    const tree = svg("path", {
      d: "M" + spine + "," + from + " L" + spine + "," + mid(shown.length - 1)
        + shown.map((_, i) => " M" + spine + "," + mid(i) + " L" + chipX + "," + mid(i)).join(""),
      class: "dm-chip-tree",
    });
    g.appendChild(tree);
    const i = shown.findIndex((b) => b.taken);
    if (i >= 0) {
      g.appendChild(svg("path", {
        d: "M" + spine + "," + from + " L" + spine + "," + mid(i) + " L" + chipX + "," + mid(i),
        class: "dm-chip-link",
      }));
    }
    const fo = svg("foreignObject", {
      x: chipX, y: top, width: CHIP.w - CHIP.indent, height: CHIP.bottom - CHIP.top, class: "dm-chips-box",
    });
    const list = el("div", "dm-chip-list");
    shown.forEach((b) => {
      const chip = el("div", "dm-chip dm-branch" + (b.taken ? " dm-chip-taken dm-branch-taken" : "")
        + (b.exit ? " dm-chip-exit" : ""), chipText(b));
      chip.dataset.node = n.id;
      chip.dataset.key = b.key;
      chip.title = chipText(b);
      list.appendChild(chip);
    });
    fo.appendChild(list);
    g.appendChild(fo);
    return g;
  }

  /**
   * 노드 시간 글: ' · ' 로 나뉜 조각을 줄마다 한 줄(tspan)로 쓴다.
   * @param {Element} meta
   * @param {string} text
   */
  function setTime(meta, text) {
    clear(meta);
    const x = meta.getAttribute("x") || "0";
    text.split(" · ").forEach((part, i) => {
      const line = svg("tspan", { x: x, dy: i === 0 ? 0 : 18 });
      line.textContent = part;
      meta.appendChild(line);
    });
  }

  /**
   * 노드 하나: 둥근 사각형 + 이름 + 실제 시간(잰 단계만). 설명은 SVG 제목(마우스오버)과 자막.
   * @param {Object} n
   * @param {[number, number]} at
   * @param {boolean} [thin] 좁은 화면(칩은 아직 그리지 않는다)
   * @param {boolean} [stack] 노드 아래 칩 묶음(편의는 출구 길로 그리므로 false)
   * @returns {SVGElement}
   */
  function nodeEl(n, at, thin, stack) {
    const [cx, cy] = at;
    const g = svg("g", { class: "dm-node", "data-id": n.id, "data-state": n.state });
    const title = svg("title");
    title.textContent = n.label_ko + " — " + n.detail_ko + (n.model ? " · " + n.model : "");
    g.appendChild(title);
    g.appendChild(svg("rect", {
      x: cx - NODE_W / 2, y: cy - NODE_H / 2, width: NODE_W, height: NODE_H, rx: 12,
      class: "dm-node-box",
    }));
    const label = svg("text", { x: cx, y: cy - 10, class: "dm-node-label", "text-anchor": "middle" });
    label.textContent = n.label_ko;
    g.appendChild(label);
    const meta = svg("text", { x: cx, y: cy + 10, class: "dm-node-time", "text-anchor": "middle" });
    setTime(meta, typeof n.real_ms === "number" ? "실제 " + n.real_ms + "ms" : "");
    g.appendChild(meta);
    if (!thin && stack !== false) {
      const chips = chipsEl(n, at);
      if (chips) g.appendChild(chips);
    }
    return g;
  }

  // ── 편의 분기: 출구 길·싱크·점수 부채 ─────────────────────────────────────

  /**
   * 화살표 경로(선 + 끝 삼각형) — 엣지와 같은 모양, 다른 클래스.
   * @param {[number, number][]} pts
   * @param {string} cls
   * @returns {SVGElement[]}
   */
  function arrow(pts, cls) {
    const d = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0] + "," + p[1]).join(" ");
    const [px, py] = pts[pts.length - 2];
    const [qx, qy] = pts[pts.length - 1];
    const len = Math.hypot(qx - px, qy - py) || 1;
    const ux = (qx - px) / len;
    const uy = (qy - py) / len;
    const s = 6;
    const tri = [[qx, qy], [qx - ux * s * 1.6 - uy * s, qy - uy * s * 1.6 + ux * s],
      [qx - ux * s * 1.6 + uy * s, qy - uy * s * 1.6 - ux * s]];
    return [
      svg("path", { d: d, class: cls + "-line" }),
      svg("path", { d: "M" + tri.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" L") + " Z",
        class: cls + "-head" }),
    ];
  }

  /**
   * 출구로 그릴 갈림길: 출구(exit)와 모아보기. '기록 없음'·'반영되지 않음'은 간 경우만.
   * @param {Object} n
   * @returns {Object[]}
   */
  function exitBranches(n) {
    return visibleBranches(n).filter((b) => b.exit);
  }

  /**
   * 길 이름: 간 갈림길이 '기록 없음'이면 화면이 지어내지 않고 "기록 없음"이라 쓴다.
   * @param {Object|undefined} n
   * @param {string} label
   * @returns {string}
   */
  function laneLabel(n, label) {
    return n && (n.branches || []).some((b) => b.taken && b.key === "unknown") ? "기록 없음" : label;
  }

  /**
   * 한 줄 배치의 출구 설정(없으면 null). 지금 그리는 trace 의 종류로 찾는다.
   * @param {Object} n
   * @returns {{from?: string, dx: number, x0: number, x1: number, list?: number[], label: string}|null}
   */
  function rowExitOf(n) {
    const kind = D.shown ? layoutOf(D.shown) : "";
    const table = /** @type {Record<string, Record<string, any>>} */ (ROW_EXITS)[kind];
    return (table && table[n.id]) || null;
  }

  /**
   * 한 줄 배치의 출구: 노드 아래 길로 가는 점선 화살표 + 길 + 길 아래 이유 칩.
   * @param {Object} n
   * @param {Record<string, [number, number]>} table
   * @param {boolean} final
   * @returns {SVGElement|null}
   */
  function rowExitEl(n, table, final) {
    const cfg = rowExitOf(n);
    if (!cfg) return null;
    const [cx, cy] = table[cfg.from || n.id] || table[n.id];
    const g = exitGroup(n, final);
    const x = cx + cfg.dx;
    arrow([[x, cy + NODE_H / 2], [x, ROW_LANE_Y]], "dm-exit").forEach((p) => g.appendChild(p));
    const lane = svg("g", { class: "dm-lane" + (tookExit(n) ? " dm-lane-taken" : ""),
      "data-state": !tookExit(n) ? "done" : final ? n.state : "pending" });
    lane.appendChild(svg("path", { d: "M" + cfg.x0 + "," + ROW_LANE_Y + " L" + cfg.x1 + "," + ROW_LANE_Y,
      class: "dm-lane-line" }));
    // 길 이름은 화살표가 닿는 쪽의 반대 끝(화살표가 글을 가로지르지 않게).
    const atEnd = x < (cfg.x0 + cfg.x1) / 2;
    const label = svg("text", { x: atEnd ? cfg.x1 - 4 : cfg.x0 + 4, y: ROW_LANE_Y - 8, class: "dm-lane-label",
      "text-anchor": atEnd ? "end" : "start" });
    label.textContent = laneLabel(n, cfg.label);
    lane.appendChild(label);
    if (tookExit(n)) (D.exitEls.get(n.id) || []).push(lane);
    g.appendChild(lane);
    const branches = exitBranches(n);
    if (branches.length) {
      const [lx, lw] = cfg.list || [x + 8, Math.min(cfg.x1 - (x + 8), 320)];
      g.appendChild(reasonList(n, branches, lx, ROW_LANE_Y + 6, lw, 110));
    }
    return g;
  }

  /**
   * 이 노드에서 간 길이 출구인가(본선이면 false).
   * @param {Object} n
   * @returns {boolean}
   */
  function tookExit(n) {
    return exitBranches(n).some((b) => b.taken);
  }

  /**
   * 출구 엣지 하나를 담는 묶음(상태는 노드를 따른다).
   * @param {Object} n
   * @param {boolean} final
   * @returns {SVGElement}
   */
  function exitGroup(n, final) {
    const g = svg("g", { class: "dm-exit" + (tookExit(n) ? " dm-exit-taken" : ""), "data-id": n.id,
      "data-state": final ? n.state : "pending" });
    const list = D.exitEls.get(n.id) || [];
    list.push(g);
    D.exitEls.set(n.id, list);
    return g;
  }

  /**
   * 출구 길에 닿은 자리의 이유 칩 묶음(서버 갈림길 글 그대로, 줄바꿈).
   * @param {Object} n
   * @param {Object[]} branches
   * @param {number} x
   * @param {number} y
   * @param {number} w
   * @param {number} h
   * @returns {SVGElement}
   */
  function reasonList(n, branches, x, y, w, h) {
    const fo = svg("foreignObject", { x: x, y: y, width: w, height: h, class: "dm-reasons-box" });
    const list = el("div", "dm-reasons");
    branches.forEach((b) => {
      const chip = el("div", "dm-chip dm-branch" + (b.taken ? " dm-chip-taken dm-branch-taken" : "")
        + (b.exit ? " dm-chip-exit" : ""));
      // 이름 한 번 + 값은 둘째 줄(값이 이름과 같으면 이름만).
      chip.appendChild(el("span", "dm-chip-label", b.label_ko));
      if (b.value_ko && b.value_ko !== b.label_ko) chip.appendChild(el("span", "dm-chip-value", b.value_ko));
      chip.dataset.node = n.id;
      chip.dataset.key = b.key;
      if (b.exit) chip.dataset.exit = "1";
      chip.title = chipText(b);
      list.appendChild(chip);
    });
    fo.appendChild(list);
    return fo;
  }

  /**
   * 이유 칩 상자를 그려진 글 높이에 맞춘다(넘치거나 빈 자리가 남지 않게). 문서 밖이면 그대로 둔다.
   * @param {Element} box
   */
  function fitReasonBoxes(box) {
    for (const fo of box.querySelectorAll("foreignObject.dm-reasons-box")) {
      const list = /** @type {HTMLElement|null} */ (fo.firstElementChild);
      if (list && list.offsetHeight) fo.setAttribute("height", String(list.offsetHeight + 2));
    }
  }

  /**
   * 위·아래 '침묵' 길과 알림 게이트 밑 싱크 두 개(모아보기·보류).
   * @param {Object[]} drawn
   * @param {boolean} final
   * @returns {SVGElement}
   */
  function lanesEl(drawn, final) {
    const g = svg("g", { class: "dm-lanes" });
    const byId = new Map(drawn.map((n) => [n.id, n]));
    const lit = (ids) => ids.some((id) => byId.has(id) && tookExit(byId.get(id))
      && (final || byId.get(id).state !== "skipped"));
    const scoreNoop = byId.has("score") && (byId.get("score").branches || []).some(
      (b) => b.key === "noop" && b.taken);
    const lane = (x0, x1, y, on, ids) => {
      // 간 길만 상태를 따른다(재생 중엔 그 노드의 ② 에서 불). 안 간 길은 늘 흐리다.
      const feed = ids.length ? byId.get(ids[0]) : undefined;
      const state = !on ? "done" : final ? String((feed && feed.state) || "done") : "pending";
      const lg = svg("g", { class: "dm-lane" + (on ? " dm-lane-taken" : ""), "data-state": state });
      lg.appendChild(svg("path", { d: "M" + x0 + "," + y + " L" + x1 + "," + y, class: "dm-lane-line" }));
      // 위 길 이름은 길 위 왼쪽, 아래 길 이름은 길 위 오른쪽 끝(길 밑은 이유 칩 자리).
      const top = y === LANE.top;
      const t = svg("text", { x: top ? x0 + 4 : x1 - 4, y: y - 10, class: "dm-lane-label",
        "text-anchor": top ? "start" : "end" });
      t.textContent = laneLabel(feed, "침묵");
      lg.appendChild(t);
      ids.forEach((id) => {
        const list = D.exitEls.get(id) || [];
        list.push(lg);
        D.exitEls.set(id, list);
      });
      g.appendChild(lg);
    };
    const topOn = lit(["rules", "gate1"]) || scoreNoop;
    const topIds = ["rules", "gate1", "score"].filter((id) => byId.has(id) && (
      id === "score" ? scoreNoop : tookExit(byId.get(id))));
    lane(LANE.left, LANE.right, LANE.top, topOn, topIds);
    const judgeOn = lit(["judge"]);
    lane(LANE.left, LANE.bottomRight, LANE.bottom, judgeOn, judgeOn ? ["judge"] : []);
    return g;
  }

  /**
   * 편의 판단 노드 하나의 출구(위·아래 길로 가는 점선 화살표 + 이유 칩, 또는 게이트 싱크).
   * @param {Object} n
   * @param {[number, number]} at
   * @param {boolean} final
   * @returns {SVGElement|null}
   */
  function exitEl(n, at, final) {
    const kind = EXIT_OF[n.id];
    if (!kind || kind === "fan") return null;
    const [cx, cy] = at;
    const g = exitGroup(n, final);
    const branches = exitBranches(n);
    if (kind === "top") {
      const x = cx + 30;
      arrow([[x, cy - NODE_H / 2], [x, LANE.top]], "dm-exit").forEach((p) => g.appendChild(p));
      // 이유 칩은 길 바로 아래: 규칙은 점수·맥락 열 위까지 넓게, 1차 안전 검사는 그래프 오른쪽 끝까지.
      const w = n.id === "rules" ? 380 : LANE.right - (x + 8);
      if (branches.length) g.appendChild(reasonList(n, branches, x + 8, LANE.top + 6, w, 104));
    } else if (kind === "bottom") {
      const x = cx + 30;
      arrow([[x, cy + NODE_H / 2], [x, LANE.bottom]], "dm-exit").forEach((p) => g.appendChild(p));
      // 이유 칩은 길 밑에서 아래로(노드에서 멀어지는 쪽) — 왼쪽 끝부터 보류 싱크 앞까지 넓게 쓴다.
      if (branches.length) {
        g.appendChild(reasonList(n, branches, LANE.left, LANE.bottom + 6,
          SINKS.SUPPRESS - SINK.w / 2 - 8 - LANE.left, VIEW_H - LANE.bottom - 6));
      }
    } else {
      // 알림 게이트: 보류(출구) 싱크와 모아보기(폰으로 가는 두 번째 길) 싱크. 간 쪽만 강조한다.
      const all = visibleBranches(n);
      [["SUPPRESS", -20], ["DIGEST", 20]].forEach(([key, dx]) => {
        const b = all.find((x) => x.key === key);
        const sx = SINKS[key];
        const one = svg("g", { class: "dm-sink" + (b && b.taken ? " dm-sink-taken" : "") });
        arrow([[cx + Number(dx), cy + NODE_H / 2], [cx + Number(dx), cy + 60], [sx, cy + 60],
          [sx, LANE.bottom - SINK.h / 2]], "dm-exit").forEach((p) => one.appendChild(p));
        const fo = svg("foreignObject", { x: sx - SINK.w / 2, y: LANE.bottom - SINK.h / 2, width: SINK.w,
          height: SINK.h + 30, class: "dm-sink-box" });
        const box = el("div", "dm-sink-card");
        const label = el("div", "dm-sink-label dm-branch" + (b && b.taken ? " dm-branch-taken" : ""),
          b ? b.label_ko : String(key === "DIGEST" ? "모아보기" : "보류"));
        label.dataset.node = n.id;
        label.dataset.key = String(key);
        if (b && b.exit) label.dataset.exit = "1";
        if (!b) label.classList.remove("dm-branch"); // 서버 갈림길이 없으면 세지 않는다(건너뜀)
        box.appendChild(label);
        if (b && b.value_ko && b.value_ko !== b.label_ko) box.appendChild(el("div", "dm-sink-value", b.value_ko));
        fo.appendChild(box);
        one.appendChild(fo);
        g.appendChild(one);
      });
      const extra = branches.filter((b) => b.key !== "DIGEST" && b.key !== "SUPPRESS");
      if (extra.length) {
        g.appendChild(reasonList(n, extra, SINKS.DIGEST + SINK.w / 2 + 8, LANE.bottom - 14, 160,
          VIEW_H - LANE.bottom + 14));
      }
    }
    return g;
  }

  /**
   * 점수 후보 부채: 점수 노드 오른쪽 아래에 후보 알약, 점수 노드에서 짧은 엣지(그 점수)로 잇는다.
   * 고른 후보는 굵은 강조로 맥락까지 이어지고, '아무것도 안 하기'는 위 침묵 길로 간다.
   * @param {Object} n 점수 노드
   * @param {[number, number]} at
   * @param {[number, number]} next 맥락 노드 좌표
   * @param {Object|undefined} nextNode
   * @param {boolean} final
   * @param {Element} box
   * @returns {boolean} 부채를 그렸으면 true(맥락으로 가는 일반 엣지를 대신한다)
   */
  function fanEl(n, at, next, nextNode, final, box) {
    const shown = visibleBranches(n);
    if (!shown.length) return false;
    const num = (b) => Number.parseFloat(b.value_ko || "") || 0;
    const order = shown.slice().sort((a, b) => (num(b) - num(a)) || (Number(b.taken) - Number(a.taken)));
    const g = exitGroup(n, final);
    g.classList.add("dm-fan");
    const fo = svg("foreignObject", { x: FAN.x, y: FAN.y, width: FAN.w, height: FAN.h, class: "dm-fan-box" });
    const list = el("div", "dm-fan-list");
    const pills = order.map((b) => {
      const sel = b.taken && b.key !== "noop" && n.edge_ko;
      const text = sel ? n.edge_ko : b.label_ko;
      const pill = el("div", "dm-fan-pill dm-branch" + (b.taken ? " dm-branch-taken" : "")
        + (b.key === "noop" ? " dm-fan-noop" : "") + (sel ? " dm-pill" : ""), text);
      pill.dataset.node = n.id;
      pill.dataset.key = b.key;
      if (b.exit) pill.dataset.exit = "1";
      pill.title = b.label_ko + (b.value_ko ? " · " + b.value_ko : "");
      list.appendChild(pill);
      return pill;
    });
    fo.appendChild(list);
    g.appendChild(fo);
    box.appendChild(g); // 높이를 재려면 문서 안에 있어야 한다
    const [cx, cy] = at;
    let fallback = 0;
    const mids = pills.map((p) => {
      const h = p.offsetHeight || 26;
      const top = p.offsetHeight ? p.offsetTop : fallback;
      fallback += h + 6;
      return { top: FAN.y + top, mid: FAN.y + top + h / 2 };
    });
    const stubs = svg("g", { class: "dm-fan-stubs" });
    order.forEach((b, i) => {
      const cls = "dm-fan-stub" + (b.taken ? " dm-fan-stub-taken" : "");
      const sg = svg("g", { class: cls });
      arrow([[cx, cy + NODE_H / 2], [cx, mids[i].mid], [FAN.x, mids[i].mid]], "dm-fan").forEach(
        (p) => sg.appendChild(p));
      const score = svg("text", { x: FAN.x - 6, y: mids[i].mid - 5, class: "dm-fan-score", "text-anchor": "end" });
      score.textContent = b.value_ko || "";
      sg.appendChild(score);
      if (b.taken) stubs.appendChild(sg);
      else stubs.insertBefore(sg, stubs.firstChild); // 간 줄을 맨 위에 그린다
    });
    g.insertBefore(stubs, fo);
    // '아무것도 안 하기' → 위 침묵 길.
    const noopIdx = order.findIndex((b) => b.key === "noop");
    if (noopIdx >= 0) {
      const ng = svg("g", { class: "dm-fan-exit" + (order[noopIdx].taken ? " dm-fan-exit-taken" : "") });
      arrow([[FAN.x + FAN.w, mids[noopIdx].mid], [FAN.noopLane, mids[noopIdx].mid], [FAN.noopLane, LANE.top]],
        "dm-exit").forEach((p) => ng.appendChild(p));
      g.appendChild(ng);
    }
    // 고른 후보 → 맥락(본선).
    const selIdx = order.findIndex((b) => b.taken && b.key !== "noop");
    if (selIdx >= 0 && nextNode) {
      const go = svg("g", { class: "dm-edge dm-fan-go", "data-state": final ? nextNode.state : "pending" });
      arrow([[next[0], mids[selIdx].top], [next[0], next[1] + NODE_H / 2]], "dm-edge").forEach(
        (p) => go.appendChild(p));
      box.appendChild(go);
      D.edgeEls.set(nextNode.id, go);
    }
    return true;
  }

  /**
   * 자막 한 줄: 노드 이름 — 설명 (· 간 갈림길) (→ 엣지 문구). 모두 서버 글 그대로.
   * @param {Object} n
   * @returns {string}
   */
  function captionOf(n) {
    const taken = (n.branches || []).find((b) => b.taken);
    let line = n.label_ko + " — " + n.detail_ko;
    // 설명이 간 갈림길 이름으로 시작하면(예: '반영되지 않음(403)') 같은 말을 되풀이하지 않고 갈림길 글로 쓴다.
    if (taken && taken.value_ko && n.detail_ko.startsWith(taken.label_ko)) line = n.label_ko + " — " + chipText(taken);
    else if (taken && taken.value_ko) line += " · " + chipText(taken);
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
    D.exitEls = new Map();
    if (thin) {
      renderNarrow(t, Boolean(final), box, drawn, kind);
      finishRender(t, final, box, drawn, true);
      return;
    }
    /** @type {Record<string, [number, number]>} */
    const table = wide;
    box.setAttribute("viewBox", "0 0 " + VIEW_W + " " + VIEW_H);
    box.classList.remove("dm-svg-thin");
    const edges = svg("g", { class: "dm-edges" });
    const nodes = svg("g", { class: "dm-nodes" });
    D.exitEls = new Map();
    const branching = kind === "comfort" && !thin;
    const scoreNode = drawn.find((n) => n.id === "score");
    const fan = branching && scoreNode !== undefined && visibleBranches(scoreNode).length > 0;
    const exits = svg("g", { class: "dm-exits" });
    if (branching) {
      drawn.forEach((n) => {
        const x = exitEl(n, table[n.id], Boolean(final));
        if (x) exits.appendChild(x);
      });
      if (scoreNode && !fan) {
        // 점수를 건너뛴 침묵: 후보는 없지만 출구 자리는 그린다.
        const [sx, sy] = table.score;
        const g = exitGroup(scoreNode, Boolean(final));
        arrow([[sx + 30, sy - NODE_H / 2], [sx + 30, LANE.top]], "dm-exit").forEach((p) => g.appendChild(p));
        exits.appendChild(g);
      }
      box.appendChild(lanesEl(drawn, Boolean(final)));
    }
    const rowBranching = !thin && Boolean(/** @type {Record<string, any>} */ (ROW_EXITS)[kind]);
    if (rowBranching) {
      drawn.forEach((n) => {
        const x = rowExitEl(n, table, Boolean(final));
        if (x) exits.appendChild(x);
      });
    }
    for (let i = 0; i < drawn.length; i += 1) {
      const n = drawn[i];
      const g = nodeEl(n, table[n.id], thin, !branching && !(rowBranching && rowExitOf(n)));
      if (!final) {
        g.setAttribute("data-state", "pending");
        const meta = g.querySelector(".dm-node-time");
        if (meta) setTime(meta, "");
        const chips = g.querySelector(".dm-chips");
        if (chips) chips.setAttribute("data-state", "pending");
      }
      D.nodeEls.set(n.id, g);
      nodes.appendChild(g);
      if (i > 0 && !(fan && n.id === "context")) {
        const prev = drawn[i - 1];
        const digest = branching && prev.id === "gate" && (prev.branches || []).some(
          (b) => b.key === "DIGEST" && b.taken);
        if (digest) {
          // 모아보기: 게이트 → 모아보기 싱크 → 폰. 곧장 가는 선은 꺼 두고(문구 없이), 싱크에서 폰으로
          // 올라가는 길이 그 노드로 들어오는 엣지다(문구 = 서버 게이트 edge_ko).
          const direct = edge(route(table[prev.id], table[n.id]), "skipped", null);
          direct.classList.add("dm-edge-off");
          edges.appendChild(direct);
          const [px, py] = table[n.id];
          const via = edge([[SINKS.DIGEST + SINK.w / 2, LANE.bottom], [px, LANE.bottom], [px, py + NODE_H / 2]],
            final ? n.state : "pending", null);
          via.classList.add("dm-edge-via");
          if (prev.edge_ko) via.appendChild(pillEl(prev.edge_ko, px + 95, LANE.bottom - 20));
          D.edgeEls.set(n.id, via);
          edges.appendChild(via);
          continue;
        }
        const e = edge(route(table[prev.id], table[n.id]), final ? n.state : "pending",
          thin ? null : prev.edge_ko);
        // 출구로 간 판단 노드 다음의 본선, 바로 알림이 아닌 알림 게이트 다음의 본선은 불을 켜지 않는다.
        const prevRow = rowBranching ? rowExitOf(prev) : null;
        if (((branching || (prevRow && !prevRow.from)) && tookExit(prev)) || (branching && prev.id === "gate"
          && !(prev.branches || []).some((b) => b.key === "IMMEDIATE" && b.taken))) {
          e.classList.add("dm-edge-off");
        }
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
        kind === "followup" ? [[x, y + NODE_H / 2], [x, ROW_PANE_Y], [PANE.x, ROW_PANE_Y]]
          : blocked ? [[x, y - NODE_H / 2], [x, y - NODE_H / 2 - 26], [PANE.x, y - NODE_H / 2 - 26]]
            : [[x + NODE_W / 2, y], [PANE.x, y]]);
      const link = edge(t.kind === "feedback" ? pts.slice().reverse() : pts,
        final ? target.state : "pending");
      link.classList.add("dm-edge-pane");
      // followup: 2단계 판정이 따라가는 알림을 보내지 않았으면 폰 연결선은 불을 켜지 않는다.
      const model = kind === "followup" ? drawn.find((n) => n.id === "model") : undefined;
      if (model && tookExit(model)) link.classList.add("dm-edge-off");
      D.edgeEls.set("__pane__", link);
      edges.appendChild(link);
    }
    D.paneNode = target ? target.id : null;
    box.appendChild(exits);
    box.appendChild(edges);
    box.appendChild(nodes);
    if (fan && scoreNode) {
      const ctx = drawn.find((n) => n.id === "context");
      fanEl(scoreNode, table.score, table.context, ctx, Boolean(final), box);
    }
    fitReasonBoxes(box);
    finishRender(t, final, box, drawn, thin);
  }

  /**
   * 그리기 마무리: 폰 창 자리, 결과 한 줄, 대기 문구, 정적 그림의 자막.
   * @param {Object} t
   * @param {boolean|undefined} final
   * @param {Element} box
   * @param {Object[]} drawn
   * @param {boolean} thin
   */
  function finishRender(t, final, box, drawn, thin) {
    placePane(box, thin);
    $("demoOutcome").textContent = t.outcome_ko || "";
    $("demoWaiting").hidden = true;
    if (final) {
      const now = drawn.find((n) => n.state === "end") || drawn[drawn.length - 1];
      $("demoCaption").textContent = now ? captionOf(now) : "";
    }
  }

  /**
   * 좁은 화면에서 그릴 갈림길 칩: 넓은 화면의 출구·후보 부채·노드 아래 칩과 같은 집합.
   * @param {Object} n
   * @param {string} kind
   * @returns {Object[]}
   */
  function narrowChips(n, kind) {
    if (kind === "comfort") {
      if (!EXIT_OF[n.id]) return [];
      return n.id === "score" ? visibleBranches(n) : exitBranches(n);
    }
    // followup 의 '보냄'은 넓은 화면에선 폰 연결선이 말하지만 좁은 화면엔 그 선이 없다 — 칩으로 모두 보인다.
    const row = rowExitOf(n);
    return row && !row.from ? exitBranches(n) : visibleBranches(n);
  }

  /**
   * 그린 HTML 상자의 높이(문서 밖·숨김이면 글자 수로 어림한다).
   * @param {HTMLElement} node
   * @param {number} guess
   * @returns {number}
   */
  function heightOf(node, guess) {
    return node.offsetHeight || guess;
  }

  /**
   * 좁은 화면: 위에서 아래로 한 줄. 노드 오른쪽에 그 노드의 갈림길 칩(간 칩만 강조), 줄 사이에
   * 엣지 문구 알약. 칩·알약은 그려서 잰 높이로 foreignObject 를 맞추고, 다음 줄을 그만큼 내린다.
   * @param {Object} t
   * @param {boolean} final
   * @param {Element} box
   * @param {Object[]} drawn
   * @param {string} kind
   */
  function renderNarrow(t, final, box, drawn, kind) {
    box.setAttribute("viewBox", "0 0 " + NARROW.w + " 4000"); // 재는 동안 넉넉히
    box.classList.add("dm-svg-thin");
    const edges = svg("g", { class: "dm-edges" });
    const exits = svg("g", { class: "dm-exits" });
    const nodes = svg("g", { class: "dm-nodes" });
    box.appendChild(exits);
    box.appendChild(edges);
    box.appendChild(nodes);
    /** @type {Record<string, [number, number]>} */
    const at = {};
    /** @type {Array<Element|null>} */
    const pills = [];
    let y = NARROW.top;
    drawn.forEach((n, i) => {
      const cy = y + NODE_H / 2;
      at[n.id] = [NARROW.x, cy];
      const g = nodeEl(n, at[n.id], true, false);
      if (!final) {
        g.setAttribute("data-state", "pending");
        const meta = g.querySelector(".dm-node-time");
        if (meta) setTime(meta, "");
      }
      D.nodeEls.set(n.id, g);
      nodes.appendChild(g);
      let bottom = y + NODE_H;
      const chips = narrowChips(n, kind);
      if (chips.length) {
        const eg = exitGroup(n, final);
        const fo = reasonList(n, chips, NARROW.chipX, y, NARROW.chipW, 1000);
        fo.classList.add("dm-reasons-narrow");
        eg.appendChild(fo);
        exits.appendChild(eg);
        const h = heightOf(/** @type {HTMLElement} */ (fo.firstChild), chips.length * 24 + 24);
        fo.setAttribute("height", String(h));
        bottom = Math.max(bottom, y + h);
      }
      y = bottom + NARROW.gap;
      const next = drawn[i + 1];
      if (next && n.edge_ko && n.state !== "skipped") {
        const fo = svg("foreignObject", { x: NARROW.pillX, y: y, width: NARROW.pillW, height: 1000,
          class: "dm-pill-box" });
        const wrap = el("div", "dm-pill-wrap dm-pill-wrap-narrow");
        const pill = el("span", "dm-pill", n.edge_ko);
        wrap.appendChild(pill);
        fo.appendChild(wrap);
        edges.appendChild(fo); // 재기 위해 잠시 붙인다 — 아래에서 엣지 묶음으로 옮긴다
        const lines = Math.ceil((String(n.edge_ko).length * 15) / (NARROW.pillW - 16));
        const h = heightOf(wrap, lines * 20 + 8);
        fo.setAttribute("height", String(h));
        pills.push(fo);
        y += h + NARROW.gap;
      } else {
        pills.push(null);
      }
    });
    for (let i = 1; i < drawn.length; i += 1) {
      const prev = drawn[i - 1];
      const n = drawn[i];
      const e = edge(route(at[prev.id], at[n.id]), final ? n.state : "pending", null);
      const pill = pills[i - 1];
      if (pill) e.appendChild(pill);
      // 모아보기는 폰으로 가는 길이라 본선을 켜 둔다(문구 = 밤 리뷰). 보류는 출구라 폰이 건너뜀이다.
      const row = rowExitOf(prev);
      if ((kind === "comfort" || (row && !row.from)) && tookExit(prev)) {
        e.classList.add("dm-edge-off");
      }
      D.edgeEls.set(n.id, e);
      edges.appendChild(e);
    }
    D.paneNode = null;
    box.setAttribute("viewBox", "0 0 " + NARROW.w + " " + Math.ceil(y + 6));
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
    if (D.step) return real === null ? "" : "실제 " + fmtMs(real); // 넘기는 동안은 시연 시간이 없다
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
    note(id, state);
  }

  /**
   * 바뀐 때를 남긴다(시험·확인용: 판단 순서를 eval 로 잰다).
   * @param {string} id 노드 id 또는 'exits:<id>'·'edge:<id>'
   * @param {string} state
   */
  function note(id, state) {
    D.marks.push({ trace: D.shown ? D.shown.trace_id : null, id: id, state: state, at: performance.now() });
    if (D.marks.length > 2000) D.marks.splice(0, 1000);
  }

  /**
   * 노드로 들어오는 엣지(그 문구 알약 포함)의 상태.
   * @param {string} id 들어오는 쪽 노드 id
   * @param {string} state
   */
  function markEdge(id, state) {
    const e = D.edgeEls && D.edgeEls.get(id);
    if (!e) return;
    e.setAttribute("data-state", state);
    note("edge:" + id, state);
  }

  /**
   * 판단 노드의 출구(출구 엣지·이유 칩·싱크·후보 부채·간 길)의 상태.
   * pending = 아직 숨김, shown = 보이되 불 꺼짐, done/end = 간 길에 불, skipped = 흐리게.
   * @param {string} id
   * @param {string} state
   */
  function markExits(id, state) {
    const list = (D.exitEls && D.exitEls.get(id)) || [];
    if (!list.length) return;
    for (const x of list) x.setAttribute("data-state", state);
    note("exits:" + id, state);
  }

  /**
   * 노드 하나를 기다림 없이 끝난 모습으로 둔다([이전]으로 되돌아간 자리 앞의 노드·건너뛴 노드).
   * 폰 알림창에 생기는 일(onEnter·onLeave)도 차례대로 한다.
   * @param {Object} t
   * @param {Object} n
   * @param {Object|undefined} next
   */
  function settle(t, n, next) {
    const link = n.id === D.paneNode ? D.edgeEls.get("__pane__") : null;
    if (n.state === "skipped") {
      mark(n.id, "skipped");
      markEdge(n.id, "skipped");
      markExits(n.id, "skipped");
      if (link) link.setAttribute("data-state", "skipped");
      return;
    }
    const meta = D.nodeEls.get(n.id).querySelector(".dm-node-time");
    if (meta) setTime(meta, timeLabel(n));
    onEnter(t, n);
    const fin = n.state === "end" ? "end" : "done";
    markExits(n.id, fin);
    if (next && next.state !== "skipped") markEdge(next.id, next.state === "end" ? "end" : "done");
    mark(n.id, fin);
    if (link) link.setAttribute("data-state", "done");
    onLeave(t, n);
  }

  /**
   * 한 단계씩일 때 [다음]을 기다린다. 건너뛰기·넘길 몫이 있으면 바로 지나간다.
   * @returns {Promise<void>}
   */
  function gate() {
    if (D.skip) return Promise.resolve();
    if (D.advance > 0) {
      D.advance -= 1;
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      D.gate = () => {
        D.gate = null;
        resolve();
      };
      showWaiting();
    });
  }

  /**
   * trace 하나를 재생한다. 노드마다 강조 시간 = max(실제, 속도의 최소)를 셋으로 나눠
   * ① 출구 엣지·후보 부채가 나타나고 ② 간 갈림길에 불이 들어오고 ③ 다음 본선 엣지와 그 문구 알약에
   * 불이 들어온다. 동작 줄이기면 셋을 한 번에 바꾼다. 마지막 장면은 간 길·알약을 그대로 남긴다.
   * 한 단계씩이면 세 장면을 짧게 보이고 ③ 뒤에서 [다음]을 기다린다.
   * @param {Object} t
   * @param {number} [startAt] 이 번호 앞의 노드는 끝난 모습으로 바로 둔다([이전]으로 온 자리)
   */
  async function play(t, startAt) {
    const gen = D.gen;
    D.skip = false;
    enterDay(t);
    render(t, false);
    const calm = reducedMotion();
    $("demoSvg").classList.toggle("dm-anim", !calm);
    const drawn = D.drawn;
    for (let i = 0; i < drawn.length; i += 1) {
      const n = drawn[i];
      const next = drawn[i + 1];
      if (n.state === "skipped" || i < (startAt || 0)) {
        settle(t, n, next);
        continue;
      }
      D.ni = i;
      mark(n.id, "current");
      showWaiting();
      follow(D.nodeEls.get(n.id));
      const meta = D.nodeEls.get(n.id).querySelector(".dm-node-time");
      if (meta) setTime(meta, timeLabel(n));
      $("demoCaption").textContent = captionOf(n);
      onEnter(t, n);
      if (n.id === D.paneNode && D.edgeEls.get("__pane__")) {
        D.edgeEls.get("__pane__").setAttribute("data-state", "current");
      }
      const third = () => (D.step ? STEP_PHASE : holdOf(n) / 3);
      markExits(n.id, "shown");                                   // ① 갈 수 있던 길이 나타난다
      if (!calm) await wait(third());
      if (gen !== D.gen) return; // 새 실행·다시 보기가 시작됐다 — 낡은 카드를 새 창에 넣지 않는다
      markExits(n.id, n.state === "end" ? "end" : "done");        // ② 간 길에 불
      if (!calm) await wait(third());
      if (gen !== D.gen) return;
      if (next && next.state !== "skipped") markEdge(next.id, next.state === "end" ? "end" : "done"); // ③ 다음 본선 + 알약
      if (D.step) await gate();
      else await wait(calm ? holdOf(n) : third());
      if (gen !== D.gen) return;
      mark(n.id, n.state === "end" ? "end" : "done");
      if (n.id === D.paneNode && D.edgeEls.get("__pane__")) {
        D.edgeEls.get("__pane__").setAttribute("data-state", "done");
      }
      onLeave(t, n);
    }
    if (gen !== D.gen) return;
    if (!D.step) await wait(SPEEDS[D.speed] / 2);
  }

  /**
   * 대기열을 도착 순서대로 재생한다(덮개가 열려 있는 동안). 같은 세대에서는 하나만 돈다.
   * @param {Object} [first] 대기열보다 먼저 재생할 trace([이전]으로 되돌아간 사건)
   * @param {number} [startAt] first 를 이 노드부터 재생한다
   */
  async function loop(first, startAt) {
    const gen = D.gen;
    if (D.looping === gen) return;
    D.looping = gen;
    D.playing = true;
    try {
      let t = first || null;
      while (D.open && gen === D.gen) {
        if (!t) {
          if (!D.queue.length) break;
          t = D.queue.shift();
        }
        D.cur = D.traces.indexOf(t);
        D.ni = -1; // 앞 사건의 노드 번호를 새 사건에 들고 가지 않는다(노드 수가 다르다)
        showWaiting();
        await play(t, t === first ? startAt : 0);
        if (gen !== D.gen) return;
        D.played += 1;
        t = null;
      }
    } finally {
      if (D.looping === gen) {
        D.looping = -1;
        D.playing = false;
        showWaiting();
      }
    }
  }

  /**
   * trace 가 그리는 노드(좌표표에 있는 것만 — render 와 같은 집합).
   * @param {Object} t
   * @returns {Object[]}
   */
  function drawnOf(t) {
    const table = LAYOUTS[layoutOf(t)] || {};
    return (t.nodes || []).filter((n) => table[n.id]);
  }

  /**
   * 지금 자리의 한 칸 앞(건너뛴 노드는 세지 않는다). 앞 사건으로 넘어가면 그 사건의 마지막 노드.
   * @returns {[number, number]|null}
   */
  function prevPos() {
    let ti = D.cur;
    let ni = D.ni - 1;
    while (ti >= 0 && ti < D.traces.length) {
      const list = drawnOf(D.traces[ti]);
      ni = ni === null ? list.length - 1 : Math.min(ni, list.length - 1);
      for (; ni >= 0; ni -= 1) if (list[ni].state !== "skipped") return [ti, ni];
      ti -= 1;
      ni = null;
    }
    return null;
  }

  /**
   * 한 단계씩 / 자동 속도를 고른다(세그먼트와 [이전]·[다음]이 같이 부른다).
   * @param {string|null} v slow·normal·fast·step
   */
  function setMode(v) {
    const seg = $("demoSpeed");
    const mode = v === "slow" || v === "fast" || v === "step" ? v : "normal";
    for (const o of seg.querySelectorAll("button")) {
      o.setAttribute("aria-checked", o.getAttribute("data-speed") === mode ? "true" : "false");
    }
    D.step = mode === "step";
    if (!D.step) {
      D.speed = /** @type {"slow"|"normal"|"fast"} */ (mode);
      D.advance = 0;
      if (D.gate) D.gate(); // 기다리던 자리에서 자동으로 이어 간다
    }
    showWaiting();
  }

  /** [다음 ›]: 한 단계씩으로 바꾸고 한 노드 앞으로. 재생이 끝났으면 기다리는 사건부터. */
  function stepNext() {
    if (!D.step) setMode("step");
    if (D.gate) {
      D.gate();
      return;
    }
    if (D.playing) {
      // 아직 세 장면을 보이는 중: 이 노드를 바로 마치고 다음 노드에서 멈춘다.
      D.advance = 1;
      if (D.wake) D.wake();
      return;
    }
    if (D.queue.length && D.open) loop();
  }

  /** [‹ 이전]: 한 단계씩으로 바꾸고 한 노드 뒤로(앞 사건까지). 알림창도 그 자리까지 다시 쌓는다. */
  function stepPrev() {
    const at = prevPos();
    if (!at) return;
    if (!D.step) setMode("step");
    const [ti, ni] = at;
    D.gen += 1;
    D.skip = true;
    if (D.wake) D.wake();
    if (D.gate) D.gate();
    D.advance = 0;
    D.cards = new Map();
    D.feedDay = null;
    if (D.feed) clear(D.feed);
    for (let k = 0; k < ti; k += 1) {
      const t = D.traces[k];
      enterDay(t);
      drawnOf(t).forEach((n) => {
        if (n.state === "skipped") return;
        onEnter(t, n);
        onLeave(t, n);
      });
    }
    D.queue = D.traces.slice(ti + 1);
    loop(D.traces[ti], ni);
  }

  /** '대기 n건'(지금 재생 중인 것은 빼고). */
  function showWaiting() {
    $("demoQueue").textContent = "대기 " + D.queue.length + "건";
    /** @type {HTMLButtonElement} */ ($("demoSkip")).disabled = !D.playing;
    /** @type {HTMLButtonElement} */ ($("demoReplay")).disabled = !D.traces.length;
    /** @type {HTMLButtonElement} */ ($("demoPrev")).disabled = !prevPos();
    /** @type {HTMLButtonElement} */ ($("demoNext")).disabled = !(D.playing || D.queue.length);
    const t = D.cur >= 0 ? D.traces[D.cur] : null;
    $("demoStep").textContent = t && D.ni >= 0
      ? "사건 " + (D.cur + 1) + "/" + D.traces.length + " · 단계 " + (D.ni + 1) + "/" + drawnOf(t).length : "";
  }

  /** 재생 중인 것을 멈추고(세대 올림) 알림창·그래프를 비운다. 받아 둔 trace 목록은 부르는 쪽이 정한다. */
  function restart() {
    D.gen += 1;
    D.queue = [];
    D.shown = null;
    D.cards = new Map();
    D.feedDay = null;
    D.played = 0;
    D.cur = -1;
    D.ni = -1;
    D.advance = 0;
    D.skip = true;
    if (D.wake) D.wake();
    if (D.gate) D.gate();
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
    if (D.gate) D.gate();
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
      b.addEventListener("click", () => setMode(b.getAttribute("data-speed")));
    }
  }

  function init() {
    $("demoClose").addEventListener("click", close);
    $("demoSkip").addEventListener("click", skip);
    $("demoReplay").addEventListener("click", replay);
    $("demoPrev").addEventListener("click", stepPrev);
    $("demoNext").addEventListener("click", stepNext);
    setupSpeed();
    document.addEventListener("keydown", (e) => {
      if (!D.open) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        if (e.key === "ArrowRight") stepNext();
        else stepPrev();
      }
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

  /**
   * 겹침 검사(시험·확인용): 지금 그린 그래프에서 노드·칩·문구 알약·후보 알약·싱크·길 이름·점수 글·
   * 폰 창의 경계 상자가 서로 교차하는 쌍, viewBox 밖으로 나간 요소, 자기 foreignObject 밖으로 넘친
   * (잘린) 글 상자를 센다. 엣지 선은 뺀다.
   * @returns {{pairs: number, outside: number, clipped: number, details: Array<Array<string|number>>}}
   */
  function overlapCheck() {
    const box = /** @type {SVGSVGElement} */ (/** @type {unknown} */ ($("demoSvg")));
    const ctm = box.getScreenCTM();
    const vb = box.viewBox.baseVal;
    const pt = (x, y) => {
      const q = box.createSVGPoint();
      q.x = x;
      q.y = y;
      return ctm ? q.matrixTransform(ctm) : q;
    };
    const a0 = pt(vb.x, vb.y);
    const a1 = pt(vb.x + vb.width, vb.y + vb.height);
    const kinds = [["node", ".dm-node-box"], ["chip", ".dm-chip"], ["pill", ".dm-pill:not(.dm-fan-pill)"],
      ["fan", ".dm-fan-pill"], ["sink", ".dm-sink-card"], ["lane", ".dm-lane-label"],
      ["score", ".dm-fan-score"], ["time", ".dm-node-time"], ["pane", ".dm-pane"]];
    /** @type {Array<[string, DOMRect, Element]>} */
    const items = [];
    for (const [name, sel] of kinds) {
      for (const node of box.querySelectorAll(sel)) {
        const r = node.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) items.push([name, r, node]);
      }
    }
    const details = [];
    let pairs = 0;
    let outside = 0;
    let clipped = 0;
    const tol = 0.5;
    for (const [name, r, node] of items) {
      const fo = node.closest("foreignObject");
      if (!fo || name === "pane") continue;
      const f = fo.getBoundingClientRect();
      if (r.left < f.left - tol || r.right > f.right + tol || r.top < f.top - tol || r.bottom > f.bottom + tol) {
        clipped += 1;
        details.push(["clipped", name, Math.round(r.left), Math.round(r.top)]);
      }
    }
    for (let i = 0; i < items.length; i += 1) {
      const [na, ra, ea] = items[i];
      if (ra.left < a0.x - tol || ra.top < a0.y - tol || ra.right > a1.x + tol || ra.bottom > a1.y + tol) {
        outside += 1;
        details.push(["outside", na, Math.round(ra.left), Math.round(ra.top)]);
      }
      for (let j = i + 1; j < items.length; j += 1) {
        const [nb, rb, eb] = items[j];
        // 노드 상자와 그 안의 시간 글은 한 몸이다.
        if ((na === "time" || nb === "time") && (ea.closest(".dm-node") === eb.closest(".dm-node"))) continue;
        if (ra.left < rb.right - tol && rb.left < ra.right - tol && ra.top < rb.bottom - tol
          && rb.top < ra.bottom - tol) {
          pairs += 1;
          details.push([na, nb, Math.round(ra.left), Math.round(ra.top), Math.round(rb.left), Math.round(rb.top)]);
        }
      }
    }
    return { pairs: pairs, outside: outside, clipped: clipped, details: details.slice(0, 20) };
  }

  /** @type {any} */ (window).LiotDemo = { open: open, close: close, push: push, reset: reset, render: render,
    skip: skip, replay: replay, next: stepNext, prev: stepPrev, mode: setMode, captionOf: captionOf, overlapCheck: overlapCheck, state: D };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
