"""정합성 수정 검증 — 외기→snapshot_hash 배선 + candidate_evaluations 영속.

리뷰에서 확인된 두 결손을 고정한다:
  1) tick 스냅샷 해시가 외부 맥락(ExternalContext)을 포함한다. 외기가 후보
     선택을 바꿀 수 있으므로, 해시에 없으면 재현성(불변식 4)이 깨진다.
  2) candidate_evaluations 테이블에 실제 writer 가 존재한다 — 서비스 run_tick
     이 후보 평가(무행동 선택 포함)를 영속하고, 거절 이력 스냅샷이 평가기
     선호 신호로 주입돼 같은 류의 재제안을 침묵시킨다.
"""

from __future__ import annotations

import json

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"  # time_bucket 'day'
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
}
# cooldown 0 — 거절 이력 테스트에서 두 번째 틱이 쿨다운이 아니라
# 선호 감점 때문에 침묵함을 구분하기 위해.
COMFORT_YAML = """
version: "1.0.0"
name: t
limits: {cooldown_minutes: 0, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid,
        source="test",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc=DAY,
        home_id="singleton",
    )


def _app(tmp_path, *, external=None) -> LiotApp:
    clock = Clock(fixed=DAY)
    p = tmp_path / "t.yaml"
    p.write_text(COMFORT_YAML, encoding="utf-8")
    gen = ProactiveGenerator(
        ruleset=load_proactive_ruleset(p),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=clock,
    )
    return LiotApp(
        clock=clock, categories=CATS, proactive=gen, external_provider=external
    )


async def _seed_vacant_light_on(app: LiotApp) -> None:
    for eid, raw in [
        ("binary_sensor.living_motion", "off"),
        ("light.living_room", "on"),
    ]:
        assert await app.process_reading(_reading(eid, raw), force_snapshot=True) is None


# ==========================================================================
# 1) 외기 → snapshot_hash
# ==========================================================================
async def _tick_hash(tmp_path, aqi: int) -> str:
    app = _app(
        tmp_path,
        external=FakeExternalProvider(clock=Clock(fixed=DAY), outdoor_aqi=aqi),
    )
    await _seed_vacant_light_on(app)
    proposal = await app.tick()
    assert proposal is not None  # 조명 후보는 AQI 무관 — 제안은 유지
    ev = app.last_candidate_evaluation
    assert ev is not None and ev["context_snapshot_hash"]
    return ev["context_snapshot_hash"]


async def test_external_context_changes_tick_snapshot_hash(tmp_path) -> None:
    # 외기가 다르면 스냅샷 해시가 달라야 한다(선택 근거가 해시에 고정).
    h_good = await _tick_hash(tmp_path, aqi=20)
    h_bad = await _tick_hash(tmp_path, aqi=180)
    assert h_good != h_bad


async def test_same_external_reproduces_same_snapshot_hash(tmp_path) -> None:
    # 같은 상태 + 같은 외기 = 같은 해시(불변식 4 재현성).
    assert await _tick_hash(tmp_path, aqi=20) == await _tick_hash(tmp_path, aqi=20)


async def test_no_external_provider_keeps_hash_stable(tmp_path) -> None:
    # provider 미설정(기존 경로)은 외기 키 자체가 없다 — 기존 동작 보존.
    app = _app(tmp_path)
    await _seed_vacant_light_on(app)
    assert await app.tick() is not None
    ev = app.last_candidate_evaluation
    assert ev is not None and ev["context_snapshot_hash"]


# ==========================================================================
# 2) candidate_evaluations 영속 + 3) 거절 이력 → 침묵
# ==========================================================================
async def _fetch_evals(db):
    cur = await db.execute(
        "SELECT trace_id, scored_json, selected_index, rationale "
        "FROM candidate_evaluations ORDER BY created_at, trace_id"
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def test_run_tick_persists_candidate_evaluation(tmp_path) -> None:
    db = await init_db(":memory:")
    try:
        app = _app(tmp_path)
        svc = LiotService(app, db, clock=app.clock)
        await _seed_vacant_light_on(app)
        res = await svc.run_tick()
        assert res is not None

        rows = await _fetch_evals(db)
        assert len(rows) == 1
        scored = json.loads(rows[0]["scored_json"])
        # 무행동 후보가 항상 기록에 포함된다.
        assert any(a["candidate"]["capability"] == "noop" for a in scored)
        sel = scored[rows[0]["selected_index"]]
        assert sel["candidate"]["capability"] == "light"
        assert rows[0]["rationale"]
        # 앱 쪽 노출분은 기록 후 소거된다(중복 기록 방지).
        assert app.last_candidate_evaluation is None
    finally:
        await db.close()


async def test_rejected_history_silences_and_records_noop(tmp_path) -> None:
    db = await init_db(":memory:")
    try:
        app = _app(tmp_path)
        svc = LiotService(app, db, clock=app.clock)
        await _seed_vacant_light_on(app)

        # 1틱: 제안 생성 → 사용자가 거절(dismissed).
        res1 = await svc.run_tick()
        assert res1 is not None
        await svc.proposals.set_state(res1.proposal.proposal_id, "dismissed")

        # 2틱: 쿨다운 0 이므로 규칙은 다시 매칭되지만, 거절 이력 스냅샷이
        # 선호를 감점해 평가기가 무행동을 고른다 → 침묵.
        res2 = await svc.run_tick()
        assert res2 is None

        rows = await _fetch_evals(db)
        assert len(rows) == 2  # 무행동 선택도 기록된다(연구 로그)
        assert len({r["trace_id"] for r in rows}) == 2  # 틱마다 별개 trace
        selected_caps = sorted(
            json.loads(r["scored_json"])[r["selected_index"]]["candidate"]["capability"]
            for r in rows
        )
        assert selected_caps == ["light", "noop"]
    finally:
        await db.close()
