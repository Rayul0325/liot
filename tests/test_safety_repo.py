"""SafetyRepo — 거주자·보호자·동의·상황·에스컬레이션·전달 기록·판단 저장.

하드 게이트:
  - 거주자 특성은 이 기기 DB 에만 저장된다(여기서는 저장만).
  - 거주자 삭제는 특성 값을 지운다(과거 기록엔 익명 사실만).
  - 동의 기록은 덧붙이기만 된다(UPDATE/DELETE 거부).
  - cognitive_decline 은 entered_by='guardian' 만 받는다.
"""

from __future__ import annotations

import sqlite3

import pytest

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Judgment
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import connect, init_db
from liot.db.safety_repo import (
    PERSON_TRAITS,
    SafetyRepo,
)

NOW = "2026-06-01T12:00:00+00:00"
H = "home-a"
OTHER = "home-b"


@pytest.fixture
def repo(tmp_db) -> SafetyRepo:
    return SafetyRepo(tmp_db, clock=Clock(fixed=NOW))


# ── 거주자 ──────────────────────────────────────────────────────────────
async def test_add_get_list_resident(repo: SafetyRepo) -> None:
    rid = await repo.add_resident(H, "할머니")
    await repo.add_resident(OTHER, "다른 집")
    r = await repo.get_resident(rid)
    assert r is not None
    assert (r.resident_id, r.home_id, r.alias) == (rid, H, "할머니")
    assert r.created_at == NOW and r.updated_at == NOW
    assert r.traits == {}
    listed = await repo.list_residents(H)
    assert [x.resident_id for x in listed] == [rid]
    assert await repo.get_resident("nope") is None


async def test_add_resident_rejects_blank_home_or_alias(repo: SafetyRepo) -> None:
    with pytest.raises(ValueError):
        await repo.add_resident("  ", "x")
    with pytest.raises(ValueError):
        await repo.add_resident(H, "  ")


async def test_set_trait_roundtrip_and_update(repo: SafetyRepo) -> None:
    rid = await repo.add_resident(H, "a")
    await repo.set_trait(rid, "age_75_plus", True, "resident")
    await repo.set_trait(rid, "cannot_self_evacuate", True, "guardian")
    await repo.set_trait(rid, "age_75_plus", False, "guardian")  # 갱신
    r = await repo.get_resident(rid)
    assert r is not None
    assert r.traits["age_75_plus"].value is False
    assert r.traits["age_75_plus"].entered_by == "guardian"
    assert r.traits["cannot_self_evacuate"].value is True
    assert set(PERSON_TRAITS) == {"age_75_plus", "cannot_self_evacuate", "cognitive_decline"}


async def test_set_trait_rejects_unknown_trait_and_entered_by(repo: SafetyRepo) -> None:
    rid = await repo.add_resident(H, "a")
    with pytest.raises(ValueError):
        await repo.set_trait(rid, "birth_date", True, "resident")
    with pytest.raises(ValueError):
        await repo.set_trait(rid, "diagnosis", True, "guardian")
    with pytest.raises(ValueError):
        # 가구 플래그는 set_lives_alone 로만.
        await repo.set_trait(rid, "lives_alone", True, "resident")
    with pytest.raises(ValueError):
        await repo.set_trait(rid, "age_75_plus", True, "doctor")
    with pytest.raises(LookupError):
        await repo.set_trait("ghost", "age_75_plus", True, "resident")


@pytest.mark.parametrize("entered_by", ["resident", "doctor", "", "GUARDIAN"])
async def test_cognitive_decline_only_from_guardian(repo: SafetyRepo, entered_by: str) -> None:
    rid = await repo.add_resident(H, "a")
    with pytest.raises(ValueError):
        await repo.set_trait(rid, "cognitive_decline", True, entered_by)
    r = await repo.get_resident(rid)
    assert r is not None and "cognitive_decline" not in r.traits


