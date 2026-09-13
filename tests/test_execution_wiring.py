"""조작 배선(불변식 8) 검증 — LiotService + ExecutionPolicy + Executor.

축:
  - 비활성(기본): AUTO 대상이라도 실행 안 함(제안-만 유지).
  - AUTO: 비안전 ≤BREATH 는 dispatch 에서 즉시 실행.
  - CONFIRM(DUSK): dispatch 보류 → 승인 후 실행.
  - DOUBLE_CONFIRM(EMBER/안전): 1차 보류 → 2차 승인에서 실행.
  - undo: 자동 실행 후 서명된 되돌리기로 복원.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Proposal
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l4_execution.executor import ExecutionPolicy, FakeExecutor
from liot.layers.l4_execution.notifier import sign_feedback
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
CATS = {"light.living_room": "light", "lock.front_door": "lock"}


async def _service(*, enabled: bool, fail: bool = False):
    clock = Clock(fixed=DAY)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS)
    fx = FakeExecutor(fail=fail)
    svc = LiotService(
        app, db, clock=clock, transport=FakeNtfyTransport(), topic="liot-test",
        secret=SECRET, executor=fx, policy=ExecutionPolicy(enabled=enabled),
    )
    return svc, fx, db


def _proposal(cap: str, action: str, category: str, tier: RiskTier, entity: str) -> Proposal:
    return Proposal(
        proposal_id=f"p_{uuid.uuid4().hex[:10]}",
        judgment_id="j1",
        home_id="singleton",
        risk_tier=tier,
        action=CandidateAction(capability=cap, action=action, category=category,
                               entity_id=entity, params={}),
        rationale="test",
        required_confirmation=tier.required_confirmation,
        perishable=False,
        expires_at=None,
        state="pending",
        created_at=DAY,
    )


async def _approve(svc: LiotService, prop: Proposal, action: str = "accept") -> str:
    """정당한 사용자 흐름 — 지금 발급돼 있는 1회용 nonce 로 링크를 만든다.

    실제로는 알림에 실려 온 링크를 누르는 것이고, 그 링크에 nonce 가 박혀 있다.
    이중확인 2차에서는 서비스가 새 nonce 를 발급하므로, 여기서 매번 현재 값을
    다시 읽는 것이 곧 '새 알림의 새 링크를 누른다'에 해당한다.
    """
    tier_int = int(prop.risk_tier)
    # 되돌리기만 nonce 를 쓰지 않는다(_handle_undo 규약 — _prior 소비로 1회용).
    nonce = "" if action == "undo" else svc._nonce.get(prop.proposal_id, "")
    sig = sign_feedback(SECRET, prop.proposal_id, tier_int, action, nonce)
    line = (
        f"GET /feedback?p={prop.proposal_id}&t={tier_int}"
        f"&n={nonce}&sig={sig}&a={action} HTTP/1.1"
    )
    _status, body = await svc._route_feedback(line)
    return body


# ── 비활성: 제안-만 유지 ────────────────────────────────────────────────
async def test_disabled_does_not_execute():
    svc, fx, db = await _service(enabled=False)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is False
        assert fx.executed == []
    finally:
        await db.close()


# ── AUTO: 비안전 ≤BREATH 즉시 실행 ─────────────────────────────────────
async def test_auto_executes_comfort():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is True and res.sent is True
        assert len(fx.executed) == 1 and fx.executed[0].entity_id == "light.living_room"
    finally:
        await db.close()


# ── CONFIRM(DUSK): dispatch 보류 → 승인 후 실행 ────────────────────────
async def test_dusk_defers_until_approval():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.DUSK, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is False and fx.executed == []  # 보류
        body = await _approve(svc, prop)
        assert "executed" in body and len(fx.executed) == 1
    finally:
        await db.close()


# ── DOUBLE_CONFIRM(안전 lock): 1차 보류 → 2차 실행 ─────────────────────
async def test_safety_double_confirm():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        res = await svc.dispatch(prop)
        assert res.executed is False  # 안전은 절대 자동 아님
        first = await _approve(svc, prop)
        assert "1/2" in first and fx.executed == []  # 1차는 보류
        second = await _approve(svc, prop)
        assert "2/2" in second and len(fx.executed) == 1
    finally:
        await db.close()


# ── undo: 자동 실행 후 서명된 되돌리기 ─────────────────────────────────
async def test_undo_after_auto():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        await svc.dispatch(prop)
        assert len(fx.executed) == 1
        body = await _approve(svc, prop, action="undo")
        assert body == "undone" and len(fx.undone) == 1
    finally:
        await db.close()


# ── 위조 undo 서명 거부 ────────────────────────────────────────────────
async def test_undo_rejects_bad_signature():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        await svc.dispatch(prop)
        line = f"GET /feedback?p={prop.proposal_id}&t=1&sig=deadbeef&a=undo HTTP/1.1"
        status, _ = await svc._route_feedback(line)
        assert status.startswith("403")
        assert fx.undone == []
    finally:
        await db.close()


# ── 재생 방어: 캡처한 링크 하나로 이중확인을 통과할 수 없다 ──────────────
async def test_replayed_accept_cannot_satisfy_double_confirm():
    """같은 수락 링크를 두 번 재생해도 DOUBLE_CONFIRM 이 충족되지 않는다.

    2026-08-29 코드리뷰가 찾은 결함: 피드백 서명이 (pid, tier, action) 만 묶어
    영구 재사용 가능한 권한이었다. `_on_approved` 는 1차를 보류하고 2차에서
    실행하는데, 같은 링크를 두 번 던지면 그 둘이 모두 채워져 EMBER·안전 임계
    행동이 실행됐다 — 두 번의 독립적인 사람 확인을 요구하는 장치가 무력화된다.
    이제 서명에 1회용 nonce 가 묶이고 성공한 피드백마다 소비된다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)

        # 공격자가 알림에서 수락 링크를 그대로 캡처했다고 가정한다.
        tier_int = int(prop.risk_tier)
        nonce = svc._nonce[prop.proposal_id]
        sig = sign_feedback(SECRET, prop.proposal_id, tier_int, "accept", nonce)
        captured = (
            f"GET /feedback?p={prop.proposal_id}&t={tier_int}"
            f"&n={nonce}&sig={sig}&a=accept HTTP/1.1"
        )

        status, body = await svc._route_feedback(captured)
        assert "200" in status and "1/2" in body  # 1차는 보류
        assert fx.executed == []

        # 재생 — 예전에는 여기서 2/2 가 되어 실제로 실행됐다.
        replay_status, _replay_body = await svc._route_feedback(captured)
        assert "403" in replay_status, "소비된 링크가 다시 통했다"
        assert fx.executed == [], "재생된 링크로 이중확인이 충족돼 실행됐다"
    finally:
        await db.close()


