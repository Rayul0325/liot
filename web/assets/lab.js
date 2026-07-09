/* LIOT 랩 — 제어판 값을 실제 파이프라인(/api/simulate)에 태우고 트레이스를 그린다.
   여기서 판단하지 않는다. 서버의 진짜 LiotApp + 로컬 LLM 응답만 렌더한다. */
(function () {
  "use strict";

  var $ = function (id) {
    return document.getElementById(id);
  };
  var reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;

  /**
   * HTML 특수문자를 이스케이프한다(innerHTML 삽입 전 XSS 방지).
   * @param {*} s 임의 값
   * @returns {string} 이스케이프된 문자열
   */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---------- 세그먼트 컨트롤(시간대·조건) ---------- */
  /**
   * 세그먼트 버튼 그룹을 배선하고, 현재 선택값을 읽는 게터를 돌려준다.
   * @param {string} id 컨테이너 요소 id
   * @returns {() => (string|null)} 선택된 버튼의 data-v 를 반환하는 함수
   */
  function seg(id) {
    var box = $(id);
    box.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      box.querySelectorAll("button").forEach(function (x) {
        x.classList.remove("on");
      });
      b.classList.add("on");
    });
    return function () {
      var on = box.querySelector("button.on");
      return on ? on.getAttribute("data-v") : null;
    };
  }
  var getTime = seg("time");
  var getCond = seg("cond");

  /* ---------- 슬라이더 값 표시 ---------- */
  /**
   * range 입력의 값을 표시 요소에 실시간 반영한다.
   * @param {string} id 입력 요소 id
   * @param {string} vid 값 표시 요소 id
   * @param {(v: string) => string} fmt 표시 포맷터
   */
  function bind(id, vid, fmt) {
    var el = $(id),
      out = $(vid);
    var upd = function () {
      out.textContent = fmt(el.value);
    };
    el.addEventListener("input", upd);
    upd();
  }
  bind("s_temp", "s_temp_v", function (v) {
    return v + "°C";
  });
  bind("s_hum", "s_hum_v", function (v) {
    return v + "%";
  });
  bind("ext_temp", "ext_temp_v", function (v) {
    return v + "°C";
  });
  bind("ext_aqi", "ext_aqi_v", function (v) {
    return v;
  });

  /* ---------- 외기 토글 → 슬라이더 표시 ---------- */
  $("ext_on").addEventListener("change", function () {
    var on = $("ext_on").checked;
    $("ext_temp_row").hidden = !on;
    $("ext_aqi_row").hidden = !on;
  });

  /* ---------- 프리셋 ---------- */
  var PRESETS = {
    vacant: {
      motion: false,
      light: true,
      gas: false,
      smoke: false,
      leak: false,
      temp: 22,
      time: "evening",
    },
    gas: {
      motion: true,
      light: true,
      gas: true,
      smoke: false,
      leak: false,
      temp: 21,
      time: "night",
    },
    cold: {
      motion: true,
      light: true,
      gas: false,
      smoke: false,
      leak: false,
      temp: 16,
      time: "evening",
      ext: true,
      extTemp: 1,
    },
    occupied: {
      motion: true,
      light: true,
      gas: false,
      smoke: false,
      leak: false,
      temp: 23,
      time: "day",
    },
  };
  /**
   * 세그먼트 그룹의 선택 상태를 프로그램적으로 지정한다(프리셋 적용용).
   * @param {string} id 컨테이너 id
   * @param {string} v 선택할 data-v 값
   */
  function setSeg(id, v) {
    var box = $(id);
    box.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-v") === v);
    });
  }
  $("presets").addEventListener("click", function (e) {
    var b = e.target.closest(".preset");
    if (!b) return;
    var p = PRESETS[b.getAttribute("data-preset")];
    if (!p) return;
    $("s_motion").checked = p.motion;
    $("s_light").checked = p.light;
    $("s_gas").checked = p.gas;
    $("s_smoke").checked = p.smoke;
    $("s_leak").checked = p.leak;
    $("s_temp").value = p.temp;
    $("s_temp").dispatchEvent(new Event("input"));
    setSeg("time", p.time);
    if (p.ext) {
      $("ext_on").checked = true;
      $("ext_temp").value = p.extTemp;
    } else {
      $("ext_on").checked = false;
    }
    $("ext_on").dispatchEvent(new Event("change"));
    $("ext_temp").dispatchEvent(new Event("input"));
    $("presets")
      .querySelectorAll(".preset")
      .forEach(function (x) {
        x.classList.remove("on");
      });
    b.classList.add("on");
  });

  /* ---------- 요청 본문 ---------- */
  /**
   * 현재 제어판 상태를 /api/simulate 요청 본문으로 직렬화한다.
   * @param {"tick"|"event"} mode 실행 모드
   * @param {{entity: string, value: *, action?: string}} [event] 이벤트 모드일 때 발생 이벤트
   * @returns {object} 요청 본문(sensors/config/outdoor/event)
   */
  function readBody(mode, event) {
    var body = {
      mode: mode,
      sensors: {
        "binary_sensor.living_motion": $("s_motion").checked,
        "light.living_room": $("s_light").checked,
        "binary_sensor.kitchen_gas": $("s_gas").checked,
        "binary_sensor.hall_smoke": $("s_smoke").checked,
        "binary_sensor.bath_leak": $("s_leak").checked,
        "sensor.living_temperature": parseFloat($("s_temp").value),
        "sensor.living_humidity": parseFloat($("s_hum").value),
      },
      config: {
        time: getTime(),
        condition: getCond(),
        llm_enabled: $("llm_on").checked,
        llm_model: "gemma3:4b",
        execution_enabled: $("exec_on").checked,
      },
    };
    if ($("ext_on").checked) {
      body.outdoor = {
        temp_c: parseFloat($("ext_temp").value),
        aqi: parseInt($("ext_aqi").value, 10),
      };
    }
    if (event) body.event = event;
    return body;
  }

  /* ---------- 렌더 조각 ---------- */
  var TIER = { ambient: "잔잔", breath: "숨결", dusk: "노을", ember: "불씨" };

  /**
   * 위험도 tier 를 색 있는 pill HTML 로 렌더한다.
   * @param {{name: string, ko: string, glyph: string}} t tier 뷰
   * @returns {string} pill HTML
   */
  function tierPill(t) {
    if (!t) return "";
    return (
      '<span class="tierpill is-' +
      esc(t.name) +
      '">' +
      esc(t.glyph) +
      " " +
      esc(t.ko) +
      " · " +
      esc(t.name.toUpperCase()) +
      "</span>"
    );
  }

  /**
   * 파이프라인 단계 블록 HTML 을 만든다.
   * @param {string} layer 레이어 라벨
   * @param {string} title 단계 제목
   * @param {string} inner 본문 HTML
   * @param {string} [cls] 추가 클래스
   * @returns {string} 단계 HTML
   */
  function stage(layer, title, inner, cls) {
    return (
      '<div class="tstage ' +
      (cls || "") +
      '">' +
      '<div class="tstage-head"><span class="tstage-layer">' +
      esc(layer) +
      "</span>" +
      "<h4>" +
      esc(title) +
      "</h4></div>" +
      '<div class="tstage-body">' +
      inner +
      "</div></div>"
    );
  }

  /**
   * 작은 모노스페이스 칩 HTML.
   * @param {string} txt 텍스트
   * @param {string} [cls] 톤 클래스(ok/warn/danger/muted/llm)
   * @returns {string} 칩 HTML
   */
  function chip(txt, cls) {
    return '<span class="tchip ' + (cls || "") + '">' + esc(txt) + "</span>";
  }

  /**
   * 후보 점수판(각 후보의 final_score 막대 + 세부 지표)을 렌더한다.
   * @param {Array<object>} cands 서버가 돌려준 후보 배열
   * @returns {string} 점수판 HTML
   */
  function candBoard(cands) {
    if (!cands || !cands.length) return "";
    var rows = cands
      .map(function (c) {
        var w = Math.max(0, Math.min(1, c.final_score)) * 100;
        var name = c.is_noop
          ? "아무것도 하지 않기"
          : c.capability + "." + c.action;
        var cls = c.is_selected ? "is-picked" : c.is_noop ? "is-noop" : "";
        return (
          '<div class="cand-row ' +
          cls +
          '">' +
          '<div class="cand-head"><span class="cand-name">' +
          esc(name) +
          "</span>" +
          '<span class="cand-origin">' +
          esc(c.origin || "") +
          "</span></div>" +
          '<div class="cand-track"><i class="cand-fill" style="width:' +
          w.toFixed(1) +
          '%"></i>' +
          '<b class="cand-score">' +
          c.final_score.toFixed(2) +
          "</b></div>" +
          '<div class="cand-mini">효과 ' +
          c.expected_effect.toFixed(2) +
          " · 선호 " +
          c.user_preference_fit.toFixed(2) +
          " · 에너지 " +
          c.energy_cost.toFixed(2) +
          " · 안전위험 " +
          c.safety_risk.toFixed(2) +
          "</div></div>"
        );
      })
      .join("");
    return '<div class="cand-wrap">' + rows + "</div>";
  }

  /**
   * 최종 제안(알림) 카드를 렌더한다.
   * @param {object} p proposal(tier/action/entity/rationale/required_confirmation…)
   * @param {object} iv intervention(condition/execution_decision…)
   * @returns {string} 알림 카드 HTML
   */
  function notiCard(p, iv) {
    var t = p.tier;
    var conf = {
      none: "확인 없이 알림",
      light: "가벼운 터치 확인",
      explicit: "명시적 확인",
      double: "이중 확인",
    };
    var execTxt = {
      auto: "자동 실행",
      confirm: "단일 확인 후 실행",
      double_confirm: "이중 확인 후 실행",
      notify_only: "알림만(실행 안 함)",
    };
    return (
      '<div class="result-card tone-' +
      esc(t.name) +
      '">' +
      '<div class="rc-top">' +
      tierPill(t) +
      '<span class="rc-origin">' +
      esc(p.origin || "") +
      "</span></div>" +
      '<p class="rc-action">' +
      esc(p.capability + "." + p.action) +
      " → <b>" +
      esc(p.entity_id) +
      "</b></p>" +
      '<p class="rc-rationale">' +
      esc(p.rationale) +
      "</p>" +
      '<div class="rc-foot">' +
      chip(
        "개입 " +
          (iv ? iv.condition : "") +
          " · " +
          (conf[p.required_confirmation] || p.required_confirmation),
        "",
      ) +
      (iv
        ? chip(
            "실행 판정: " +
              (execTxt[iv.execution_decision] || iv.execution_decision) +
              (iv.execution_enabled ? "" : " (옵트인 OFF)"),
            "",
          )
        : "") +
      (p.expires_at
        ? chip("만료 " + esc(p.expires_at.slice(11, 16)), "muted")
        : "") +
      "</div></div>"
    );
  }

  /**
   * 침묵(제안 없음) 카드를 렌더한다.
   * @param {string} reason 침묵 사유
   * @returns {string} 침묵 카드 HTML
   */
  function silenceCard(reason) {
    return (
      '<div class="result-card tone-silent">' +
      '<div class="rc-top"><span class="tierpill is-silent">◌ 침묵</span></div>' +
      '<p class="rc-rationale">' +
      esc(reason) +
      "</p>" +
      '<p class="rc-note">아무것도 도착하지 않았습니다 — 그리고 그것이 정답일 수 있습니다.</p></div>'
    );
  }

  /* ---------- 메인 렌더 ---------- */
  /**
   * 서버 응답(실제 파이프라인 트레이스)을 단계별 UI 로 그린다.
   * @param {object} d /api/simulate 응답 JSON
   */
  function render(d) {
    var badges = $("traceBadges");
    if (!d.ok) {
      $("traceBody").innerHTML =
        '<div class="trace-err">파이프라인 오류: ' + esc(d.error) + "</div>";
      badges.innerHTML = "";
      return;
    }
    var inp = d.input || {};
    badges.innerHTML =
      chip(
        (inp.time_bucket || "") +
          " · " +
          (inp.occupancy === "vacant" ? "비어있음" : "재실"),
        "",
      ) +
      chip("조건 " + inp.condition, "") +
      (inp.llm && inp.llm.enabled
        ? chip(
            "LLM " +
              inp.llm.model +
              " · " +
              (d.timing_ms ? d.timing_ms.pipeline_and_llm + "ms" : ""),
            "llm",
          )
        : chip("LLM off (FakeLLM)", "muted"));

    var html = "";

    // L1
    var stChips = Object.keys(inp.state || {})
      .map(function (k) {
        var v = inp.state[k];
        var lbl = k.split(".").pop();
        return chip(
          lbl + "=" + (typeof v === "boolean" ? (v ? "on" : "off") : v),
          "",
        );
      })
      .join("");
    html += stage(
      "Layer 1 · 감지",
      "센서 상태 정규화",
      stChips +
        (inp.outdoor
          ? '<div class="tsub">외기: ' +
            esc(JSON.stringify(inp.outdoor)) +
            "</div>"
          : ""),
    );

    // L2 snapshot
    if (d.snapshot) {
      html += stage(
        "Layer 2 · 맥락",
        "스냅샷 고정",
        '<p class="tsub">' +
          esc(d.snapshot.nl_summary || "(요약 없음)") +
          "</p>" +
          '<span class="thash">hash ' +
          esc(d.snapshot.snapshot_hash) +
          "…</span>",
      );
    }

    // ① 트리거 감지
    if (d.trigger) {
      html += stage(
        "① 트리거 감지",
        d.trigger.source === "llm" ? "LLM 옵트인" : "규칙 상시",
        chip(d.trigger.kind || "") +
          (d.trigger.origin
            ? '<span class="thash">' + esc(d.trigger.origin) + "</span>"
            : ""),
      );
    }
    // 자연어 지시(활성)
    if (d.directives && d.directives.active && d.directives.active.length) {
      html += stage(
        "🗣 자연어 지시",
        "자율 제안 억제(안전 제외)",
        d.directives.active
          .map(function (x) {
            return chip(x.raw_text || x.capability + " " + x.constraint, "warn");
          })
          .join(""),
      );
    }
    // ② 선호 임베딩
    if (d.embedding) {
      html += stage(
        "② 선호 임베딩",
        "맥락별 개인화 (유튜브식)",
        chip("선호 " + Number(d.embedding.preference).toFixed(2)) +
          '<span class="thash">' +
          esc(d.embedding.cell) +
          "</span><p class=\"tsub\">" +
          esc(d.embedding.note) +
          "</p>",
      );
    }
    // ③ 2갈래 제안 (Line A · ML / Line B · LLM)
    if (d.lines && (d.lines.line_a.length || d.lines.line_b.length)) {
      var lr = function (label, arr, cls) {
        var inner = !arr || !arr.length
          ? "—"
          : arr
              .map(function (x) {
                return (
                  '<span class="tlbl">' +
                  esc(x.label) +
                  " · " +
                  (x.line_confidence != null
                    ? Number(x.line_confidence).toFixed(2)
                    : "") +
                  "</span>"
                );
              })
              .join("");
        return '<div class="tline t-' + cls + '"><b>' + label + "</b>" + inner + "</div>";
      };
      html += stage(
        "③ 2갈래 제안",
        "ML · LLM 라인이 각각 제안",
        lr("Line A · ML", d.lines.line_a, "a") +
          lr("Line B · LLM", d.lines.line_b, "b"),
      );
    }
    // ④ 아비터 · 융합 (후보 + 평가)
    if (d.candidates && d.candidates.length) {
      var arbline =
        d.arbiter && d.arbiter.origin
          ? '<p class="tsub thash">합의: ' + esc(d.arbiter.origin) + "</p>"
          : "";
      var evline = d.evaluation
        ? '<p class="tsub">' + esc(d.evaluation.rationale) + "</p>"
        : "";
      html += stage(
        "④ 아비터 · 융합",
        "결정론 가중합으로 최종 1개 선택",
        candBoard(d.candidates) + arbline + evline,
      );
    }

    // Gate1
    if (d.gate1) {
      html += stage(
        "Layer 3 · Gate 1",
        "결정론적 안전 게이트",
        tierPill(d.gate1.floor_tier) +
          " 하한 부여 " +
          (d.gate1.safety_critical ? chip("SAFETY-CRITICAL", "danger") : "") +
          (d.gate1.blocked ? chip("BLOCKED", "danger") : "") +
          '<div class="tsub mono">' +
          esc(d.gate1.verdict) +
          "</div>",
      );
    }

    // Gate2 LLM
    if (d.gate2) {
      var vmap = {
        concur: "동의(승격 없음)",
        injection_suspect: "인젝션 의심 — 무시",
        degraded_fail_safe: "LLM 비가용 → 보수적 강등",
        escalated: "위험도 승격",
      };
      var vcls = d.gate2.degraded
        ? "warn"
        : d.gate2.verdict === "injection_suspect"
          ? "danger"
          : "ok";
      html += stage(
        "Layer 3 · Gate 2",
        "로컬 LLM 교차 검증 (승격만 가능)",
        chip(vmap[d.gate2.verdict] || d.gate2.verdict, vcls) +
          chip("conf " + d.gate2.confidence, "") +
          '<div class="tsub mono">' +
          esc(d.gate2.model_id) +
          (d.gate2.degraded ? " · degraded" : "") +
          "</div>",
      );
    }

    // 판정
    if (d.judgment) {
      html += stage(
        "Layer 3 · 판정",
        "최종 위험도 = max(Gate1, Gate2)",
        tierPill(d.judgment.risk_tier) +
          '<p class="tsub">' +
          esc(d.judgment.rationale) +
          "</p>",
      );
    }

    // 결과
    html +=
      '<div class="tstage result">' +
      '<div class="tstage-head"><span class="tstage-layer">Layer 4 · 개입</span><h4>' +
      (d.silence ? "침묵" : "휴대폰 알림 도착") +
      '</h4></div><div class="tstage-body">' +
      (d.silence
        ? silenceCard(d.silence_reason)
        : notiCard(d.proposal, d.intervention)) +
      "</div></div>";

    $("traceBody").innerHTML = html;

    if (!reduceMotion) {
      requestAnimationFrame(function () {
        $("traceBody")
          .querySelectorAll(".cand-fill")
          .forEach(function (el) {
            var w = el.style.width;
            el.style.width = "0";
            requestAnimationFrame(function () {
              el.style.width = w;
            });
          });
      });
    }
  }

  /* ---------- 실행 ---------- */
  var busy = false;
  /**
   * 요청을 만들어 실제 파이프라인에 태우고 결과를 렌더한다(중복 실행 방지).
   * @param {"tick"|"event"} mode 실행 모드
   * @param {object} [event] 이벤트 모드 이벤트 명세
   */
  function run(mode, event) {
    if (busy) return;
    busy = true;
    $("liveTag").hidden = false;
    document.querySelectorAll(".lab-run").forEach(function (b) {
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
      .then(render)
      .catch(function (e) {
        $("traceBody").innerHTML =
          '<div class="trace-err">요청 실패: ' +
          esc(e.message) +
          '<br /><span class="tsub">데모 서버가 켜져 있나요? <code>python -m liot.demo_server</code></span></div>';
      })
      .finally(function () {
        busy = false;
        $("liveTag").hidden = true;
        document.querySelectorAll(".lab-run").forEach(function (b) {
          b.disabled = false;
        });
      });
  }

  $("runTick").addEventListener("click", function () {
    run("tick");
  });
  $("runEvent").addEventListener("click", function () {
    var v = $("eventEntity").value;
    var ev;
    if (v.indexOf(":") >= 0) {
      var parts = v.split(":");
      ev = {
        entity: parts[0],
        value: parts[1] === "turn_on",
        action: parts[1],
      };
    } else {
      ev = { entity: v, value: true };
    }
    run("event", ev);
  });
})();