async def test_cognitive_decline_accepted_from_guardian(repo: SafetyRepo) -> None:
    rid = await repo.add_resident(H, "a")
    await repo.set_trait(rid, "cognitive_decline", True, "guardian")
    r = await repo.get_resident(rid)
    assert r is not None
    assert r.traits["cognitive_decline"].value is True
    assert r.traits["cognitive_decline"].entered_by == "guardian"


async def test_trait_never_survives_delete_resident(tmp_path) -> None:
    path = str(tmp_path / "purge.db")
    db = await init_db(path)
    try:
        repo = SafetyRepo(db, clock=Clock(fixed=NOW))
        rid = await repo.add_resident(H, "ZZ-purge-alias-ZZ")
        keep = await repo.add_resident(H, "남는 사람")
        for t in PERSON_TRAITS:
            await repo.set_trait(rid, t, True, "guardian")
        await repo.set_trait(keep, "age_75_plus", True, "resident")
        assert await repo.delete_resident(rid) is True
        assert await repo.get_resident(rid) is None
        cur = await db.execute(
            "SELECT COUNT(*) FROM resident_traits WHERE resident_id = ?", (rid,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0
        cur = await db.execute("SELECT COUNT(*) FROM residents WHERE resident_id = ?", (rid,))
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0
        # 다른 거주자의 특성은 그대로.
        other = await repo.get_resident(keep)
        assert other is not None and other.traits["age_75_plus"].value is True
        # 연결이 열린 채로도 DB·WAL 파일 바이트에 지운 별칭이 남지 않는다
        # (secure_delete + 체크포인트는 delete_resident 가 직접 한다).
        for suffix in ("", "-wal"):
            try:
                with open(path + suffix, "rb") as fh:
                    assert b"ZZ-purge-alias-ZZ" not in fh.read(), suffix
            except FileNotFoundError:
                pass
        # secure_delete 는 원래 값으로 돌아온다(연결 전체 성능 설정을 바꾸지 않는다).
        cur = await db.execute("PRAGMA secure_delete")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0
        # 두 번째 삭제는 False(없음).
        assert await repo.delete_resident(rid) is False
    finally:
        await db.close()


async def test_trait_and_flag_values_must_be_bool(repo: SafetyRepo) -> None:
    rid = await repo.add_resident(H, "a")
    with pytest.raises(ValueError):
        await repo.set_trait(rid, "age_75_plus", 1948, "resident")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await repo.set_lives_alone(H, "yes")  # type: ignore[arg-type]


async def test_has_vulnerable_resident(repo: SafetyRepo) -> None:
    assert await repo.has_vulnerable_resident(H) is False
    rid = await repo.add_resident(H, "a")
    await repo.set_trait(rid, "age_75_plus", False, "resident")
    assert await repo.has_vulnerable_resident(H) is False
    await repo.set_trait(rid, "cannot_self_evacuate", True, "resident")
    assert await repo.has_vulnerable_resident(H) is True
    assert await repo.has_vulnerable_resident(OTHER) is False
    await repo.delete_resident(rid)
    assert await repo.has_vulnerable_resident(H) is False
    await repo.set_lives_alone(H, True)
    assert await repo.has_vulnerable_resident(H) is True
    await repo.set_lives_alone(H, False)
    assert await repo.has_vulnerable_resident(H) is False


@pytest.mark.parametrize("trait", ["age_75_plus", "cannot_self_evacuate", "cognitive_decline"])
async def test_each_person_trait_marks_vulnerable(repo: SafetyRepo, trait: str) -> None:
    rid = await repo.add_resident(H, "a")
    await repo.set_trait(rid, trait, True, "guardian")
    assert await repo.has_vulnerable_resident(H) is True


# ── 집 설정 ─────────────────────────────────────────────────────────────
async def test_home_settings_defaults_and_updates(repo: SafetyRepo) -> None:
    s = await repo.get_home_settings(H)
    assert tuple(s) == (False, "우리 집")
    assert s.lives_alone is False and s.home_alias == "우리 집"
    await repo.set_lives_alone(H, True)
    assert tuple(await repo.get_home_settings(H)) == (True, "우리 집")
    await repo.set_home_alias(H, "  본가  ")
    assert tuple(await repo.get_home_settings(H)) == (True, "본가")
    await repo.set_home_alias(OTHER, "다른 집")
    assert tuple(await repo.get_home_settings(OTHER)) == (False, "다른 집")
    with pytest.raises(ValueError):
        await repo.set_home_alias(H, "   ")


# ── 보호자 ──────────────────────────────────────────────────────────────
async def test_guardians_add_list_deactivate(repo: SafetyRepo) -> None:
    g1, topic1 = await repo.add_guardian(H, "딸")
    g2, topic2 = await repo.add_guardian(H, "아들")
    await repo.add_guardian(OTHER, "남")
    assert topic1 != topic2 and len(topic1) >= 32
    gs = await repo.list_guardians(H)
    assert {g.guardian_id for g in gs} == {g1, g2}
    by_id = {g.guardian_id: g for g in gs}
    assert by_id[g1].topic == topic1 and by_id[g1].active is True
    # 비밀 토픽은 repr(로그)에 나오지 않는다.
    assert topic1 not in repr(by_id[g1])
    assert await repo.deactivate_guardian(H, g1) is True
    assert [g.guardian_id for g in await repo.list_guardians(H)] == [g2]
    all_g = await repo.list_guardians(H, active_only=False)
    assert {g.guardian_id for g in all_g} == {g1, g2}
    # 다른 집 id 로는 비활성화되지 않는다.
    assert await repo.deactivate_guardian(OTHER, g2) is False


# ── 동의 ────────────────────────────────────────────────────────────────
async def test_consent_grant_by_a_revoke_by_b_stops(repo: SafetyRepo) -> None:
    a = await repo.add_resident(H, "a")
    b = await repo.add_resident(H, "b")
    g, _ = await repo.add_guardian(H, "딸")
    assert await repo.consented_guardians(H) == []
    await repo.grant_consent(H, g, a)
    assert [x.guardian_id for x in await repo.consented_guardians(H)] == [g]
    await repo.revoke_consent(H, g, b)
    assert await repo.consented_guardians(H) == []
    await repo.grant_consent(H, g, b)
    assert [x.guardian_id for x in await repo.consented_guardians(H)] == [g]
    hist = await repo.consent_history(H)
    assert [(e.event, e.by_resident_id) for e in hist] == [
        ("grant", a),
        ("revoke", b),
        ("grant", b),
    ]
    assert all(e.guardian_id == g and e.home_id == H for e in hist)


async def test_consent_history_is_append_only(repo: SafetyRepo, tmp_db) -> None:
    a = await repo.add_resident(H, "a")
    g, _ = await repo.add_guardian(H, "딸")
    await repo.grant_consent(H, g, a)
    with pytest.raises(sqlite3.DatabaseError):
        await tmp_db.execute("UPDATE consent_events SET event = 'revoke'")
    with pytest.raises(sqlite3.DatabaseError):
        await tmp_db.execute("DELETE FROM consent_events")
    await tmp_db.rollback()
    assert [e.event for e in await repo.consent_history(H)] == ["grant"]


async def test_non_resident_cannot_grant_or_revoke(repo: SafetyRepo) -> None:
    a = await repo.add_resident(H, "a")
    outsider = await repo.add_resident(OTHER, "x")
    g, _ = await repo.add_guardian(H, "딸")
    with pytest.raises(PermissionError):
        await repo.grant_consent(H, g, outsider)
    with pytest.raises(PermissionError):
        await repo.grant_consent(H, g, "ghost")
    await repo.grant_consent(H, g, a)
    with pytest.raises(PermissionError):
        await repo.revoke_consent(H, g, outsider)
    assert [e.event for e in await repo.consent_history(H)] == ["grant"]


async def test_consent_rejects_guardian_of_other_home(repo: SafetyRepo) -> None:
    a = await repo.add_resident(H, "a")
    foreign, _ = await repo.add_guardian(OTHER, "남")
    with pytest.raises(LookupError):
        await repo.grant_consent(H, foreign, a)
    with pytest.raises(LookupError):
        await repo.revoke_consent(H, foreign, a)


async def test_inactive_guardian_cannot_be_granted_but_can_be_revoked(repo: SafetyRepo) -> None:
    a = await repo.add_resident(H, "a")
    g, _ = await repo.add_guardian(H, "딸")
    await repo.grant_consent(H, g, a)
    await repo.deactivate_guardian(H, g)
    # 비활성 보호자는 동의가 있어도 전달 대상이 아니다.
    assert await repo.consented_guardians(H) == []
    with pytest.raises(ValueError):
        await repo.grant_consent(H, g, a)
    await repo.revoke_consent(H, g, a)
    assert [e.event for e in await repo.consent_history(H)] == ["grant", "revoke"]


async def test_resident_with_guardian_entered_cognitive_decline_can_revoke(
    repo: SafetyRepo,
) -> None:
    a = await repo.add_resident(H, "a")
    b = await repo.add_resident(H, "b")
    await repo.set_trait(b, "cognitive_decline", True, "guardian")
    g, _ = await repo.add_guardian(H, "딸")
    await repo.grant_consent(H, g, a)
    await repo.revoke_consent(H, g, b)
    assert await repo.consented_guardians(H) == []


async def test_consent_history_survives_resident_delete(repo: SafetyRepo) -> None:
    a = await repo.add_resident(H, "a")
    g, _ = await repo.add_guardian(H, "딸")
    await repo.grant_consent(H, g, a)
    await repo.delete_resident(a)
    hist = await repo.consent_history(H)
    assert [(e.event, e.by_resident_id) for e in hist] == [("grant", a)]


# ── 상황 ────────────────────────────────────────────────────────────────
async def test_one_open_situation_per_entity(repo: SafetyRepo) -> None:
    sid, new = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.WARNING, NOW)
    assert new is True
    sid2, new2 = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.NOTICE, NOW)
    assert (sid2, new2) == (sid, False)
    # 다른 엔티티/카테고리/집은 별개 에피소드.
    s3, n3 = await repo.open_or_get_situation(H, "gas", "s.gas2", SeverityLevel.NOTICE, NOW)
    s4, n4 = await repo.open_or_get_situation(OTHER, "gas", "s.gas", SeverityLevel.NOTICE, NOW)
    assert n3 and n4 and len({sid, s3, s4}) == 3
    sit = await repo.get_open_situation(H, "gas", "s.gas")
    assert sit is not None
    assert sit.situation_id == sid and sit.closed_at is None
    # 하한은 내려가지 않는다(승격만).
    assert sit.severity_floor == SeverityLevel.WARNING
    await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.CRITICAL, NOW)
    sit = await repo.get_open_situation(H, "gas", "s.gas")
    assert sit is not None and sit.severity_floor == SeverityLevel.CRITICAL