async def test_second_confirmation_uses_a_freshly_issued_link():
    """2차 확인은 새 nonce 를 가진 별도 알림으로만 온다(1차 링크로는 불가)."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        first_nonce = svc._nonce[prop.proposal_id]
        sent_before = len(svc.transport.sent)

        assert "1/2" in await _approve(svc, prop)

        second_nonce = svc._nonce[prop.proposal_id]
        assert second_nonce != first_nonce, "1차 성공 후 nonce 가 갱신되지 않았다"
        # 2차 확인 요청이 실제 알림으로 나갔다(사용자가 누를 새 링크가 생긴다).
        assert len(svc.transport.sent) == sent_before + 1
        assert f"n={second_nonce}" in svc.transport.sent[-1][1]["signed_deeplink"]

        # 새 링크로는 정상 실행된다(정당한 흐름은 막히지 않는다).
        assert "2/2" in await _approve(svc, prop)
        assert len(fx.executed) == 1
    finally:
        await db.close()


def _delivered_nonce(svc: LiotService) -> str:
    """사용자 폰에 마지막으로 도착한 알림의 링크에 박힌 nonce 를 읽는다."""
    transport = svc.transport
    assert isinstance(transport, FakeNtfyTransport)
    link = str(transport.sent[-1][1]["signed_deeplink"])
    return parse_qs(urlsplit(link).query)["n"][0]


async def _click(svc: LiotService, prop: Proposal, action: str, nonce: str) -> tuple[str, str]:
    """주어진 nonce 가 박힌 링크를 그대로 누른다(서비스 내부 상태를 읽지 않는다)."""
    tier_int = int(prop.risk_tier)
    sig = sign_feedback(SECRET, prop.proposal_id, tier_int, action, nonce)
    line = (
        f"GET /feedback?p={prop.proposal_id}&t={tier_int}"
        f"&n={nonce}&sig={sig}&a={action} HTTP/1.1"
    )
    return await svc._route_feedback(line)


# ── R2-01: 틱이 한 번 돌아도 2차 확인 링크는 살아 있다 ──────────────────
async def test_second_confirmation_link_survives_a_tick():
    """1차 수락 → 틱(스윕) → 2차 알림의 링크 → 실행.

    2026-09-01 리뷰 R2-01: 1차 수락이 상태를 accepted 로 옮기므로, pending 만
    살려 두던 스윕이 방금 발급한 2차 링크의 nonce 를 회수했다. 상시 데몬은 매
    틱 스윕하므로 사람이 2차 알림을 누를 즈음엔 403 이었고 실행은 일어나지 않았다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        status, first = await _click(svc, prop, "accept", _delivered_nonce(svc))
        assert "200" in status and "1/2" in first and fx.executed == []

        second_nonce = _delivered_nonce(svc)  # 2차 확인 알림의 새 링크
        await svc.run_tick()  # 데몬은 매 틱 _sweep_nonces 를 돈다

        status, second = await _click(svc, prop, "accept", second_nonce)
        assert "200" in status, f"2차 링크가 틱 뒤에 죽었다: {status} {second}"
        assert "2/2" in second and len(fx.executed) == 1
    finally:
        await db.close()


