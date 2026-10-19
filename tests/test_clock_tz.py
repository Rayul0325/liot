"""집 시간대(Clock tz) — 시간대 버킷과 능동 예산의 '하루'를 집 지역 시각으로 센다.

핵심 검증:
  - KST 07:30(= 22:30Z)은 아침, KST 23:00(= 14:00Z)은 밤이다. 경계(06·11·17·22시)도 KST.
  - 타임스탬프(now_utc·now_dt)는 tz 와 무관하게 UTC 그대로다.
  - KST 하루 안에서는 UTC 날짜가 바뀌어도 시간대 예산·하루 예산이 한 번만 쓰인다.
  - tz 를 주지 않으면 예전과 똑같다(UTC 시·UTC 날짜).
  - 데몬 진입점의 LIOT_HOME_TZ: 기본 Asia/Seoul, 모르는 이름은 한국어 ValueError.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from liot.core.clock import Clock, resolve_tz
from liot.layers.l2_context.proactive import ProactiveGenerator, ProactiveState
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.serve import DEFAULT_HOME_TZ, resolve_home_tz

KST = "Asia/Seoul"
CUR = {"light.living_room": True}


def _rules_yaml(match: str, *, per_bucket: int, per_day: int) -> str:
    return f"""
version: "1.0.0"
name: tz
limits: {{cooldown_minutes: 1, max_per_bucket: {per_bucket}, max_per_day: {per_day}}}
rules:
  - id: light-off
    match: {match}
    state: [{{entity: light.living_room, op: eq, value: true}}]
    propose: {{target: light.living_room, capability: light, action: turn_off}}
    ttl_minutes: 15
"""


def _generator(tmp_path, clock: Clock, text: str) -> ProactiveGenerator:
    path = tmp_path / "tz.yaml"
    path.write_text(text, encoding="utf-8")
    return ProactiveGenerator(
        ruleset=load_proactive_ruleset(path),
        catalog=load_device_catalog(),
        categories={"light.living_room": "light"},
        state=ProactiveState(),
        clock=clock,
    )


# ── 시간대 버킷 ──────────────────────────────────────────────────────────────


def test_kst_0730_is_morning_while_utc_reads_night() -> None:
    at = "2026-06-01T22:30:00+00:00"  # KST 06-02 07:30
    assert Clock(fixed=at, tz=KST).time_bucket() == "morning"
    assert Clock(fixed=at).time_bucket() == "night"


def test_kst_2300_is_night_while_utc_reads_day() -> None:
    at = "2026-06-01T14:00:00+00:00"  # KST 23:00
    assert Clock(fixed=at, tz=KST).time_bucket() == "night"
    assert Clock(fixed=at).time_bucket() == "day"


@pytest.mark.parametrize(
    ("kst_hhmm", "bucket"),
    [
        ("05:59", "night"),
        ("06:00", "morning"),
        ("10:59", "morning"),
        ("11:00", "day"),
        ("16:59", "day"),
        ("17:00", "evening"),
        ("21:59", "evening"),
        ("22:00", "night"),
    ],
)
def test_bucket_edges_are_kst_hours(kst_hhmm: str, bucket: str) -> None:
    clock = Clock(tz=KST)
    assert clock.time_bucket(f"2026-06-02T{kst_hhmm}:00+09:00") == bucket


def test_timestamps_stay_utc_with_tz() -> None:
    clock = Clock(fixed="2026-06-01T22:30:00+00:00", tz=KST)
    assert clock.now_utc() == "2026-06-01T22:30:00+00:00"
    assert clock.now_dt() == datetime(2026, 6, 1, 22, 30, tzinfo=UTC)
    local = clock.local_dt()
    assert (local.date().isoformat(), local.hour, local.minute) == ("2026-06-02", 7, 30)
    assert local == clock.now_dt()  # 같은 순간


def test_no_tz_is_utc_for_every_hour() -> None:
    expected = ["night"] * 6 + ["morning"] * 5 + ["day"] * 6 + ["evening"] * 5 + ["night"] * 2
    clock = Clock()
    got = [clock.time_bucket(f"2026-06-02T{h:02d}:15:00+00:00") for h in range(24)]
    assert got == expected
    assert clock.local_dt("2026-06-02T23:30:00+00:00").date().isoformat() == "2026-06-02"


def test_unknown_tz_is_a_korean_value_error() -> None:
    with pytest.raises(ValueError, match="알 수 없는 시간대"):
        Clock(tz="Mars/Olympus")
    with pytest.raises(ValueError, match="알 수 없는 시간대"):
        resolve_tz("")


# ── 능동 예산의 '하루' ───────────────────────────────────────────────────────


def test_bucket_budget_fires_once_per_kst_morning_across_utc_midnight(tmp_path) -> None:
    clock = Clock(fixed="2026-06-01T23:30:00+00:00", tz=KST)  # KST 06-02 08:30
    gen = _generator(tmp_path, clock, _rules_yaml("{time_bucket: morning}", per_bucket=1,
                                                  per_day=10))
    assert gen.propose(CUR).candidate is not None
    clock.set_fixed("2026-06-02T00:30:00+00:00")  # KST 06-02 09:30 — UTC 날짜만 바뀜
    second = gen.propose(CUR)
    assert second.candidate is None
    assert second.suppressed_by == "budget"
    clock.set_fixed("2026-06-02T23:30:00+00:00")  # KST 06-03 08:30 — 다음 날 아침
    assert gen.propose(CUR).candidate is not None


def test_day_budget_counts_the_kst_day_across_utc_midnight(tmp_path) -> None:
    clock = Clock(fixed="2026-06-01T23:30:00+00:00", tz=KST)  # KST 06-02 08:30 아침
    gen = _generator(tmp_path, clock, _rules_yaml("{occupancy: unknown}", per_bucket=5,
                                                  per_day=1))
    assert gen.propose(CUR).candidate is not None
    clock.set_fixed("2026-06-02T04:00:00+00:00")  # KST 06-02 13:00 낮 — 같은 KST 날
    later = gen.propose(CUR)
    assert later.candidate is None
    assert later.suppressed_by == "budget"
    clock.set_fixed("2026-06-02T15:30:00+00:00")  # KST 06-03 00:30 — 다음 날
    assert gen.propose(CUR).candidate is not None


def test_no_tz_budget_still_resets_at_utc_midnight(tmp_path) -> None:
    clock = Clock(fixed="2026-06-01T23:30:00+00:00")  # UTC night
    gen = _generator(tmp_path, clock, _rules_yaml("{time_bucket: night}", per_bucket=1,
                                                  per_day=10))
    assert gen.propose(CUR).candidate is not None
    clock.set_fixed("2026-06-01T23:45:00+00:00")  # 같은 UTC 날 밤
    assert gen.propose(CUR).suppressed_by == "budget"
    clock.set_fixed("2026-06-02T00:30:00+00:00")  # UTC 날짜가 바뀜 → 예전처럼 새 하루
    assert gen.propose(CUR).candidate is not None


# ── 데몬 진입점 ──────────────────────────────────────────────────────────────


def test_daemon_home_tz_defaults_to_seoul() -> None:
    assert DEFAULT_HOME_TZ == "Asia/Seoul"
    assert resolve_home_tz(None) == "Asia/Seoul"
    assert resolve_home_tz("   ") == "Asia/Seoul"
    assert resolve_home_tz(" Europe/Berlin ") == "Europe/Berlin"


def test_daemon_home_tz_rejects_unknown_name_loudly() -> None:
    with pytest.raises(ValueError, match="LIOT_HOME_TZ 를 알 수 없습니다"):
        resolve_home_tz("Seoul")