async def test_db_enforces_single_open_episode(repo: SafetyRepo, tmp_db) -> None:
    await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.WARNING, NOW)
    with pytest.raises(sqlite3.IntegrityError):
        await tmp_db.execute(
            "INSERT INTO situations (situation_id, home_id, category, entity_id, opened_at,"
            " severity_floor) VALUES ('dup', ?, 'gas', 's.gas', ?, 2)",
            (H, NOW),
        )
    await tmp_db.rollback()


async def test_close_situation_then_reopen_new_episode(repo: SafetyRepo) -> None:
    sid, _ = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.WARNING, NOW)
    assert await repo.close_situation(sid, "2026-06-01T12:10:00+00:00") is True
    assert await repo.get_open_situation(H, "gas", "s.gas") is None
    assert await repo.close_situation(sid, "2026-06-01T12:11:00+00:00") is False
    sid2, new = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.NOTICE, NOW)
    assert new is True and sid2 != sid


# ── 에스컬레이션 사다리 ──────────────────────────────────────────────────
async def test_ladder_create_update_get(repo: SafetyRepo) -> None:
    sid, _ = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.CRITICAL, NOW)
    await repo.create_ladder(sid, 0, "2026-06-01T12:01:00+00:00")
    lad = await repo.get_ladder(sid)
    assert lad is not None
    assert (lad.situation_id, lad.home_id, lad.stage) == (sid, H, 0)
    assert lad.next_fire_at == "2026-06-01T12:01:00+00:00"
    assert lad.notified_guardian_ids == ()
    assert (lad.acked, lad.acked_by, lad.acked_at) == (False, None, None)
    assert (lad.guidance_119_sent, lad.cancelled, lad.closed) == (False, False, False)
    assert lad.updated_at == NOW
    await repo.update_ladder(
        sid,
        stage=1,
        next_fire_at=None,
        notified_guardian_ids=["g1", "g2"],
        acked=True,
        acked_by="g1",
        acked_at="2026-06-01T12:02:00+00:00",
        guidance_119_sent=True,
    )
    lad = await repo.get_ladder(sid)
    assert lad is not None
    assert lad.stage == 1 and lad.next_fire_at is None
    assert lad.notified_guardian_ids == ("g1", "g2")
    assert lad.acked is True and lad.acked_by == "g1"
    assert lad.guidance_119_sent is True
    assert await repo.get_ladder("nope") is None