async def test_sweep_still_revokes_second_link_once_proposal_is_superseded():
    """2차 대기라도 제안이 accepted 를 벗어나면(대체·만료) 링크는 회수된다."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        await _click(svc, prop, "accept", _delivered_nonce(svc))
        second_nonce = _delivered_nonce(svc)
        await svc.proposals.set_state(prop.proposal_id, "superseded")

        await svc.run_tick()

        assert prop.proposal_id not in svc._nonce
        status, _ = await _click(svc, prop, "accept", second_nonce)
        assert "403" in status and fx.executed == []
    finally:
        await db.close()


# ── R2-17: 2차 확인 알림 송신 실패는 알리고, 같은 버튼으로 재시도할 수 있다 ──
async def test_second_confirmation_send_failure_is_reported_and_retryable():
    """2차 알림이 안 나가면 '다시 확인하세요'라고 답하지 않는다.

    2026-09-01 리뷰 R2-17: 송신 실패를 contextlib.suppress 로 삼키고도 응답은
    confirm-again(1/2) 였다. 사람 손엔 누를 2차 링크가 없고 1차 링크는 소비돼,
    안전 확인 흐름이 소리 없이 끝났다. 이제 실패를 응답에 드러내고, 방금 누른
    1차 링크를 되살려 같은 버튼을 다시 누르면 2차 알림 발송을 재시도한다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        transport = svc.transport
        assert isinstance(transport, FakeNtfyTransport)
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        first_nonce = _delivered_nonce(svc)

        transport.fail = True
        status, body = await _click(svc, prop, "accept", first_nonce)
        assert "200" in status
        assert "not-sent" in body, f"송신 실패가 응답에 드러나지 않았다: {body}"
        assert "confirm-again" not in body, "나가지 않은 알림을 다시 확인하라고 안내했다"
        assert fx.executed == []

        await svc.run_tick()  # 재시도 경로는 틱 스윕 뒤에도 살아 있어야 한다

        transport.fail = False
        sent_before = len(transport.sent)
        status, body = await _click(svc, prop, "accept", first_nonce)
        assert "200" in status and "1/2" in body, f"재시도가 막혔다: {status} {body}"
        assert len(transport.sent) == sent_before + 1, "재시도에서 2차 알림이 안 나갔다"
        assert fx.executed == []

        # 2차 링크가 실제로 나간 뒤에는 1차 링크가 다시 죽는다(재생 방어 유지).
        replay_status, _ = await _click(svc, prop, "accept", first_nonce)
        assert "403" in replay_status

        status, body = await _click(svc, prop, "accept", _delivered_nonce(svc))
        assert "2/2" in body and len(fx.executed) == 1
    finally:
        await db.close()


