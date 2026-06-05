"""실행 가능한 LIOT Phase1 데모.

세 가지 시나리오를 FakeSensorSource 로 주입해, 각 시나리오가 만드는
Proposal 의 위험도 단계(tier)·글리프·rationale·필요 확인 강도를 출력한다.

  1. 일상 조명 켜기              → ambient(잔잔 ●)
  2. 야간 thermostat 온도 내리기  → 최소 dusk(땅거미 ?) 이상
  3. 가스 누출 감지(emergency)    → ember(불씨 🛡), Safety-Never-Silent
  4. 능동 컴포트 틱(Phase2)       → AI 가 먼저 "자리 비움 + 조명 켜짐 → 끄기" 제안

네트워크/HA 연결 없이 결정론적으로 동작한다.

    python -m liot.demo
"""

from __future__ import annotations

import asyncio

from liot.core.clock import Clock
from liot.core.contracts import Proposal, SensorReading
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize
from liot.app import LiotApp


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

    # 4) 능동 컴포트 틱(Phase2) — AI 가 먼저 말을 건다.
    #    관찰(자리 비움 + 조명 켜짐)은 침묵으로 흡수되고, 주기 틱에서
    #    능동 규칙(vacant-light-off)이 '조명 끄기'를 발의한다. 후보는
    #    기존 Gate1+Gate2 를 그대로 통과한다(BREATH 상한, 불변식 7).
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

    print("\n[Phase1] 위 제안들은 결코 자동 실행되지 않는다(제안-만 모델).")
    print("[Phase2] 능동 제안은 컴포트 전용 + breath 상한(불변식 7) — 초과는 침묵.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