async def test_ladder_rejects_unknown_fields_and_missing_situation(repo: SafetyRepo) -> None:
    with pytest.raises(LookupError):
        await repo.create_ladder("ghost", 0, None)
    sid, _ = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.CRITICAL, NOW)
    await repo.create_ladder(sid, 0, None)
    with pytest.raises(ValueError):
        await repo.update_ladder(sid, home_id="other")
    with pytest.raises(ValueError):
        await repo.update_ladder(sid, notified_guardian_ids="g1")
    with pytest.raises(ValueError):
        await repo.update_ladder(sid, acked="yes")
    with pytest.raises(ValueError):
        await repo.create_ladder(sid, 1, None)  # 상황당 하나
    with pytest.raises(LookupError):
        await repo.update_ladder("ghost", stage=2)


async def test_open_ladders_survive_restart(tmp_path) -> None:
    path = str(tmp_path / "ladder.db")
    db = await init_db(path)
    repo = SafetyRepo(db, clock=Clock(fixed=NOW))
    s_open, _ = await repo.open_or_get_situation(H, "gas", "a", SeverityLevel.CRITICAL, NOW)
    s_acked, _ = await repo.open_or_get_situation(H, "gas", "b", SeverityLevel.CRITICAL, NOW)
    s_closed, _ = await repo.open_or_get_situation(H, "gas", "c", SeverityLevel.CRITICAL, NOW)
    s_cancel, _ = await repo.open_or_get_situation(H, "gas", "d", SeverityLevel.CRITICAL, NOW)
    s_other, _ = await repo.open_or_get_situation(OTHER, "gas", "a", SeverityLevel.WARNING, NOW)
    for s in (s_open, s_acked, s_closed, s_cancel, s_other):
        await repo.create_ladder(s, 1, "2026-06-01T12:05:00+00:00")
    await repo.update_ladder(s_open, notified_guardian_ids=["g1"])
    await repo.update_ladder(s_acked, acked=True, acked_by="g1", acked_at=NOW)
    await repo.update_ladder(s_closed, closed=True)
    await repo.update_ladder(s_cancel, cancelled=True)
    await db.close()

    db2 = await connect(path)
    try:
        repo2 = SafetyRepo(db2, clock=Clock(fixed=NOW))
        ladders = await repo2.list_open_ladders()
        assert {lad.situation_id for lad in ladders} == {s_open, s_other}
        by_id = {lad.situation_id: lad for lad in ladders}
        assert by_id[s_open].notified_guardian_ids == ("g1",)
        assert by_id[s_open].stage == 1
        assert by_id[s_other].home_id == OTHER
    finally:
        await db2.close()


