"""실행 가능한 LIOT Phase1 데모.

세 가지 시나리오를 FakeSensorSource 로 주입해, 각 시나리오가 만드는
Proposal 의 위험도 단계(tier)·글리프·rationale·필요 확인 강도를 출력한다.

  1. 일상 조명 켜기              → ambient(잔잔 ●)
  2. 야간 thermostat 온도 내리기  → 최소 dusk(땅거미 ?) 이상
  3. 가스 누출 감지(emergency)    → ember(불씨 🛡), Safety-Never-Silent
  4. 능동 컴포트 틱               → 다중 후보(무행동 포함) 생성 → 평가·선택 →
     안전 게이트 → 제안. 후보별 점수와 선택 근거(Pipeline Trace)를 출력한다.

네트워크/HA 연결 없이 결정론적으로 동작한다.

    python -m liot.demo
"""

from __future__ import annotations

import asyncio

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import Proposal, SensorReading
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize


def _reading(
    entity_id: str,
    raw_value: object,
    *,
    ts_utc: str,
    home_id: str = "singleton",
) -> SensorReading:
    return SensorReading(
        entity_id=entity_id,
        source="fake",
        raw_value=raw_value,
        norm_value=normalize(raw_value),
        ts_utc=ts_utc,
        home_id=home_id,
    )


def _print_proposal(title: str, proposal: Proposal | None) -> None:
    print(f"\n=== {title} ===")
    if proposal is None:
        print("  (스냅샷 미생성: 의미 변화 임계 미달 → 제안 없음)")
        return
    tier = RiskTier.from_str(proposal.risk_tier)
    print(f"  tier                 : {tier.name} ({tier.label_ko}) {tier.glyph}")
    print(f"  required_confirmation: {proposal.required_confirmation}")
    print(f"  action               : {proposal.action.capability}.{proposal.action.action}"
          f" [{proposal.action.category}]")
    print(f"  rationale            : {proposal.rationale}")


def _print_candidate_trace(app: LiotApp) -> None:
    """직전 틱의 후보 평가 트레이스(다중 후보 → 점수 → 선택)를 출력한다."""
    ev = app.last_candidate_evaluation
    if ev is None:
        return
    evaluation = ev["evaluation"]
    print("  ── Pipeline Trace: 후보 생성 → 평가 → 선택 ──")
    for i, ac in enumerate(evaluation.scored):
        mark = "→" if i == evaluation.selected_index else " "
        c = ac.candidate
        origin = ac.origin or "?"
        print(
            f"   {mark} [{origin}] {c.capability}.{c.action}"
            f" score={ac.final_score:.2f}"
            f" (효과 {ac.expected_effect:.2f} · 선호 {ac.user_preference_fit:.2f}"
            f" · 안전위험 {ac.safety_risk:.2f})"
        )
    print(f"   선택 근거: {evaluation.rationale}")


async def run() -> None:
    # 야간(UTC 23시)으로 시각 고정 → night_quiet 규칙이 발동하도록.
    # climate.living_room 은 비안전 'climate' 카테고리로 두어 방향 비대칭
    # (내리기=dusk)을 보이도록 한다(기본 카탈로그의 high_temp 안전분류 오버라이드).
    night_clock = Clock(fixed="2026-06-01T23:30:00Z")
    app = LiotApp(
        clock=night_clock,
        categories={
            "light.living_room": "ambient",
            "climate.living_room": "climate",
            "switch.gas_valve": "gas",
            "binary_sensor.living_motion": "motion",  # 재실(occupancy) 유도용
        },
    )

    # 1) 일상 조명 켜기 → ambient.
    light_on = _reading("light.living_room", "on", ts_utc="2026-06-01T23:30:00Z")
    p1 = await app.process_reading(
        light_on, action="turn_on", force_snapshot=True
    )
    _print_proposal("1) 일상 조명 켜기 (ambient 기대)", p1)

    # 2) 야간 thermostat 온도 내리기 → climate set_temperature(decrease).
    #    카탈로그상 lower=dusk + 야간 규칙은 비안전 액션 하한을 끌어올린다.
    thermo = _reading(
        "climate.living_room", 19.0, ts_utc="2026-06-01T23:31:00Z"
    )
    p2 = await app.process_reading(
        thermo,
        action="set_temperature",
        params={"temperature": -2},
        force_snapshot=True,
    )
    _print_proposal("2) 야간 thermostat 온도 내리기 (>= dusk 기대)", p2)

    # 3) 가스 누출 감지 → ember (Safety-Never-Silent, LLM 무관).
    gas = _reading(
        "switch.gas_valve", "detected", ts_utc="2026-06-01T23:32:00Z"
    )
    p3 = await app.process_reading(
        gas, action="alert", force_snapshot=True
    )
    _print_proposal("3) 가스 누출 감지 (ember 기대, Safety-Never-Silent)", p3)

    # 4) 능동 컴포트 틱 — AI 가 먼저 말을 건다(다중 후보 파이프라인).
    #    관찰(자리 비움 + 조명 켜짐)은 침묵으로 흡수되고, 주기 틱에서
    #    후보 집합(규칙 후보 + 무행동)이 만들어져 평가기가 점수로 고른다.
    #    선택된 후보는 기존 Gate1+Gate2 를 그대로 통과한다(BREATH 상한, 불변식 7).
    motion_off = _reading(
        "binary_sensor.living_motion", "off", ts_utc="2026-06-01T23:33:00Z"
    )
    silent = await app.process_reading(motion_off, force_snapshot=True)
    assert silent is None  # 관찰은 침묵(센서/액추에이터 분리)
    p4 = await app.tick()
    _print_proposal("4) 능동 컴포트 틱 (자리 비움 + 조명 켜짐 → 끄기 제안)", p4)
    if p4 is not None:
        print(f"  origin               : {p4.action.params.get('__origin__')}")
        print(f"  expires_at           : {p4.expires_at} (신선도 — 만료 후 미표출)")
    _print_candidate_trace(app)

    print("\n[제안-만] 위 제안들은 자동 실행되지 않는다(실행은 옵트인·기본 OFF, 불변식 8).")
    print("[능동] 후보 생성→평가·선택→안전 게이트. 컴포트 전용 + breath 상한(불변식 7),")
    print("       무행동이 이기면 침묵. 개입 방식(조건 A/B)은 L4 에서만 갈린다.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
