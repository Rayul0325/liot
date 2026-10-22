"""하루 시뮬레이터(liot/day_sim.py) — 실제 LiotService 위에서 KST 15분 단계로 사흘.

핵심 검증(씨앗 7, 기본 성향: 환기 좋아함·공기청정기 싫어함·책상 조명 반반·취침 준비 좋아함):
  - 같은 씨앗 = 같은 사건 목록(가짜 판정기·고정 대본·씨앗 난수).
  - 공기청정기가 폰에 도달한 수(IMMEDIATE)가 1일차보다 3일차에 적다 — 3일차에도 나쁜 공기가
    오므로 감소는 1일차 [그만해](실제 피드백 경로 → 30일 차단) 때문이다.
  - 4장면이 사흘 안에 각각 한 번 이상 폰 또는 모아보기에 도착한다.
  - 안전 사건은 그 값이 들어온 단계에 즉시 경보이고, 첫 알림 송신이 그 단계의 어떤 모델·외부
    호출보다 먼저다(cloud 모드의 같은 시험은 tests/test_day_sim_cloud.py).
  - 보는 사람이 환기 제안에 [그만해]를 누르면 다음 환기 제안이 나오지 않는다.
  - 임시 DB 디렉터리는 실행이 끝나면 지워진다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from liot.day_sim import (
    GAS,
    LEAK,
    DaySim,
    build_script,
    parse_day_request,
)
from liot.live_runs import LiveRequestError

Events = list[tuple[str, dict[str, Any]]]


def _run(sim: DaySim, *, on_event: Any = None, stop_after_day: int | None = None) -> Events:
    events: Events = []

    def emit(stage: str, data: dict[str, Any]) -> None:
        events.append((stage, data))
        if on_event is not None:
            on_event(sim, stage, data)

    def should_stop() -> bool:
        return stop_after_day is not None and any(
            st == "day.end" and d["day"] >= stop_after_day for st, d in events
        )

    asyncio.run(sim.run(emit, should_stop))
    return events


@pytest.fixture(scope="module")
def seed7() -> tuple[DaySim, Events]:
    sim = DaySim(seed=7, days=3)
    return sim, _run(sim)


def _comfort(sim: DaySim, **match: Any) -> list[dict[str, Any]]:
    return [p for p in sim.proposals if all(p.get(k) == v for k, v in match.items())]


# ── 결정론 ───────────────────────────────────────────────────────────────────


def test_same_seed_gives_the_same_event_list(seed7: tuple[DaySim, Events]) -> None:
    _, first = seed7
    again = _run(DaySim(seed=7, days=3))
    assert again == first
    assert _run(DaySim(seed=8, days=3)) != first


def test_script_is_deterministic_and_has_one_safety_event_per_day() -> None:
    plans = build_script(7, 3)
    assert plans == build_script(7, 3)
    assert [p.kind for p in plans] == ["home_bad_air", "out_clear", "home_cloudy_bad_air"]
    for plan in plans:
        sensor = LEAK if plan.safety == "leak" else GAS
        abnormal = [s for s in plan.steps if s.readings[sensor]]
        assert 2 <= len(abnormal) <= 4  # 30~60분 안에 정상 복귀
        assert abnormal[0].at_local.strftime("%H:%M") == plan.safety_at


# ── 학습: 싫어하는 장면이 줄어든다 ───────────────────────────────────────────


def test_purifier_reaches_the_phone_less_on_day3_than_day1(seed7: tuple[DaySim, Events]) -> None:
    sim, events = seed7
    day1 = _comfort(sim, scene="purifier", day=1, decision="IMMEDIATE")
    day3 = _comfort(sim, scene="purifier", day=3, decision="IMMEDIATE")
    assert len(day1) == 1
    assert len(day3) == 0
    # 3일차에도 나쁜 공기였다 — 줄어든 건 대본이 아니라 [그만해] 때문이다.
    plan3 = build_script(7, 3)[2]
    assert any(s.weather["pm25"] >= 36 and s.readings["binary_sensor.living_motion"]
               for s in plan3.steps)
    taps = [d for st, d in events if st == "feedback" and d["id"] == day1[0]["id"]]
    assert [(t["signal"], t["source"], t["status"]) for t in taps] == [
        ("dismissed", "persona", "200")
    ]
    assert day1[0]["state"] == "dismissed"
    # 3일차 낮: 거절된 공기청정기는 예산을 쓰지 않고 건너뛰어 흐린 낮이 그 자리를 얻는다.
    desk = _comfort(sim, scene="desk_light", day=3)
    assert len(desk) == 1 and desk[0]["matched_rules"][0] == "bad-air-purifier"


def test_all_four_scenes_arrive_within_three_days(seed7: tuple[DaySim, Events]) -> None:
    sim, _ = seed7
    arrived = {p["scene"] for p in sim.proposals if p["decision"] in ("IMMEDIATE", "DIGEST")}
    assert {"ventilate", "purifier", "bedtime", "desk_light"} <= arrived
    by_scene = {p["scene"]: p for p in sim.proposals}
    assert by_scene["ventilate"]["decision"] == "IMMEDIATE"
    assert by_scene["purifier"]["decision"] == "IMMEDIATE"
    assert by_scene["bedtime"]["decision"] == "DIGEST"
    assert by_scene["desk_light"]["decision"] == "DIGEST"
    assert all(p["reason_ko"] for p in sim.proposals if p["scene"])


def test_day_metrics_are_counted_from_records(seed7: tuple[DaySim, Events]) -> None:
    sim, events = seed7
    ends = [d["metrics"] for st, d in events if st == "day.end"]
    assert ends == sim.metrics
    for m in sim.metrics:
        day = [p for p in sim.proposals if p["day"] == m["day"]]
        pushes = [p for p in day if p["decision"] == "IMMEDIATE"]
        assert m["proposals"] == len(day)
        assert m["pushes"] == len(pushes)
        assert m["digests"] == sum(p["decision"] == "DIGEST" for p in day)
        assert m["accepted"] == sum(p["state"] == "accepted" for p in pushes)
        assert m["tray_accepted"] == sum(
            p["state"] == "accepted" and p["decision"] == "DIGEST" for p in day
        )
        assert m["dismissed"] == sum(p["state"] == "dismissed" for p in day)
        assert m["expired"] == sum(p["state"] == "expired" for p in day)
        assert m["accept_rate"] == (round(m["accepted"] / len(pushes), 3) if pushes else None)
        assert m["safety_alerts"] == 1
    assert sim.metrics[0]["pushes"] == 2  # 환기 + 공기청정기
    assert sim.metrics[0]["accept_rate"] == 0.5


def test_digest_items_are_only_tapped_at_the_22h_tray_review(
    seed7: tuple[DaySim, Events],
) -> None:
    sim, events = seed7
    digests = [p for p in sim.proposals if p["decision"] == "DIGEST"]
    persona_ids = {d["id"] for st, d in events if st == "feedback" and d["source"] == "persona"}
    assert not persona_ids & {p["id"] for p in digests}  # 알림 피드에서는 누르지 않는다
    assert {d["id"] for st, d in events if st == "digest"} == {p["id"] for p in digests}
    reviews = [d for st, d in events if st == "tray.review"]
    assert [(r["day"], r["clock_ko"]) for r in reviews] == [
        (1, "1일차 22:00"), (2, "2일차 22:00"), (3, "3일차 22:00"),
    ]
    tray_taps = [d for st, d in events if st == "feedback" and d["source"] == "tray"]
    assert tray_taps and all(t["hhmm"] == "22:00" and t["status"] == "200" for t in tray_taps)
    by_scene: dict[str, list[str]] = {}
    for p in digests:
        by_scene.setdefault(p["scene"], []).append(p["state"])
    # 취침 준비(21시, 90분 유효)는 22시 확인 때 살아 있어 수락된다. 흐린 낮(11시, 60분)은
    # 그 전에 만료돼 그대로 만료로 남는다(실제 동작).
    assert set(by_scene["bedtime"]) == {"accepted"}
    assert by_scene["desk_light"] == ["expired"]
    day3_review = reviews[2]
    assert any(i["before_ko"] == "만료됨" and i["tapped"] is None for i in day3_review["items"])


def test_bedtime_does_not_fade_by_day3(seed7: tuple[DaySim, Events]) -> None:
    sim, _ = seed7
    bedtimes = _comfort(sim, scene="bedtime")
    assert [b["day"] for b in bedtimes] == [1, 2, 3]
    scores = [b["score"] for b in bedtimes]
    assert scores == sorted(scores) and scores[-1] > scores[0]  # 모아보기 수락이 선호를 올린다


def test_events_carry_server_side_korean_labels(seed7: tuple[DaySim, Events]) -> None:
    sim, events = seed7
    for st, d in events:
        assert re.fullmatch(r"[1-3]일차 \d\d:\d\d", d["clock_ko"]), d["clock_ko"]
        if st in ("proposal", "feedback", "safety", "tray.review", "day.start", "day.end"):
            assert d["line_ko"], (st, d)
    vent = _comfort(sim, scene="ventilate")[0]
    assert vent["title_ko"] == "창문을 5~10분 열어 환기해 보세요"
    assert (vent["decision_ko"], vent["tier_ko"]) == ("바로 폰 알림", "숨결")
    assert vent["noop_score"] == 0.35 and vent["score"] > vent["noop_score"]
    assert any(c["selected"] and c["label_ko"] == vent["title_ko"] for c in vent["candidates"])
    first = next(s for s in sim.safety_events if s["kind"] == "first_alert")
    assert first["situation_ko"] and first["ntfy"]["title"] == "LIOT"
    assert first["ntfy"]["body"].endswith("제안이 도착했어요. 열어서 확인해 주세요.")
    assert first["guardian_forwarded"] is False  # 시뮬레이션 집에는 보호자가 없다
    steps = [d for st, d in events if st == "step"]
    assert steps[0]["weather_ko"].startswith("미세먼지 ")


# ── 안전: 즉시, 모델을 기다리지 않는다 ───────────────────────────────────────


def test_safety_alert_is_immediate_at_the_reading_step_before_the_model(
    seed7: tuple[DaySim, Events],
) -> None:
    sim, _ = seed7
    plans = build_script(7, 3)
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3
    previous = -1
    for plan, alert in zip(plans, firsts, strict=True):
        assert alert["decision"] == "IMMEDIATE" and alert["sent"] is True
        assert alert["tier"] == "ember"
        assert (alert["day"], alert["hhmm"]) == (plan.day, plan.safety_at)
        pid = sim._tracked[alert["id"]].pid
        sent_at = sim.log.index(("send", pid))
        sensor = LEAK if alert["category"] == "leak" else GAS
        # 이 상황의 2단계 모델 호출 = 앞 상황 뒤 처음 나오는 (그 센서, 파급도 질문) 호출.
        model_at = next(
            i for i, entry in enumerate(sim.log)
            if i > previous and entry == ("model", (sensor, True))
        )
        assert sent_at < model_at  # 첫 알림이 나간 뒤에야 2단계 모델이 돈다
        previous = model_at
        # 넓힌 순서: 첫 알림이 그 단계의 어떤 모델·외부 호출보다도 먼저다(단계 표식 사이).
        start = max(i for i, e in enumerate(sim.log[:sent_at]) if e[0] == "step")
        end = next((i for i, e in enumerate(sim.log) if i > sent_at and e[0] == "step"),
                   len(sim.log))
        calls = [i for i in range(start, end) if sim.log[i][0] in ("model", "ext")]
        assert calls and sent_at < min(calls)


def test_safety_episode_closes_and_is_acknowledged(seed7: tuple[DaySim, Events]) -> None:
    sim, events = seed7
    kinds = [s["kind"] for s in sim.safety_events]
    assert kinds.count("first_alert") == kinds.count("normal") == kinds.count("resolved") == 3
    acks = [d for st, d in events if st == "feedback" and d["kind"] == "safety"]
    assert len(acks) == 3 and all(a["signal"] == "ok" and a["status"] == "200" for a in acks)
    assert "guidance_119" not in kinds  # 다음 단계에 확인해 사다리가 멈췄다


# ── 보는 사람 응답 ───────────────────────────────────────────────────────────


def test_viewer_dismiss_on_ventilation_silences_the_next_one() -> None:
    def on_event(sim: DaySim, stage: str, data: dict[str, Any]) -> None:
        if stage == "proposal" and data["scene"] == "ventilate" and data["id"] == "p1":
            sim.enqueue_feedback("p1", "dismissed")

    sim = DaySim(seed=7, days=3)
    events = _run(sim, on_event=on_event)
    taps = [d for st, d in events if st == "feedback" and d["id"] == "p1"]
    assert [(t["source"], t["signal"], t["status"]) for t in taps] == [
        ("viewer", "dismissed", "200")
    ]  # 거주자 성향(수락)은 이미 처리된 제안을 다시 누르지 않는다
    vents = _comfort(sim, scene="ventilate")
    assert [v["id"] for v in vents] == ["p1"]
    assert vents[0]["state"] == "dismissed"


def test_viewer_tap_validation() -> None:
    sim = DaySim(seed=7, days=1)
    with pytest.raises(LiveRequestError, match="signal"):
        sim.enqueue_feedback("p1", "maybe")
    with pytest.raises(LiveRequestError, match="proposal_id"):
        sim.enqueue_feedback("../x", "accepted")


def test_viewer_tap_on_unknown_proposal_is_reported() -> None:
    def on_event(sim: DaySim, stage: str, data: dict[str, Any]) -> None:
        if stage == "day.start":
            sim.enqueue_feedback("p999", "accepted")

    events = _run(DaySim(seed=7, days=1), on_event=on_event)
    taps = [d for st, d in events if st == "feedback" and d["id"] == "p999"]
    assert taps and taps[0]["status"] == "404"


# ── 실행 수명 ────────────────────────────────────────────────────────────────


def test_temp_db_directory_is_removed(seed7: tuple[DaySim, Events]) -> None:
    sim, _ = seed7
    assert sim.tmp_dir is not None and not sim.tmp_dir.exists()


def test_stop_ends_after_the_current_step_with_a_partial_day() -> None:
    sim = DaySim(seed=7, days=3)
    events = _run(sim, stop_after_day=1)
    ends = [d for st, d in events if st == "day.end"]
    assert [e["day"] for e in ends] == [1]
    assert sim.tmp_dir is not None and not sim.tmp_dir.exists()


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"days": 0}, "days"),
        ({"days": 8}, "days"),
        ({"days": True}, "days"),
        ({"seed": "7"}, "seed"),
        ({"persona": "night_owl"}, "persona"),
        ({"pace_ms": 501}, "pace_ms"),
        ({"model": "x"}, "model"),
    ],
)
def test_day_request_is_validated(body: dict[str, Any], field: str) -> None:
    with pytest.raises(LiveRequestError) as exc:
        parse_day_request(body)
    assert exc.value.status == 400 and exc.value.field == field


def test_day_request_defaults() -> None:
    req = parse_day_request({})
    assert (req.days, req.seed, req.persona, req.pace_ms) == (3, 7, "default", 0)


@pytest.mark.parametrize("value", ["true", 1, 0, None, [True], {"on": True}])
def test_trace_request_key_must_be_a_bool(value: Any) -> None:
    with pytest.raises(LiveRequestError) as exc:
        parse_day_request({"trace": value})
    assert exc.value.status == 400 and exc.value.field == "trace"
    assert parse_day_request({"trace": True}).trace is True
    assert parse_day_request({}).trace is False