# ── 보호자 전달 기록 ─────────────────────────────────────────────────────
async def test_record_and_list_forwards(repo: SafetyRepo) -> None:
    g, _ = await repo.add_guardian(H, "딸")
    sid, _ = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.CRITICAL, NOW)
    osid, _ = await repo.open_or_get_situation(OTHER, "gas", "s.gas", SeverityLevel.NOTICE, NOW)
    await repo.record_forward(sid, g, "forward", "2026-06-01T12:01:00+00:00")
    await repo.record_forward(sid, None, "guidance_119", "2026-06-01T12:02:00+00:00")
    await repo.record_forward(sid, g, "resolved", "2026-06-01T12:03:00+00:00")
    og, _ = await repo.add_guardian(OTHER, "남")
    await repo.record_forward(osid, og, "forward", NOW)
    fw = await repo.list_forwards(H)
    assert [(f.situation_id, f.guardian_id, f.kind) for f in fw] == [
        (sid, g, "forward"),
        (sid, None, "guidance_119"),
        (sid, g, "resolved"),
    ]
    assert all(f.home_id == H for f in fw)
    assert fw[0].at == "2026-06-01T12:01:00+00:00"


async def test_record_forward_validation(repo: SafetyRepo) -> None:
    g, _ = await repo.add_guardian(H, "딸")
    og, _ = await repo.add_guardian(OTHER, "남")
    sid, _ = await repo.open_or_get_situation(H, "gas", "s.gas", SeverityLevel.CRITICAL, NOW)
    with pytest.raises(ValueError):
        await repo.record_forward(sid, g, "sms", NOW)
    with pytest.raises(ValueError):
        await repo.record_forward(sid, None, "forward", NOW)
    with pytest.raises(ValueError):
        await repo.record_forward(sid, g, "guidance_119", NOW)
    with pytest.raises(LookupError):
        await repo.record_forward("ghost", g, "forward", NOW)
    with pytest.raises(LookupError):
        await repo.record_forward(sid, og, "forward", NOW)
    assert await repo.list_forwards(H) == []