async def test_undelivered_second_confirmation_never_lets_first_link_execute():
    """되살린 1차 링크는 발송 재시도만 한다 — 몇 번을 눌러도 실행되지 않는다."""
    svc, fx, db = await _service(enabled=True)
    try:
        transport = svc.transport
        assert isinstance(transport, FakeNtfyTransport)
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        first_nonce = _delivered_nonce(svc)

        transport.fail = True
        for _ in range(3):
            status, body = await _click(svc, prop, "accept", first_nonce)
            assert "200" in status and "not-sent" in body
        assert fx.executed == [], "2차 링크가 나간 적 없는데 1차 링크로 실행됐다"
        assert prop.proposal_id not in svc._awaiting_second
    finally:
        await db.close()


# ── R2-05: 스누즈는 링크를 소비하지 않는다 ─────────────────────────────
async def test_snooze_keeps_the_delivered_links_alive():
    """스누즈 뒤에도 같은 알림의 버튼으로 수락할 수 있다.

    2026-09-01 리뷰 R2-05: 스누즈는 제안을 pending 으로 남기고 새 링크를 내보내지
    않는데, 성공한 피드백이라며 nonce 를 소비했다. 사람이 가진 버튼이 전부 403
    이 되고 제안은 pending 으로 영원히 남았다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.DUSK, "light.living_room")
        await svc.dispatch(prop)
        nonce = _delivered_nonce(svc)

        status, body = await _click(svc, prop, "snooze", nonce)
        assert "200" in status and "pending" in body

        status, body = await _click(svc, prop, "accept", nonce)
        assert "200" in status, f"스누즈가 알림의 버튼을 죽였다: {status} {body}"
        assert "accepted" in body and len(fx.executed) == 1

        # 수락은 여전히 소비한다(1회용 보장은 그대로).
        replay_status, _ = await _click(svc, prop, "accept", nonce)
        assert "403" in replay_status
    finally:
        await db.close()


# ── R2-02: 비-ASCII n=/sig= 는 예외(→500)가 아니라 403 ──────────────────
@pytest.mark.parametrize("field", ["nonce", "sig", "undo_sig"])
async def test_non_ascii_link_fields_are_403_for_live_and_unknown_pid_alike(field: str):
    """%C3%A9 한 글자로 500 을 내던 요청이 이제 403 이고, pid 가 살아 있든 없든 같다.

    2026-09-01 리뷰 R2-02: hmac.compare_digest 는 비-ASCII str 에 TypeError 를
    던진다. 살아 있는 nonce 가 있는 pid 에서만 그 비교까지 가므로, 500 과 403 의
    차이가 'nonce 가 살아 있는가'를 알려 주는 신호였다(수신구는 예외를 500 으로 바꾼다).
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        nonce = _delivered_nonce(svc)
        tier_int = int(prop.risk_tier)
        good_sig = sign_feedback(SECRET, prop.proposal_id, tier_int, "accept", nonce)
        non_ascii = "%C3%A9"  # 'é'

        def line(pid: str) -> str:
            if field == "nonce":
                return f"GET /feedback?p={pid}&t={tier_int}&n={non_ascii}&sig={good_sig}&a=accept HTTP/1.1"
            if field == "sig":
                return f"GET /feedback?p={pid}&t={tier_int}&n={nonce}&sig={non_ascii}&a=accept HTTP/1.1"
            return f"GET /feedback?p={pid}&t={tier_int}&sig={non_ascii}&a=undo HTTP/1.1"

        live_status, _ = await svc._route_feedback(line(prop.proposal_id))
        unknown_status, _ = await svc._route_feedback(line("p_does_not_exist"))
        assert live_status.startswith("403"), live_status
        assert unknown_status == live_status, "살아 있는 pid 와 없는 pid 의 응답이 갈린다"
        assert fx.executed == [] and fx.undone == []
        assert (await svc.proposals.get(prop.proposal_id)).state == "pending"
    finally:
        await db.close()


