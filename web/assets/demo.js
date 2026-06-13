/* LIOT 파이프라인 시뮬레이터
   실제 파이프라인(L1→L2→Gate1→Gate2→L4)의 판단 결과를 시나리오별 스크립트로 재현한다.
   여기에 실제 네트워크 호출은 없다 — 순수 프론트 시뮬레이션. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var STEPS = ["l1", "l2", "g1", "g2", "l4"];

  /* 각 시나리오: 단계별 (로그, 상태, 결과) + 최종 결말 */
  var SCENARIOS = {
    gas: {
      steps: {
        l1: { log: "주방 가스 센서 — 농도 임계 초과. <span class='warn'>safety-critical 카테고리</span>로 분류.", state: "수신", result: "done" },
        l2: { log: "스냅샷 고정. 시간대=심야 · 재실=수면 추정. 변화 감지 → 즉시 후보 생성.", state: "고정", result: "done" },
        g1: { log: "가드레일 판정: <span class='block'>EMBER(잉걸·3)</span> 부여. 안전 신호는 하향 불가 — escalate only.", state: "EMBER", result: "done" },
        g2: { log: "안전 경로는 LLM 의견과 무관하게 전진. <span class='ok'>거부권 적용 대상 아님.</span>", state: "통과", result: "done" },
        l4: { log: "알림 예산 소진 + 심야 — 그러나 EMBER는 피로도 장치를 <span class='warn'>전부 우회</span>. 정제 후 송신.", state: "송신", result: "done" }
      },
      outcome: {
        type: "alert",
        tier: "ember",
        glyph: "●",
        title: "● 잉걸 — 즉시 확인해 주세요",
        text: "주방에서 가스 농도 상승이 감지됐어요. 창문을 열고 직접 확인해 주세요.",
        actions: ["확인했어요"],
        verdict: "<b>Safety-Never-Silent.</b> 예산이 바닥나도, 새벽이어도, 안전 신호는 반드시 도달합니다. 단, LIOT이 한 일은 알림뿐 — 밸브를 잠그는 것은 사람의 몫입니다."
      }
    },

    light: {
      steps: {
        l1: { log: "재실 센서 — 거실 30분째 무인. 새 명령 없음, 평온한 저녁.", state: "수신", result: "done" },
        l2: { log: "능동 틱: 결정론적 규칙 <span class='warn'>vacant-light-off</span> 매칭. 쿨다운·예산 확인 통과.", state: "제안", result: "done" },
        g1: { log: "가드레일 판정: <span class='ok'>BREATH(잔잔·1)</span> — 능동 상한선 이내. 안전 카테고리 아님 확인.", state: "BREATH", result: "done" },
        g2: { log: "LLM 심사: 맥락상 무리 없음. <span class='ok'>거부권 행사하지 않음.</span>", state: "통과", result: "done" },
        l4: { log: "발화 게이트: 중복 없음 · 야간 아님 · 예산 여유. 개인정보 정제 후 <span class='ok'>가볍게 송신.</span>", state: "송신", result: "done" }
      },
      outcome: {
        type: "alert",
        tier: "breath",
        glyph: "○",
        title: "○ 잔잔 — 제안이 하나 있어요",
        text: "거실이 30분째 비어 있어요. 조명을 꺼 둘까요?",
        actions: ["좋아요", "괜찮아요"],
        verdict: "<b>AI가 먼저 말을 걸었습니다.</b> 그러나 조명은 아직 켜져 있습니다 — 버튼을 누르기 전까지 세상은 1mm도 바뀌지 않습니다. 거절하면 같은 류의 제안은 한동안 조용해집니다."
      }
    },

    heat: {
      steps: {
        l1: { log: "온도 센서 — 침실 18.2°C, 바깥 기온 하강 중.", state: "수신", result: "done" },
        l2: { log: "능동 틱: 규칙 <span class='warn'>evening-chill-heat</span> 매칭 — 난방 22°C 제안 생성.", state: "제안", result: "done" },
        g1: { log: "가드레일 판정: 난방은 생활에 닿는 변화 — <span class='block'>DUSK(노을·2)</span>로 상향. escalate only.", state: "DUSK", result: "done" },
        g2: { log: "판정 후 검사: 능동 제안 상한은 BREATH(1). DUSK(2) > 상한 — <span class='block'>침묵 결정.</span>", state: "차단", result: "blocked" },
        l4: { log: "도달하지 않음. 제안은 기록만 남기고 소멸 — 사용자는 방해받지 않습니다.", state: "—", result: "skip" }
      },
      outcome: {
        type: "silent",
        verdict: "<b>Proactive-Safe-Silent.</b> 능동 제안이 BREATH 상한을 넘는 순간 LIOT은 입을 다뭅니다. 잃은 것은 약간의 편리함뿐 — 난방이 정말 필요하면 사용자가 직접 요청하면 되고, 그 경로는 언제나 열려 있습니다. 편의의 침묵은 안전합니다."
      }
    }
  };

  var buttons = document.querySelectorAll(".scenario");
  var resultZone = document.getElementById("resultZone");
  var liveTag = document.getElementById("liveTag");
  if (!buttons.length || !resultZone) return;

  var timers = [];
  var running = false;

  function clearTimers() {
    timers.forEach(clearTimeout);
    timers = [];
  }

  function later(fn, ms) {
    timers.push(setTimeout(fn, reduceMotion ? 0 : ms));
  }

  function resetBoard() {
    clearTimers();
    STEPS.forEach(function (key) {
      var step = document.querySelector('[data-step="' + key + '"]');
      var log = document.querySelector('[data-log="' + key + '"]');
      var state = document.querySelector('[data-state="' + key + '"]');
      if (step) step.classList.remove("is-active", "is-done", "is-blocked");
      if (log) log.innerHTML = "";
      if (state) state.textContent = "대기";
    });
    resultZone.innerHTML = "";
  }

  function setStep(key, phase, logHtml, stateText) {
    var step = document.querySelector('[data-step="' + key + '"]');
    var log = document.querySelector('[data-log="' + key + '"]');
    var state = document.querySelector('[data-state="' + key + '"]');
    if (!step) return;
    step.classList.remove("is-active", "is-done", "is-blocked");
    if (phase === "active") step.classList.add("is-active");
    if (phase === "done") step.classList.add("is-done");
    if (phase === "blocked") step.classList.add("is-blocked");
    if (logHtml !== undefined && log) log.innerHTML = logHtml;
    if (stateText !== undefined && state) state.textContent = stateText;
  }

  function tierIconSvg(tier) {
    if (tier === "ember") {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 3 19h18L12 2z"/><path d="M12 9v5M12 17v.5"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v2M5.6 5.6l1.4 1.4M3 12h2M18.4 5.6 17 7M19 12h2M8 17a4 4 0 1 1 8 0v1H8z"/><path d="M7 21h10"/></svg>';
  }

  function renderOutcome(outcome) {
    if (outcome.type === "silent") {
      resultZone.innerHTML =
        '<div class="result-idle" style="opacity:.85">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/><path d="M11 18h2"/></svg>' +
        "<p>… 아무것도 도착하지 않았습니다.<br />그리고 그것이 정답입니다.</p>" +
        "</div>" +
        '<div class="result-verdict v-silent">' + outcome.verdict + "</div>";
      return;
    }

    var actionsHtml = outcome.actions
      .map(function (label) {
        return "<span data-fb>" + label + "</span>";
      })
      .join("");

    resultZone.innerHTML =
      '<div class="demo-noti">' +
      '<div class="noti" style="opacity:1;transform:none;animation:none;border:0;background:none;padding:0">' +
      '<div class="noti-icon t-' + outcome.tier + '" aria-hidden="true">' + tierIconSvg(outcome.tier) + "</div>" +
      '<div class="noti-body">' +
      '<div class="noti-app"><span>LIOT · ntfy</span><span>지금</span></div>' +
      '<p class="noti-title">' + outcome.title + "</p>" +
      '<p class="noti-text">' + outcome.text + "</p>" +
      '<div class="noti-actions">' + actionsHtml + "</div>" +
      "</div></div></div>" +
      '<div class="result-verdict v-alert">' + outcome.verdict + "</div>";

    /* 피드백 버튼 — HMAC 서명 피드백 흐름 재현 */
    resultZone.querySelectorAll("[data-fb]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (resultZone.querySelector(".result-feedback")) return;
        var fb = document.createElement("p");
        fb.className = "result-feedback";
        fb.textContent =
          "서명 검증 통과 — “" + btn.textContent + "” 피드백이 기록되어 다음 제안의 빈도에 반영됩니다.";
        resultZone.appendChild(fb);
      });
    });
  }

  function run(name, btn) {
    if (running) return;
    running = true;
    resetBoard();

    buttons.forEach(function (b) {
      b.classList.toggle("is-active", b === btn);
      b.disabled = true;
    });
    if (liveTag) liveTag.hidden = false;

    /* 보드가 화면 밖이면(특히 모바일) 트레이스가 보이도록 끌어온다 */
    var rail = document.querySelector(".board-rail");
    if (rail) {
      var r = rail.getBoundingClientRect();
      var viewH = window.innerHeight || document.documentElement.clientHeight;
      if (r.top > viewH * 0.55 || r.top < 0) {
        rail.scrollIntoView({
          behavior: reduceMotion ? "auto" : "smooth",
          block: "start"
        });
      }
    }

    var sc = SCENARIOS[name];
    var stepGap = 950;

    STEPS.forEach(function (key, i) {
      var info = sc.steps[key];

      later(function () {
        if (info.result === "skip") {
          setStep(key, "", info.log, info.state);
          return;
        }
        setStep(key, "active", info.log, "진행 중");
      }, i * stepGap);

      later(function () {
        if (info.result === "done") setStep(key, "done", undefined, info.state);
        else if (info.result === "blocked") setStep(key, "blocked", undefined, info.state);
        else setStep(key, "", undefined, info.state);
      }, i * stepGap + 700);
    });

    later(function () {
      renderOutcome(sc.outcome);
      if (liveTag) liveTag.hidden = true;
      buttons.forEach(function (b) {
        b.disabled = false;
      });
      running = false;
    }, STEPS.length * stepGap + 300);
  }

  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      run(btn.getAttribute("data-scenario"), btn);
    });
  });
})();
