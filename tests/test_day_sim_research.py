"""하루 시뮬레이터의 연구용 옵션(조건 A/B·확률적 거주자·학습률·영구 차단·60일).

핵심: 옵션을 하나도 안 켜면 사건·요약이 예전과 바이트까지 같다(``scripts/day_fingerprint.py`` 와
같은 지문), 켜면 그 옵션만 움직인다. 기대값은 리터럴이다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from liot.day_sim import (
    DaySim,
    SceneTaste,
    StochasticResident,
    build_script,
    parse_day_request,
)
from liot.layers.l2_context.feature_vector import apply_decay, apply_signal
from liot.live_runs import LiveRequestError

TASTES = {
    "ventilate": SceneTaste(0.8, 0.05),
    "purifier": SceneTaste(0.1, 0.6),
    "desk_light": SceneTaste(0.5, 0.1),
    "bedtime": SceneTaste(0.8, 0.0),
}


def _resident(**kw: Any) -> StochasticResident:
    kw.setdefault("tastes", TASTES)
    return StochasticResident(**kw)


def _run(sim: DaySim) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    asyncio.run(sim.run(lambda stage, data: events.append((stage, data))))
    return events


def _fingerprint(seed: int, days: int, **kw: Any) -> tuple[int, str]:
    sim = DaySim(seed=seed, days=days, **kw)
    events = [[stage, data] for stage, data in _run(sim)]
    for data in [d for _, d in events] + sim.summary()["safety"]:
        data.pop("first_alert_ms", None)  # 시간(ms)은 실행마다 다르다 — 지문에서 뺀다
    summary = sim.summary()
    for rec in summary["safety"]:
        rec.pop("first_alert_ms", None)
    blob = json.dumps({"events": events, "summary": summary},
                      ensure_ascii=False, sort_keys=True, default=str)
    return len(events), hashlib.sha256(blob.encode()).hexdigest()


def _purifier_pushes(sim: DaySim) -> dict[int, int]:
    out: dict[int, int] = {}
    for p in sim.proposals:
        if p["scene"] == "purifier" and p["decision"] == "IMMEDIATE":
            out[p["day"]] = out.get(p["day"], 0) + 1
    return out


def _day_totals(sim: DaySim) -> list[tuple[int, int, int, int, int, int]]:
    return [(m["day"], m["pushes"], m["accepted"], m["tray_accepted"], m["dismissed"], m["expired"])
            for m in sim.metrics]


# ── 기본 불변: 옵션을 안 켜면 예전과 같다 ──────────────────────────────────────


def test_default_run_is_byte_identical_to_the_pre_option_fingerprint() -> None:
    # scripts/day_fingerprint.py 의 7x3 지문(옵션 도입 전 커밋에서 잰 값)
    assert _fingerprint(7, 3) == (
        129, "fcac9460938918a69999671e2da53dca69b9b201c106fec34d55e76e6016a3c2")


def test_default_summary_has_no_research_block_and_safety_has_no_first_alert_ms() -> None:
    sim = DaySim(seed=7, days=3)
    _run(sim)
    summary = sim.summary()
    assert "research" not in summary
    assert all("first_alert_ms" not in s for s in summary["safety"])
    assert all("resident" not in p for p in summary["proposals"])


def test_explicit_condition_b_does_not_change_the_comfort_decisions() -> None:
    base, b = DaySim(seed=7, days=3), DaySim(seed=7, days=3, condition="B")
    _run(base)
    _run(b)
    key = ("day", "hhmm", "scene", "decision", "confirmation")
    assert [tuple(p[k] for k in key) for p in base.proposals] == \
        [tuple(p[k] for k in key) for p in b.proposals]


# ── 조건 A/B ────────────────────────────────────────────────────────────────


def test_condition_a_asks_explicit_for_every_comfort_proposal_and_b_is_tiered() -> None:
    a, b = DaySim(seed=7, days=3, condition="A"), DaySim(seed=7, days=3, condition="B")
    _run(a)
    _run(b)
    assert {p["confirmation"] for p in a.proposals} == {"explicit"}
    assert {p["confirmation"] for p in b.proposals} == {"none", "light"}


def test_condition_is_case_insensitive_and_validated() -> None:
    assert DaySim(seed=1, days=1, condition=" a ").condition == "A"
    with pytest.raises(ValueError, match="condition"):
        DaySim(seed=1, days=1, condition="C")


def test_condition_a_and_b_leave_the_safety_path_identical() -> None:
    """실험이 안전을 바꾸지 않는다: 조건 A/B 의 안전 사건(날·시각·종류·단계)이 같다."""
    a, b = DaySim(seed=7, days=3, condition="A"), DaySim(seed=7, days=3, condition="B")
    _run(a)
    _run(b)

    def shape(sim: DaySim) -> list[tuple[Any, ...]]:
        return [(s["day"], s["hhmm"], s["kind"], s.get("phase"), s.get("severity_label"))
                for s in sim.safety_events]

    assert shape(a) == shape(b)
    assert [s["kind"] for s in a.safety_events].count("first_alert") == 3


# ── 날 수 ───────────────────────────────────────────────────────────────────


def test_direct_use_allows_60_days_but_http_keeps_7() -> None:
    assert DaySim(seed=1, days=60).days == 60
    with pytest.raises(ValueError, match="60"):
        DaySim(seed=1, days=61)
    with pytest.raises(LiveRequestError):
        parse_day_request({"days": 8})
    assert len(build_script(1, 60)) == 60


def test_script_for_30_days_is_a_prefix_of_the_script_for_60() -> None:
    short, long = build_script(5, 30), build_script(5, 60)
    assert [(p.day, p.kind, p.safety_at) for p in short] == \
        [(p.day, p.kind, p.safety_at) for p in long[:30]]


# ── 학습률·영구 차단 ─────────────────────────────────────────────────────────


def test_apply_signal_alpha_override_and_default_literals() -> None:
    assert apply_signal({}, "k", "accepted") == {"k": 0.65}  # 출하값 0.3: 0.5 + 0.3*0.5
    assert apply_signal({}, "k", "accepted", alpha=0.6) == {"k": 0.8}
    assert apply_signal({}, "k", "accepted", alpha=0.0) == {"k": 0.5}
    assert apply_signal({"k": 0.5}, "k", "dismissed", alpha=0.1) == {"k": 0.45}


def test_apply_decay_rate_override_and_default_literals() -> None:
    assert apply_decay({"k": 1.0}) == {"k": 0.975}  # 출하값 0.05: 0.5 + 0.5*0.95
    assert apply_decay({"k": 1.0}, rate=0.0) == {"k": 1.0}
    assert apply_decay({"k": 1.0}, rate=0.5) == {"k": 0.75}


def test_options_are_threaded_into_the_service_only_when_given() -> None:
    plain = DaySim(seed=7, days=1)
    _run(plain)
    assert plain._svc is not None
    assert (plain._svc.pref_alpha, plain._svc.pref_decay, plain._svc.reject_window_days) == \
        (None, None, 30)
    tuned = DaySim(seed=7, days=1, learning_rate=0.6, decay_rate=0.1, reject_window_days=0)
    _run(tuned)
    assert tuned._svc is not None
    assert (tuned._svc.pref_alpha, tuned._svc.pref_decay, tuned._svc.reject_window_days) == \
        (0.6, 0.1, 0)
    perm = DaySim(seed=7, days=1, permanent_block_on_dismiss=True)
    _run(perm)
    assert perm._svc is not None and perm._svc.reject_window_days == 36500


def _final_cells(**kw: Any) -> dict[str, float]:
    sim = DaySim(seed=7, days=3, research=True, **kw)
    _run(sim)
    cells: dict[str, float] = sim.summary()["research"]["pref_cells"][-1]["cells"]
    return cells


def test_learning_rate_and_decay_overrides_reach_the_preference_cells() -> None:
    """씨앗 7(기본 거주자): 공기청정기 셀(fan:set_speed)은 [그만해], 환기 셀은 [도움됐어요]로 움직인다."""
    fan, vent = "day|occupied|fan:set_speed", "morning|occupied|advice:ventilate"
    default = _final_cells()
    assert (default[fan], default[vent]) == (0.3897, 0.7823)
    assert set(_final_cells(learning_rate=0.0).values()) == {0.5}
    fast = _final_cells(learning_rate=0.6, decay_rate=0.0)
    assert (fast[fan], fast[vent]) == (0.2, 0.968)
    forgetful = _final_cells(learning_rate=0.6, decay_rate=0.5)
    assert (forgetful[fan], forgetful[vent]) == (0.4954, 0.5915)


def test_dismiss_block_and_learning_can_be_switched_off() -> None:
    """기본: 1일차 [그만해] 뒤 3일차 공기청정기가 막힌다. 차단·학습을 끄면 3일차에 다시 도달한다."""
    default = DaySim(seed=7, days=3)
    free = DaySim(seed=7, days=3, learning_rate=0.0, reject_window_days=0)
    _run(default)
    _run(free)
    assert _purifier_pushes(default) == {1: 1}
    assert _purifier_pushes(free) == {1: 1, 3: 1}


def test_permanent_block_outlasts_the_thirty_day_window() -> None:
    """씨앗 1: 1일차 [그만해] 뒤 34일차에 나쁜 공기가 다시 온다. 30일 쉬기는 끝나 있고 영구 차단은 아니다."""
    default = DaySim(seed=1, days=36)
    perm = DaySim(seed=1, days=36, permanent_block_on_dismiss=True)
    _run(default)
    _run(perm)
    assert _purifier_pushes(default) == {1: 1, 34: 1}
    assert _purifier_pushes(perm) == {1: 1}


def test_exploration_reoffers_a_blocked_suggestion_only_on_every_nth_day() -> None:
    """씨앗 1: 나쁜 공기 날 [1,3,4,9,20,21]. 기본은 1일차 뒤 막혀 있고, 7일마다 탐색하면 21일차에 다시 온다."""
    default = DaySim(seed=1, days=21)
    explore = DaySim(seed=1, days=21, explore_every_days=7)
    _run(default)
    _run(explore)
    assert _purifier_pushes(default) == {1: 1}
    assert _purifier_pushes(explore) == {1: 1, 21: 1}
    assert explore.summary()["research"]["explore_days"] == [7, 14, 21]


def test_exploration_is_validated() -> None:
    with pytest.raises(ValueError, match="explore_every_days"):
        DaySim(seed=1, days=1, explore_every_days=0)


def test_permanent_block_and_window_are_exclusive_and_validated() -> None:
    with pytest.raises(ValueError, match="함께"):
        DaySim(seed=1, days=1, permanent_block_on_dismiss=True, reject_window_days=5)
    with pytest.raises(ValueError, match="learning_rate"):
        DaySim(seed=1, days=1, learning_rate=1.5)
    with pytest.raises(ValueError, match="reject_window_days"):
        DaySim(seed=1, days=1, reject_window_days=-1)


# ── 확률적 거주자: 모델 단위 ───────────────────────────────────────────────────


def test_latency_pmf_inverse_cdf_literals() -> None:
    r = _resident(latency_pmf=(0.5, 0.25, 0.15, 0.1))
    assert [r.latency_steps(u) for u in (0.0, 0.49, 0.5, 0.74, 0.75, 0.89, 0.9, 0.999)] == \
        [1, 1, 2, 2, 3, 3, 4, 4]


def test_flip_day_swaps_only_the_listed_scenes() -> None:
    r = _resident(flip_day=15, tastes_after={"purifier": SceneTaste(0.9, 0.0)})
    assert [r.helpful("purifier", d) for d in (1, 14, 15, 60)] == [False, False, True, True]
    assert [r.helpful("ventilate", d) for d in (1, 14, 15, 60)] == [True] * 4
    assert r.helpful("unknown_scene", 20) is False


@pytest.mark.parametrize(
    "kw",
    [
        {"attention": 1.2},
        {"fatigue": -0.1},
        {"effort_cost": 0.5},
        {"latency_pmf": (0.5, 0.4)},
        {"load_half_life_steps": 0.0},
        {"flip_day": 15},
    ],
)
def test_resident_rejects_bad_parameters(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _resident(**kw)


def test_scene_taste_rejects_probabilities_over_one() -> None:
    with pytest.raises(ValueError):
        SceneTaste(0.7, 0.4)


def test_resident_with_cloud_models_is_refused() -> None:
    with pytest.raises(ValueError, match="fake"):
        DaySim(seed=1, days=1, models="cloud", resident=_resident())


def test_confirmation_strength_lowers_the_chance_of_acting() -> None:
    """효과: effort_cost 0.33 에서 none 은 늘 누르고 double 은 거의 안 누른다(explicit 은 중간)."""
    sim = DaySim(seed=3, days=1, resident=_resident(
        attention=1.0, fatigue=0.0, effort_cost=0.33,
        tastes={"ventilate": SceneTaste(1.0, 0.0)}))
    counts = {}
    for conf in ("none", "light", "explicit", "double"):
        counts[conf] = sum(
            sim._resident_react("push", f"k{i}", "ventilate", 1, conf, gate=1.0)[0] == "accepted"
            for i in range(400))
    assert counts["none"] == 400
    assert counts["none"] > counts["light"] > counts["explicit"] > counts["double"]
    assert counts["double"] <= 20


def test_zero_effort_cost_makes_confirmation_strength_irrelevant() -> None:
    sim = DaySim(seed=3, days=1, resident=_resident(
        attention=1.0, fatigue=0.0, effort_cost=0.0, tastes={"ventilate": SceneTaste(1.0, 0.0)}))
    for conf in ("none", "double"):
        assert all(
            sim._resident_react("push", f"k{i}", "ventilate", 1, conf, gate=1.0)[0] == "accepted"
            for i in range(100))


def test_fatigue_factor_follows_the_load_and_halves_per_half_life() -> None:
    sim = DaySim(seed=3, days=1, resident=_resident(fatigue=1.0, load_half_life_steps=16.0))
    assert sim._fatigue_factor() == 1.0
    sim._load_add(1.0)
    assert sim._fatigue_factor() == pytest.approx(0.5)
    sim._tick_n = 16
    assert sim._fatigue_factor() == pytest.approx(1.0 / 1.5)
    sim._load_add(3.0)  # 0.5 + 3 = 3.5
    assert sim._fatigue_factor() == pytest.approx(1.0 / 4.5)


def test_zero_fatigue_ignores_the_load() -> None:
    sim = DaySim(seed=3, days=1, resident=_resident(fatigue=0.0))
    sim._load_add(50.0)
    assert sim._fatigue_factor() == 1.0


def test_reaction_draws_are_keyed_per_event_so_arms_share_them() -> None:
    a = DaySim(seed=3, days=1, resident=_resident())
    b = DaySim(seed=3, days=1, resident=_resident())
    b._load_add(2.0)  # 부하만 다르다 — 같은 사건 열쇠는 같은 난수를 받는다
    ra = a._resident_react("push", "07:00:ventilate", "ventilate", 2, "none", gate=0.9)
    rb = b._resident_react("push", "07:00:ventilate", "ventilate", 2, "none", gate=0.9)
    assert ra[1]["latency_steps"] == rb[1]["latency_steps"]
    c = DaySim(seed=4, days=1, resident=_resident())
    rc = [c._resident_react("push", f"k{i}", "ventilate", 2, "none", gate=0.9)[1]["latency_steps"]
          for i in range(30)]
    ra30 = [a._resident_react("push", f"k{i}", "ventilate", 2, "none", gate=0.9)[1]["latency_steps"]
            for i in range(30)]
    assert rc != ra30


# ── 확률적 거주자: 실행 ──────────────────────────────────────────────────────


def test_resident_run_is_deterministic_and_seed_dependent() -> None:
    r = _resident()
    fp1 = _fingerprint(3, 5, resident=r)
    assert _fingerprint(3, 5, resident=r) == fp1
    assert _fingerprint(4, 5, resident=r) != fp1


def test_always_accepting_resident_accepts_every_phone_push_it_can() -> None:
    r = StochasticResident(tastes={s: SceneTaste(1.0) for s in TASTES}, attention=1.0,
                           fatigue=0.0, effort_cost=0.0, latency_pmf=(1.0,))
    sim = DaySim(seed=7, days=3, resident=r)
    _run(sim)
    pushed = [p for p in sim.proposals if p["decision"] == "IMMEDIATE"]
    assert len(pushed) == 5
    assert [p["state"] for p in pushed] == ["accepted"] * 5


def test_never_reacting_resident_lets_every_phone_push_expire() -> None:
    r = StochasticResident(tastes={s: SceneTaste(0.0) for s in TASTES})
    sim = DaySim(seed=7, days=3, resident=r)
    _run(sim)
    pushed = [p for p in sim.proposals if p["decision"] == "IMMEDIATE"]
    assert pushed and {p["state"] for p in pushed} == {"expired"}
    assert sum(m["accepted"] + m["dismissed"] for m in sim.metrics) == 0


def test_dismissing_resident_triggers_the_production_block() -> None:
    r = StochasticResident(tastes={s: SceneTaste(0.0, 1.0) for s in TASTES}, attention=1.0,
                           fatigue=0.0, effort_cost=0.0, latency_pmf=(1.0,))
    sim = DaySim(seed=7, days=3, resident=r)
    _run(sim)
    ventilate = [p for p in sim.proposals if p["scene"] == "ventilate"
                 and p["decision"] == "IMMEDIATE"]
    assert [(p["day"], p["state"]) for p in ventilate] == [(1, "dismissed")]


def test_zero_fatigue_and_zero_effort_make_conditions_a_and_b_identical() -> None:
    """H4.3 의 시뮬레이터 결함 점검: 피로·수고가 0 이면 A/B 거주자 결과가 같아야 한다."""
    r = _resident(fatigue=0.0, effort_cost=0.0)
    a, b = DaySim(seed=3, days=6, condition="A", resident=r), \
        DaySim(seed=3, days=6, condition="B", resident=r)
    _run(a)
    _run(b)
    assert _day_totals(a) == _day_totals(b)
    assert _day_totals(a) == [
        (1, 2, 0, 0, 0, 3), (2, 1, 1, 1, 0, 0), (3, 2, 1, 1, 1, 0),
        (4, 1, 1, 1, 0, 0), (5, 1, 0, 1, 0, 1), (6, 1, 1, 0, 0, 1)]


def test_effort_makes_condition_a_get_fewer_accepts_than_b() -> None:
    r = StochasticResident(tastes={s: SceneTaste(1.0) for s in TASTES}, attention=1.0,
                           fatigue=0.0, effort_cost=0.33, latency_pmf=(1.0,))
    totals = {}
    for cond in ("A", "B"):
        sim = DaySim(seed=7, days=10, condition=cond, resident=r)
        _run(sim)
        totals[cond] = sum(m["accepted"] for m in sim.metrics)
    assert totals["A"] < totals["B"]


def test_resident_records_carry_the_decision_basis_only_when_on() -> None:
    sim = DaySim(seed=3, days=2, condition="B", resident=_resident())
    _run(sim)
    summary = sim.summary()
    assert summary["research"]["condition"] == "B"
    assert summary["research"]["reject_window_days"] == 30
    assert summary["research"]["resident"]["tastes"]["purifier"] == {"accept": 0.1, "dismiss": 0.6}
    log = summary["research"]["resident_log"]
    assert {r["channel"] for r in log} >= {"push", "tray_open"}
    assert all(0.0 <= r["p_notice"] <= 1.0 for r in log if r["channel"] == "push")
    assert all("resident" in p for p in summary["proposals"] if p["decision"] == "IMMEDIATE")


def test_opportunities_are_logged_with_scene_names_and_reasons() -> None:
    sim = DaySim(seed=7, days=3, research=True)
    _run(sim)
    opp = sim.summary()["research"]["opportunities"]
    first = opp[0]
    assert (first["day"], first["hhmm"], first["winner"], first["decision"]) == \
        (1, "06:45", "ventilate", "IMMEDIATE")
    # 3일차 11:00: 공기청정기는 1일차 [그만해] 때문에 건너뛰고 책상 조명이 모아보기로 간다
    assert {"day": 3, "hhmm": "11:00", "matched": ["purifier", "desk_light"], "winner": "desk_light",
            "decision": "DIGEST", "silence": None,
            "skipped": [{"scene": "purifier", "reason": "rejected"}]} in opp


# ── 안전 ─────────────────────────────────────────────────────────────────────


def test_first_alert_ms_is_recorded_only_with_an_option_and_never_changes_safety() -> None:
    plain, research = DaySim(seed=7, days=3), DaySim(seed=7, days=3, research=True)
    _run(plain)
    _run(research)
    firsts = [s for s in research.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3
    assert all(isinstance(s["first_alert_ms"], float) and 0.0 <= s["first_alert_ms"] < 5000.0
               for s in firsts)
    assert all("first_alert_ms" not in s for s in plain.safety_events)

    def shape(sim: DaySim) -> list[tuple[Any, ...]]:
        return [(s["day"], s["hhmm"], s["kind"], s["id"]) for s in sim.safety_events]

    assert shape(plain) == shape(research)


def test_stochastic_resident_does_not_delay_or_drop_safety_alerts() -> None:
    sim = DaySim(seed=7, days=3, resident=_resident(fatigue=5.0))
    _run(sim)
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3
    assert all(s["decision"] == "IMMEDIATE" and s["sent"] for s in firsts)