# ── R2-28: 서명은 nonce 에 묶인다 — 옛 서명에 새 nonce 를 붙여도 통하지 않는다 ──
async def test_old_signature_cannot_ride_a_freshly_issued_nonce():
    """캡처한 1차 수락 서명 + 2차 알림의 새 nonce 조합은 거부된다.

    2차 알림의 '열기' 딥링크는 nonce 를 서명 없이 운반한다(sign_deeplink). 서명이
    nonce 를 묶지 않으면, 1차 링크를 캡처한 사람이 2차 알림에서 새 nonce 만 읽어
    옛 서명에 붙이는 것으로 이중확인 2차를 채울 수 있다. 서버 쪽 nonce 비교는 이
    조합을 못 막는다(nonce 자체는 지금 유효한 값이다) — 서명의 nonce 바인딩이 막는다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        tier_int = int(prop.risk_tier)
        first_nonce = _delivered_nonce(svc)
        captured_sig = sign_feedback(SECRET, prop.proposal_id, tier_int, "accept", first_nonce)
        status, body = await _click(svc, prop, "accept", first_nonce)
        assert "1/2" in body

        fresh_nonce = _delivered_nonce(svc)  # 2차 알림 딥링크에서 누구나 읽는 값
        forged = (
            f"GET /feedback?p={prop.proposal_id}&t={tier_int}"
            f"&n={fresh_nonce}&sig={captured_sig}&a=accept HTTP/1.1"
        )
        status, body = await svc._route_feedback(forged)
        assert "403" in status, f"옛 서명이 새 nonce 로 통했다: {status} {body}"
        assert fx.executed == []

        # 정당한 2차 링크는 그 거부에 영향받지 않는다.
        assert "2/2" in (await _click(svc, prop, "accept", fresh_nonce))[1]
        assert len(fx.executed) == 1
    finally:
        await db.close()


# ── 2차 확인 링크의 상한: 창(window) ────────────────────────────────────
async def test_second_confirmation_link_dies_after_the_window():
    """1차 수락 뒤 시간이 충분히 지나면 2차 링크는 403 이고 실행되지 않는다.

    R2-01 수정(2차 링크가 틱 한 번에 죽던 문제)이 '영원히 유효'로 뒤집히지 않게
    하는 상한이다 — accepted 상태는 expire_due 가 건드리지 않으므로 상태만으로는
    이 링크가 절대 죽지 않는다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        first = _delivered_nonce(svc)
        assert "1/2" in (await _click(svc, prop, "accept", first))[1]
        second = _delivered_nonce(svc)

        svc.clock.set_fixed("2026-06-02T12:16:00+00:00")  # 창(15분) 초과
        status, body = await _click(svc, prop, "accept", second)
        assert "403" in status, f"{status} {body}"
        assert fx.executed == [], "만료된 2차 링크로 실행됐다"
        # 죽은 토큰의 대기 표식·발급시각도 함께 사라진다(새는 상태 없음).
        assert prop.proposal_id not in svc._nonce
        assert prop.proposal_id not in svc._nonce_issued
        assert prop.proposal_id not in svc._awaiting_second
    finally:
        await db.close()


async def test_second_confirmation_link_still_works_inside_the_window():
    """창 안에서는(그리고 틱이 돌아도) 2차 링크가 그대로 실행된다."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        assert "1/2" in (await _click(svc, prop, "accept", _delivered_nonce(svc)))[1]
        second = _delivered_nonce(svc)
        svc.clock.set_fixed("2026-06-02T12:14:00+00:00")  # 창 안
        await svc._sweep_nonces()
        status, body = await _click(svc, prop, "accept", second)
        assert "200" in status and "executed(2/2)" in body, f"{status} {body}"
        assert len(fx.executed) == 1
    finally:
        await db.close()


# ── 2차 확인 링크의 상한: 제안 자신의 만료 ──────────────────────────────
async def test_second_confirmation_link_dies_when_the_proposal_expires():
    """제안의 expires_at 이 지나면 창이 남아 있어도 2차 링크는 죽는다."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        prop = prop.model_copy(update={"expires_at": "2026-06-02T12:05:00+00:00"})
        await svc.dispatch(prop)
        assert "1/2" in (await _click(svc, prop, "accept", _delivered_nonce(svc)))[1]
        second = _delivered_nonce(svc)

        svc.clock.set_fixed("2026-06-02T12:06:00+00:00")  # 만료 뒤, 창(15분) 안
        status, body = await _click(svc, prop, "accept", second)
        assert "403" in status, f"{status} {body}"
        assert fx.executed == [], "만료된 제안이 2차 링크로 실행됐다"
    finally:
        await db.close()


