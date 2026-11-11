"""U1: 컴포트 판정은 이벤트 루프 밖에서 돈다 — 가스 첫 알림이 컴포트 판정에 막히지 않는다.

축:
  1) 컴포트 판정이 모델 안에서 막혀 있는 동안 들어온 가스 신호의 첫 알림이, 컴포트 판정이
     풀리기 전에 나간다(하드 게이트 1). 판정을 루프에서 동기로 부르면 이 테스트가 깨진다.
  2) 평범한 틱의 산출물(제안·아비터 결정·선호 갱신·판단 행)은 예전 동기 경로와 같다.
  3) 판정이 겹쳐 돌 때 사용량 기록(`JevBackend.last_usage`)이 호출마다 따로 남는다.
  4) 틱 동안 DB 접근은 루프 스레드에서만 일어난다(판정기에 넘기는 것은 이미 만들어진 값뿐).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l2_context.feature_vector import load_cells
from liot.layers.l3_judgment.jev_backend import JevBackend
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from tests.test_pipeline_situations import (
    GAS,
    _FixedBackend,
    _judgments,
    _make,
    _reading,
)

SEED = [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]


class _ComfortBlockingBackend(_FixedBackend):
    """컴포트(비안전) 후보만 풀어 줄 때까지 막는 백엔드. 안전 후보(2단계)는 바로 답한다.

    막힌 판정이 깨어난 시점에 이미 나간 알림 수와, 깨어난 이유(풀어 줌/제한시간)를 남긴다.
    제한시간은 판정을 루프에서 동기로 부르는 옛 코드가 테스트를 멈추지 않고 실패하게 하는 장치다.
    """

    model_id = "comfort-blocking-test-model"

    def __init__(self, transport: FakeNtfyTransport | None = None, wait_s: float = 1.5) -> None:
        super().__init__(tier=RiskTier.AMBIENT)
        self.transport = transport
        self.wait_s = wait_s
        self.started = threading.Event()
        self.release = threading.Event()
        self.woke_by_release: bool | None = None
        self.sent_when_woke: int | None = None

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        if candidate.category == "gas":
            return super().judge_raw(candidate, ctx, gate1_floor, severity_floor)
        self.started.set()
        self.woke_by_release = self.release.wait(timeout=self.wait_s)
        assert self.transport is not None
        self.sent_when_woke = len(self.transport.sent)
        return super().judge_raw(candidate, ctx, gate1_floor, severity_floor)


# ==========================================================================
# 1) 가스 첫 알림이 막힌 컴포트 판정보다 먼저 나간다
# ==========================================================================
async def test_gas_first_alert_is_sent_while_comfort_judgment_is_blocked(tmp_path) -> None:
    transport = FakeNtfyTransport()
    backend = _ComfortBlockingBackend(transport)
    svc, db, transport = await _make(tmp_path, backend=backend, transport=transport)
    tick_task: asyncio.Task[Any] | None = None
    try:
        for eid, raw in SEED:
            assert await svc.handle_reading(_reading(eid, raw)) is None
        assert transport.sent == []

        tick_task = asyncio.create_task(svc.run_tick())
        # 컴포트 판정이 모델 안으로 들어갈 때까지 기다린다. 루프가 막혀 있으면 이 대기 자체가
        # 판정의 제한시간이 끝난 뒤에야 돌아온다.
        assert await asyncio.to_thread(backend.started.wait, 5)
        assert not tick_task.done()  # 판정이 막혀 있으니 틱은 아직 끝나지 않았다

        res = await asyncio.wait_for(svc.handle_reading(_reading(GAS, "on")), timeout=5)
        assert res is not None and res.sent is True
        assert len(transport.sent) == 1  # 가스 첫 알림은 이미 나갔다
        assert not backend.release.is_set()  # 컴포트 판정은 아직 풀리지 않았다

        backend.release.set()
        proposal = await asyncio.wait_for(tick_task, timeout=5)
        assert proposal is not None  # 틱은 풀린 뒤 정상으로 끝난다
        await asyncio.wait_for(svc.drain_phase2(), timeout=5)

        # 막힌 판정은 제한시간이 아니라 '풀어 줘서' 깨어났고, 그때 이미 가스 알림이 나가 있었다.
        assert backend.woke_by_release is True
        assert backend.sent_when_woke == 1
        # 가스 알림이 컴포트 제안보다 앞이다(컴포트는 DIGEST 라 나가지 않는다).
        _, payload = transport.sent[0]
        assert payload["severity_label"] == SeverityLevel.CRITICAL.label_ko
        # 틱의 판단 행도 남았다(컴포트 + 가스 1단계·2단계).
        models = [r["model_id"] for r in await _judgments(db)]
        assert models.count(backend.model_id) >= 2  # 컴포트 1 + 가스 2단계 1
    finally:
        backend.release.set()
        if tick_task is not None and not tick_task.done():
            await asyncio.gather(tick_task, return_exceptions=True)
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 2) 평범한 틱의 산출물은 동기 경로와 같다
# ==========================================================================
async def _run_ordinary_tick(tmp_path) -> dict[str, Any]:
    svc, db, _ = await _make(tmp_path, backend=_FixedBackend(tier=RiskTier.AMBIENT))
    try:
        for eid, raw in SEED:
            assert await svc.handle_reading(_reading(eid, raw)) is None
        res = await svc.run_tick()
        assert res is not None
        await svc._apply_pref_signal(res.proposal.proposal_id, "accepted")
        cur = await db.execute(
            "SELECT p.action_json, p.risk_tier, p.state FROM proposals AS p ORDER BY p.rowid"
        )
        proposals = [tuple(r) for r in await cur.fetchall()]
        await cur.close()
        cur = await db.execute(
            "SELECT e.scored_json, e.selected_index, e.rationale"
            " FROM candidate_evaluations AS e ORDER BY e.rowid"
        )
        evaluations = [tuple(r) for r in await cur.fetchall()]
        await cur.close()
        cells = sorted((await load_cells(svc.prefs)).items())
        rows = await _judgments(db)
        for r in rows:  # 판단 id 는 매번 새로 뽑는 난수다 — 비교에서 뺀다
            r.pop("judgment_id")
        action = json.loads(proposals[0][0])
        action.pop("proposal_id", None)
        return {
            "decision": res.decision,
            "sent": res.sent,
            "action": res.proposal.action.model_dump(),
            "tier": int(res.proposal.risk_tier),
            "proposals": [(a, t, s) for a, t, s in proposals],
            "evaluations": evaluations,
            "pref_cells": cells,
            "judgments": rows,
            "last_facts": svc.app.last_facts,
        }
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_ordinary_tick_outputs_equal_the_synchronous_path(tmp_path, monkeypatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    offloop = await _run_ordinary_tick(tmp_path / "a")

    async def _sync_call(fn, /, *args, **kwargs):  # 옛 동작: 루프 스레드에서 그대로 부른다
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _sync_call)  # app.py 는 같은 모듈 객체를 쓴다
    sync = await _run_ordinary_tick(tmp_path / "b")

    assert offloop["action"]["action"] == "turn_off"
    assert offloop["evaluations"], "아비터 평가 행이 있어야 비교가 의미가 있다"
    assert offloop["pref_cells"], "선호 갱신이 있어야 비교가 의미가 있다"
    assert offloop == sync


# ==========================================================================
# 3) 판정이 겹쳐 돌아도 사용량 기록은 호출마다 따로다
# ==========================================================================
class _RendezvousResponse(dict[str, Any]):
    """응답을 읽는 중간(사용량 기록을 쓰기 직전)에 두 호출이 만나게 하는 응답."""

    def __init__(self, data: dict[str, Any], barrier: threading.Barrier) -> None:
        super().__init__(data)
        self._barrier = barrier

    def get(self, key: str, default: Any = None) -> Any:
        if key == "usage":
            self._barrier.wait(timeout=5)  # 두 호출 모두 '지연(seconds)' 기록을 마친 뒤
        return super().get(key, default)


class _TwoCallTransport:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._n = 0

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        with self._lock:
            self._n += 1
            n = self._n
        tokens = 100 * n
        probs = {str(i): (0.85 if i == 1 else 0.05) for i in range(4)}
        return _RendezvousResponse(
            {
                "model": f"jev-test-{n}",
                "answers": {"risk": {"probabilities": probs}},
                "usage": {"input_tokens": tokens, "output_tokens": n},
            },
            self.barrier,
        )


def test_last_usage_is_per_call_when_two_judgments_overlap() -> None:
    jev = JevBackend("test-key-not-real", transport=_TwoCallTransport())
    cand = CandidateAction(capability="light", action="turn_on", params={},
                           entity_id="light.living_room", category="light")
    ctx = ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
        structured={"time_bucket": "day"}, nl_summary="[day] 1 signals", summary_template_sha="x",
        created_at="2026-06-02T12:00:00+00:00",
    )
    done = threading.Barrier(2)
    seen: dict[str, dict[str, Any]] = {}
    errors: list[BaseException] = []

    def _worker(name: str) -> None:
        try:
            jev.judge_raw(cand, ctx, RiskTier.AMBIENT)
            done.wait(timeout=5)  # 둘 다 끝난 뒤에 읽는다 — 다른 호출이 덮어쓸 시간을 준다
            seen[name] = dict(jev.last_usage)
        except BaseException as exc:  # noqa: BLE001 — 스레드 안 실패를 메인으로 전한다
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert set(seen) == {"a", "b"}
    # 각 호출은 자기 응답의 사용량을 본다: 토큰 수와 응답 모델 번호가 짝으로 맞는다.
    for u in seen.values():
        n = int(u["model"].rsplit("-", 1)[1])
        assert u["input_tokens"] == 100 * n and u["output_tokens"] == n
        assert u["seconds"] >= 0
    assert {u["input_tokens"] for u in seen.values()} == {100, 200}


def test_last_usage_is_empty_before_any_call_and_never_stale() -> None:
    from liot.layers.l3_judgment.llm_judge import LLMUnavailable
    from tests.test_jev_backend import FakeJev

    jev = JevBackend("test-key-not-real", transport=FakeJev())
    assert jev.last_usage == {}
    cand = CandidateAction(capability="light", action="turn_on", params={},
                           entity_id="light.living_room", category="light")
    jev.judge_raw(cand, None, RiskTier.AMBIENT)
    assert jev.last_usage["input_tokens"] == 321

    def _blocked(*a: Any, **kw: Any) -> Any:  # 요청을 만들다 막히는 경우(예: egress 차단)
        raise LLMUnavailable("blocked before send")

    jev.request_body = _blocked  # type: ignore[method-assign]
    with pytest.raises(LLMUnavailable):
        jev.judge_raw(cand, None, RiskTier.AMBIENT)
    # 앞 호출(같은 스레드)의 토큰 수가 이 실패한 호출 것으로 읽히지 않는다.
    assert jev.last_usage == {}


# ==========================================================================
# 4) 워커 스레드의 판정기 경로는 DB 를 만지지 않는다
# ==========================================================================
async def test_sqlite_is_only_touched_from_the_loop_thread(tmp_path, monkeypatch) -> None:
    import aiosqlite

    backend = _FixedBackend(tier=RiskTier.AMBIENT)
    svc, db, _ = await _make(tmp_path, backend=backend)
    loop_thread = threading.get_ident()
    touched: list[int] = []
    real_execute = aiosqlite.Connection.execute

    def _spy(self, *a: Any, **kw: Any):
        touched.append(threading.get_ident())
        return real_execute(self, *a, **kw)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _spy)
    try:
        for eid, raw in SEED:
            await svc.handle_reading(_reading(eid, raw))
        before = len(touched)
        assert await svc.run_tick() is not None
        assert touched[before:], "틱은 DB 를 쓴다(판단 행 저장)"
        # 판정기는 루프 밖에서 돌았고(별도 호출 기록), DB 접근은 모두 루프 스레드에서만 일어났다.
        assert backend.thread_ids
        assert all(t == loop_thread for t in touched)
    finally:
        await svc.drain_phase2()
        await db.close()
