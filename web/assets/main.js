/* LIOT 사이트 공통 모션/내비게이션 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- 테마(라이트/다크) ---------- */
  (function theme() {
    var root = document.documentElement;
    var toggle = document.getElementById("themeToggle");
    var meta = document.getElementById("themeColorMeta");
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var META_COLOR = { dark: "#0b0908", light: "#f3eee5" };

    // 현재 '실효' 테마: 수동 오버라이드가 있으면 그것, 없으면 시스템
    var effective = function () {
      var attr = root.getAttribute("data-theme");
      if (attr === "light" || attr === "dark") return attr;
      return mq.matches ? "dark" : "light";
    };

    var sync = function () {
      var eff = effective();
      if (toggle) {
        toggle.setAttribute("data-mode", eff);
        toggle.setAttribute(
          "aria-label",
          eff === "dark" ? "밝은 테마로 전환" : "어두운 테마로 전환"
        );
        toggle.setAttribute("aria-pressed", String(eff === "light"));
      }
      if (meta) meta.setAttribute("content", META_COLOR[eff]);
    };

    if (toggle) {
      toggle.addEventListener("click", function () {
        var next = effective() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try {
          localStorage.setItem("liot-theme", next);
        } catch (e) {}
        sync();
      });
    }

    // 시스템 테마가 바뀌면(수동 오버라이드가 없을 때만 의미 있음) 아이콘/메타 갱신
    var onMq = function () {
      sync();
    };
    if (mq.addEventListener) mq.addEventListener("change", onMq);
    else if (mq.addListener) mq.addListener(onMq);

    sync();
  })();

  /* ---------- 내비 스크롤 상태 ---------- */
  var nav = document.getElementById("nav");
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle("is-scrolled", window.scrollY > 24);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ---------- 모바일 메뉴 ---------- */
  var toggle = document.getElementById("navToggle");
  var menu = document.getElementById("mobileMenu");
  if (toggle && menu) {
    var setMenu = function (open) {
      menu.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "메뉴 닫기" : "메뉴 열기");
      document.body.style.overflow = open ? "hidden" : "";
    };
    toggle.addEventListener("click", function () {
      setMenu(!menu.classList.contains("is-open"));
    });
    menu.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () {
        setMenu(false);
      });
    });
    window.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setMenu(false);
    });
  }

  /* ---------- 스크롤 리빌 ---------- */
  var reveals = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    reveals.forEach(function (el) {
      el.classList.add("is-in");
    });
  } else {
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-in");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
    );
    reveals.forEach(function (el) {
      io.observe(el);
    });
  }

  /* ---------- 숫자 카운트업 ---------- */
  var counters = document.querySelectorAll("[data-count]");
  if (counters.length) {
    var runCount = function (el) {
      var target = parseInt(el.getAttribute("data-count"), 10) || 0;
      if (reduceMotion || target === 0) {
        el.textContent = String(target);
        return;
      }
      var dur = 1200;
      var t0 = null;
      var step = function (ts) {
        if (!t0) t0 = ts;
        var p = Math.min((ts - t0) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = String(Math.round(target * eased));
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };

    if (!("IntersectionObserver" in window)) {
      counters.forEach(function (el) {
        el.textContent = el.getAttribute("data-count");
      });
    } else {
      var cio = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              runCount(entry.target);
              cio.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.5 }
      );
      counters.forEach(function (el) {
        cio.observe(el);
      });
    }
  }
})();