async def test_sweep_drops_the_dead_second_confirmation_link(monkeypatch):
    """스윕도 같은 상한으로 죽은 2차 토큰을 버린다(클릭 없이도 정리된다)."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        assert "1/2" in (await _click(svc, prop, "accept", _delivered_nonce(svc)))[1]
        pid = prop.proposal_id
        assert pid in svc._nonce and pid in svc._awaiting_second

        svc.clock.set_fixed("2026-06-02T12:16:00+00:00")
        await svc._sweep_nonces()
        assert pid not in svc._nonce
        assert pid not in svc._nonce_issued
        assert pid not in svc._awaiting_second
        assert fx.executed == []
    finally:
        await db.close()


# ── R-13: 종결 피드백은 2차 대기 표식을 남기지 않는다 ───────────────────
async def test_dismiss_after_first_confirmation_leaves_no_marker():
    """1차 수락 뒤 거절하면 대기 표식이 남지 않는다(pid 문자열 누적 방지).

    2026-09-13 리뷰 R-13: 클릭 경로는 _drop_nonce 를 쓸 수 없어(바로 뒤 _on_approved
    가 표식을 읽는다) _awaiting_second 만 남겼고, 스윕은 nonce 가 있는 pid 만 돌아
    그 표식을 다시 볼 일이 없었다 — 프로세스 수명만큼 쌓였다.
    """
    svc, fx, db = await _service(enabled=True)
    try:
        for i in range(3):
            prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, f"lock.door_{i}")
            await svc.dispatch(prop)
            assert "1/2" in (await _click(svc, prop, "accept", _delivered_nonce(svc)))[1]
            status, _ = await _click(svc, prop, "dismiss", _delivered_nonce(svc))
            assert "200" in status
        assert svc._awaiting_second == set(), svc._awaiting_second
        assert svc._nonce == {} and svc._nonce_issued == {}
        assert fx.executed == []
    finally:
        await db.close()


async def test_sweep_prunes_a_marker_left_without_a_nonce():
    """토큰 없이 표식만 남은 pid 도 스윕이 정리한다(자기교정)."""
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        await svc.dispatch(prop)
        assert "1/2" in (await _click(svc, prop, "accept", _delivered_nonce(svc)))[1]
        # 토큰만 사라지고 표식이 남은 상태를 인위적으로 만든다(옛 결함의 잔재 형태).
        svc._nonce.pop(prop.proposal_id, None)
        svc._nonce_issued.pop(prop.proposal_id, None)
        assert prop.proposal_id in svc._awaiting_second
        await svc._sweep_nonces()
        assert prop.proposal_id not in svc._awaiting_second
    finally:
        await db.close()


# ── 출하 카탈로그에는 '한 번 확인으로 실행되는' 쌍이 없다 ────────────────
def test_shipped_catalog_has_no_single_confirm_executable_pair():
    """CONFIRM(단일 확인 → 실행) 등급의 출하 조합이 0 임을 고정한다.

    2026-09-13 리뷰: 이벤트(반응) 제안은 만료가 없어 그 링크가 영원히 유효하다.
    그 자체로 실행까지 가려면 '비안전 카테고리 + DUSK + 컴포트 화이트리스트' 조합이
    필요하고, 출하 카탈로그에는 그런 쌍이 없다(cover.garage 는 door=안전 카테고리라
    NOTIFY_ONLY). 카탈로그 편집으로 이 성질이 깨지면 여기서 빨개진다 — 그때는
    '영원히 유효한 링크'가 곧바로 단일 클릭 실행 경로가 된다.
    """
    from liot.core.contracts import CandidateAction
    from liot.layers.l4_execution.device_catalog import load_device_catalog
    from liot.layers.l4_execution.executor import ExecDecision, ExecutionPolicy

    catalog = load_device_catalog()
    policy = ExecutionPolicy(enabled=True)  # 옵트인을 켠 최악의 경우로 본다
    confirm_pairs = []
    for row in catalog.available_actions():
        candidate = CandidateAction(
            capability=str(row["capability"]), action=str(row["action"]), params={},
            entity_id=str(row["entity_id"]), category=str(row["category"]),
        )
        if policy.decide(candidate, row["tier"]) is ExecDecision.CONFIRM:
            confirm_pairs.append((row["entity_id"], row["capability"], row["action"]))
    assert confirm_pairs == [], f"단일 확인으로 실행되는 출하 조합이 생겼다: {confirm_pairs}"