# ── 판단 저장 ───────────────────────────────────────────────────────────
def _judgment(**over: object) -> Judgment:
    base: dict[str, object] = {
        "judgment_id": "j-1",
        "trace_id": "t-1",
        "home_id": H,
        "snapshot_hash": "h" * 8,
        "action": CandidateAction(
            capability="valve", action="close", params={"x": 1},
            entity_id="switch.gas_valve", category="gas",
        ),
        "risk_tier": RiskTier.EMBER,
        "confidence": 0.9,
        "rationale": "가스 감지",
        "gate1_verdict": "gate1:ok",
        "gate2_verdict": "gate2:agree",
        "bundle_id": "b-1",
        "model_id": "m-1",
        "degraded": True,
        "severity": SeverityLevel.CRITICAL,
        "severity_floor": SeverityLevel.WARNING,
        "severity_sources": ["table", "resident"],
        "situation_id": "sit-1",
        "model_probs": {"tier": {"ember": 0.7, "breath": 0.3}, "severity": {"critical": 1.0}},
    }
    base.update(over)
    return Judgment.model_validate(base)


async def test_save_judgment_roundtrips_new_fields(repo: SafetyRepo, tmp_db) -> None:
    j = _judgment()
    await repo.save_judgment(j)
    back = await repo.get_judgment(H, "j-1")
    assert back == j
    cur = await tmp_db.execute(
        "SELECT severity, severity_floor, severity_sources, model_probs, created_at"
        " FROM judgments WHERE judgment_id = 'j-1'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert row[0] == 3 and row[1] == 2
    assert row[2] == '["table","resident"]' or row[2] == '["table", "resident"]'
    assert row[4] == NOW
    # 다른 집에서는 안 보인다.
    assert await repo.get_judgment(OTHER, "j-1") is None


async def test_save_judgment_without_severity(repo: SafetyRepo) -> None:
    j = _judgment(
        judgment_id="j-2", severity=None, severity_floor=None, severity_sources=[],
        situation_id=None, model_probs=None, gate2_verdict=None, degraded=False,
    )
    await repo.save_judgment(j)
    back = await repo.get_judgment(H, "j-2")
    assert back == j
