"""하루 시뮬레이터 cloud 모드 — 가짜 HTTP(Jev·싼 모델·luna)와 가짜 memgineering CLI 만 쓴다.

지키는 것:
  - 각 단계의 첫 안전 알림이 그 단계의 모든 외부 호출보다 먼저 나간다.
  - 안전 2단계 마감이 12초다. 판정 기록의 model_id 는 실제로 쓴 판정기 이름이다.
  - 상한(호출 수·실행 비용·하루 비용)을 넘기면 가짜로 바뀌고 사건이 한 번 나온다.
  - 거주자 응답이 이상하면 성향 표로 대체한다. [그만해]는 --rule 로 기록된다.
  - 다음 이유 입력에 같은 실행의 집 기억이 들어가고, 다른 실행의 기억은 섞이지 않는다.
  - 키 상태는 이름과 있음/없음만. fake 모드에는 cloud 필드가 없다.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from liot import day_sim
from liot.cheap_llm import CheapLLM, CheapModelConfig, ProviderRequest
from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.day_cloud import CloudKit, CloudLimits, UsageMeter, load_cloud_settings
from liot.day_sim import GAS, LEAK, DayRuns, DaySim, _RecordingJudge
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.jev_backend import JevBackend
from liot.layers.l3_judgment.llm_judge import FakeLLM, LLMJudge
from liot.live_runs import LiveRequestError
from liot.luna_shadow import LunaConfig, LunaShadow
from liot.memory_client import RESIDENT_SCOPE, MemoryClient
from liot.secrets_env import KNOWN_KEYS

KEY = "fake" + "-cloud-" + "key-0001"  # 실행 중에 만든다(비밀 스캔과 같은 이유)
HOME = Path("/tmp/liot-test-mg-home")  # 가짜 CLI 만 본다 — 만들지 않는다
TODAY = "2026-10-20"
Events = list[tuple[str, dict[str, Any]]]


# ── 가짜 외부 서비스 ─────────────────────────────────────────────────────────


class FakeJev:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.calls += 1
        # 가장 낮은 선택지(잔잔/기록). 하한보다 낮은 답이라 최종값은 하한 그대로다.
        probs = {"0": 0.4, "1": 0.3, "2": 0.2, "3": 0.1}
        answers = {k: {"probabilities": probs} for k in payload["questions"]}
        return {"model": "jev-test", "answers": answers,
                "usage": {"input_tokens": 300, "output_tokens": 0}}


class FakeCheap:
    """Anthropic 모양 응답. 거주자 질문이면 보기 하나, 이유면 한 줄."""

    def __init__(self, *, odd_choice: bool = False, reason: str = "오늘 공기가 맑아 환기하기 좋아요.") -> None:
        self.odd_choice = odd_choice
        self.reason = reason
        self.requests: list[ProviderRequest] = []

    def post(self, request: ProviderRequest, *, timeout_s: float) -> dict[str, Any]:
        self.requests.append(request)
        facts = json.loads(request.body["messages"][0]["content"])["facts"]
        if self.is_choice(request):
            text = "음, 잘 모르겠어요" if self.odd_choice else (
                "[그만해]" if "공기청정기" in facts["title"] else "도움됐어요")
        else:
            text = self.reason
        return {"content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 120, "output_tokens": 20}}

    @staticmethod
    def is_choice(request: ProviderRequest) -> bool:
        return str(request.body["system"]).startswith("너는 스마트홈 알림을 받은 가상 거주자다")

    def reason_inputs(self) -> list[dict[str, Any]]:
        return [json.loads(r.body["messages"][0]["content"]) for r in self.requests
                if not self.is_choice(r)]


class FakeLuna:
    def post(self, request: ProviderRequest, *, timeout_s: float) -> dict[str, Any]:
        top = [{"token": "D", "logprob": -0.1}, {"token": "C", "logprob": -2.0}]
        return {"choices": [{"logprobs": {"content": [{"token": "D", "top_logprobs": top}]}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 1}}


class FakeBrain:
    """memgineering CLI 흉내. 범위별 노트를 들고 있다. leaky=True 면 범위를 무시하고 다 준다."""

    def __init__(self, *, brain: str = "liot-demo", leaky: bool = False) -> None:
        self.brain = brain
        self.leaky = leaky
        self.notes: list[dict[str, Any]] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str],
                 timeout_s: float) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.argvs.append(argv)
        assert env["MEMGINEERING_HOME"] == str(HOME) and env["MEMGINEERING_NO_UPDATE"] == "1"
        verb = argv[1]
        if verb == "use":
            out: dict[str, Any] = {"hosted": {"name": self.brain, "signed_in": True},
                                   "destination": "hosted"}
        elif verb == "recall":
            scope = argv[argv.index("--scope") + 1] if "--scope" in argv else None
            cards = [n for n in self.notes if self.leaky or n["scope"] == scope]
            out = {"query": argv[2], "cards": cards}
        else:
            scope = argv[argv.index("--scope") + 1]
            self.notes.append({"title": argv[2], "summary": f"{argv[2]} ({scope})", "scope": scope,
                               "binding": "--rule" in argv, "kind": None,
                               "handle": "h", "path": "observations/x.md"})
            out = {"id": "m", "op_id": f"OP{len(self.notes)}", "binding": "--rule" in argv}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out, ensure_ascii=False), "")

    def remembers(self) -> list[list[str]]:
        return [a for a in self.argvs if a[1] == "remember"]


def _kit(
    tmp: Path,
    *,
    cheap: FakeCheap | None = None,
    brain: FakeBrain | None = None,
    call_cap: int = 1000,
    run_cap: float = 0.5,
    day_cap: float = 2.0,
    prices: dict[str, Any] | None = None,
) -> CloudKit:
    _, shipped = load_cloud_settings()
    limits = CloudLimits(call_cap, run_cap, day_cap, tmp / "usage.json")
    meter = UsageMeter(limits, shipped if prices is None else prices, today=lambda: TODAY)
    return CloudKit(
        meter=meter,
        judge=JevBackend(transport=FakeJev()),
        cheap=CheapLLM(CheapModelConfig("anthropic", "claude-haiku-5-5", 1.0, 64), KEY,
                       transport=cheap or FakeCheap()),
        luna=LunaShadow(LunaConfig("logprobs", "gpt-6-luna", 1.0), KEY, transport=FakeLuna()),
        memory=MemoryClient(home=HOME, runner=brain or FakeBrain()),
        status=[{"part": "memory", "state": "pending", "model": "memgineering:liot-demo",
                 "reason_ko": None}],
    )


def _run(sim: DaySim) -> Events:
    events: Events = []
    asyncio.run(sim.run(lambda stage, data: events.append((stage, data))))
    return events


@pytest.fixture(scope="module")
def cloud7(tmp_path_factory: pytest.TempPathFactory) -> tuple[DaySim, Events, FakeCheap, FakeBrain]:
    cheap, brain = FakeCheap(), FakeBrain()
    sim = DaySim(seed=7, days=3, models="cloud",
                 cloud=_kit(tmp_path_factory.mktemp("c7"), cheap=cheap, brain=brain),
                 house_id="runA")
    return sim, _run(sim), cheap, brain


def _segments(log: list[tuple[str, Any]]) -> list[list[tuple[int, tuple[str, Any]]]]:
    segs: list[list[tuple[int, tuple[str, Any]]]] = []
    for i, entry in enumerate(log):
        if entry[0] == "step":
            segs.append([])
        if segs:
            segs[-1].append((i, entry))
    return segs


# ── 순서: 첫 안전 알림이 그 단계의 모든 외부 호출보다 먼저 ──────────────────────


def test_first_safety_alert_precedes_every_external_call_in_its_step(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, _, _, _ = cloud7
    firsts = {sim._tracked[a["id"]].pid for a in sim.safety_events if a["kind"] == "first_alert"}
    assert len(firsts) == 3
    checked = 0
    for seg in _segments(sim.log):
        sends = [i for i, e in seg if e[0] == "send" and e[1] in firsts]
        if not sends:
            continue
        exts = [i for i, e in seg if e[0] == "ext"]
        assert exts, "the alert step must contain external calls (phase-2 judge at least)"
        assert sends[0] < min(exts)
        checked += 1
    assert checked == 3


def test_safety_alert_step_reorders_only_in_cloud_mode(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, events, _, _ = cloud7
    acks = [d for st, d in events if st == "feedback" and d["kind"] == "safety"]
    assert len(acks) == 3 and all(a["status"] == "200" for a in acks)
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert all(f["decision"] == "IMMEDIATE" and f["sent"] and f["tier"] == "ember" for f in firsts)
    assert {LEAK, GAS} >= {LEAK if f["category"] == "leak" else GAS for f in firsts}


def test_phase2_deadline_is_12s_in_cloud_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []
    from liot.service import LiotService as real

    class Spy(real):
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.append(kw.get("phase2_deadline_s"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(day_sim, "LiotService", Spy)
    _run(DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path)))
    _run(DaySim(seed=7, days=1))
    assert seen == [12.0, None]


# ── 판정기 전환과 model_id ───────────────────────────────────────────────────


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
                           structured={"time_bucket": "day"}, nl_summary="[day] light",
                           summary_template_sha="x", created_at="2026-06-02T12:00:00+00:00")


def test_judgment_model_id_is_the_backend_actually_used(tmp_path: Path) -> None:
    kit = _kit(tmp_path, call_cap=1)
    log: list[tuple[str, Any]] = []
    judge = _RecordingJudge(FakeLLM(), log, cloud=kit.judge, kit=kit)
    cand = CandidateAction(capability="light", action="turn_on", params={},
                           entity_id="light.living_room", category="light")
    g = Guardrail()
    llm = LLMJudge(backend=judge)
    assert judge.model_id == "jev:jev-latest"
    first = llm.judge(cand, _ctx(), g.evaluate(cand, _ctx()), ruleset_version=g.ruleset_version)
    assert judge.model_id == "fake-llm-v1"  # 상한(1회)에 닿은 뒤 고를 판정기
    second = llm.judge(cand, _ctx(), g.evaluate(cand, _ctx()), ruleset_version=g.ruleset_version)
    assert first.model_id == "jev:jev-latest"
    assert second.model_id == "fake-llm-v1"  # 상한(1회) 뒤에는 가짜 판정기
    assert kit.meter.tripped == "calls"
    assert [e for e in log if e[0] == "ext"] == [("ext", "judge")]


def test_degraded_cloud_judgment_keeps_the_cloud_model_id(tmp_path: Path) -> None:
    from liot.layers.l3_judgment.llm_judge import LLMUnavailable

    class Down:
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            raise LLMUnavailable("jev HTTP 503")

    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=Down())
    judge = _RecordingJudge(FakeLLM(), [], cloud=kit.judge, kit=kit)
    cand = CandidateAction(capability="light", action="turn_on", params={},
                           entity_id="light.living_room", category="light")
    g = Guardrail()
    jd = LLMJudge(backend=judge).judge(cand, _ctx(), g.evaluate(cand, _ctx()),
                                       ruleset_version=g.ruleset_version)
    assert jd.degraded and jd.model_id == "jev:jev-latest"
    assert kit.meter.summary()["by_kind"]["judge"]["calls"] == 1


def test_cloud_run_judges_with_jev_and_records_usage(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, events, _, _ = cloud7
    models = sum(1 for e in sim.log if e[0] == "model")
    judges = sum(1 for e in sim.log if e == ("ext", "judge"))
    assert models == judges > 0
    usage = sim.summary()["models"]["usage"]
    assert usage["by_kind"]["judge"]["calls"] == judges
    assert usage["calls"] == sum(1 for e in sim.log if e[0] == "ext")
    assert usage["tripped"] is None and usage["run_usd"] > 0
    ends = [d for st, d in events if st == "day.end"]
    assert all("usage" in d for d in ends)


def test_luna_shadow_runs_beside_each_jev_judgment(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, _, _, _ = cloud7
    judges = sum(1 for e in sim.log if e == ("ext", "judge"))
    assert len(sim.luna_results) == judges
    assert all(r["status"] == "ok" and "tier" in r["agree"] for r in sim.luna_results)
    assert sim.summary()["models"]["luna_compared"] == judges


# ── 상한 ─────────────────────────────────────────────────────────────────────


def _no_ext_after_fallback(sim: DaySim, events: Events) -> None:
    fallbacks = [d for st, d in events if st == "models.fallback"]
    assert len(fallbacks) == 1 and fallbacks[0]["line_ko"]
    usage = sim.summary()["models"]["usage"]
    assert usage["calls"] == sum(1 for e in sim.log if e[0] == "ext")


def test_call_cap_switches_to_fake_with_one_event(tmp_path: Path) -> None:
    sim = DaySim(seed=7, days=3, models="cloud", cloud=_kit(tmp_path, call_cap=10))
    events = _run(sim)
    _no_ext_after_fallback(sim, events)
    assert sim.summary()["models"]["usage"]["calls"] == 10
    assert sim.summary()["models"]["usage"]["tripped"] == "calls"
    # 넘친 뒤의 제안 이유는 규칙 문장(가짜)이다.
    fb_seq = next(i for i, (st, _) in enumerate(events) if st == "models.fallback")
    later = [d for st, d in events[fb_seq:] if st == "proposal"]
    assert later and all(p["reason_source"] == "template" for p in later)


def test_run_cost_cap_switches_to_fake(tmp_path: Path) -> None:
    from liot.day_cloud import Price

    pricey = {"jev-latest": Price(1000.0, 1000.0), "claude-haiku-5-5": Price(1000.0, 1000.0),
              "gpt-6-luna": Price(1000.0, 1000.0)}
    sim = DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path, prices=pricey, run_cap=0.5))
    events = _run(sim)
    _no_ext_after_fallback(sim, events)
    assert sim.summary()["models"]["usage"]["tripped"] == "run_cost"


def test_day_cost_cap_from_usage_file_blocks_every_call(tmp_path: Path) -> None:
    (tmp_path / "usage.json").write_text(json.dumps({TODAY: {"usd": 2.5, "calls": 9}}))
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path))
    events = _run(sim)
    assert not any(e[0] == "ext" for e in sim.log)
    assert [d["reason"] for st, d in events if st == "models.fallback"] == ["day_cost"]


def test_usage_file_accumulates_today(tmp_path: Path) -> None:
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path))
    _run(sim)
    book = json.loads((tmp_path / "usage.json").read_text())
    usage = sim.summary()["models"]["usage"]
    assert book[TODAY]["calls"] == usage["calls"]
    assert book[TODAY]["usd"] == pytest.approx(usage["run_usd"], abs=1e-6)


def test_unreadable_usage_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "usage.json").write_text("{not json")
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path))
    events = _run(sim)
    assert not any(e[0] == "ext" for e in sim.log)
    assert [d["reason"] for st, d in events if st == "models.fallback"] == ["usage_unreadable"]


# ── 이유 작성기 · 거주자 결정기 · 집 기억 ─────────────────────────────────────


def test_reasons_come_from_the_cheap_model(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, _, _, _ = cloud7
    scenes = [p for p in sim.proposals if p["scene"]]
    assert scenes and all(p["reason_source"] == "model" for p in scenes)
    assert all(p["reason_model"] == "anthropic:claude-haiku-5-5" for p in scenes)
    assert all(p["reason_ko"] == "오늘 공기가 맑아 환기하기 좋아요." for p in scenes)


def test_invalid_reason_output_falls_back_to_the_rule_sentence(tmp_path: Path) -> None:
    cheap = FakeCheap(reason="Open the window.")
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path, cheap=cheap))
    _run(sim)
    fake = DaySim(seed=7, days=1)
    _run(fake)
    templates = {p["scene"]: p["reason_ko"] for p in fake.proposals}
    for p in sim.proposals:
        assert p["reason_source"] == "template" and p["reason_ko"] == templates[p["scene"]]


def test_odd_resident_answer_falls_back_to_the_persona_table(tmp_path: Path) -> None:
    sim = DaySim(seed=7, days=3, models="cloud", cloud=_kit(tmp_path, cheap=FakeCheap(odd_choice=True)))
    events = _run(sim)
    taps = [d for st, d in events if st == "feedback" and d["source"] in ("persona", "tray")
            and d["kind"] == "comfort"]
    assert taps and all(t["decided_by"] == "persona_table" for t in taps)
    purifier = [p for p in sim.proposals if p["scene"] == "purifier" and p["day"] == 1]
    assert purifier[0]["state"] == "dismissed"  # 성향 표: 공기청정기 싫어함


def test_resident_model_answer_is_used_when_valid(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    sim, events, _, _ = cloud7
    taps = [d for st, d in events if st == "feedback" and d["source"] in ("persona", "tray")
            and d["kind"] == "comfort"]
    assert taps and all(t["decided_by"] == "model" for t in taps)
    vent = [p for p in sim.proposals if p["scene"] == "ventilate"]
    assert vent[0]["state"] == "accepted"


def test_dismiss_is_remembered_as_a_rule_and_accept_as_an_episode(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    _, events, _, brain = cloud7
    rems = brain.remembers()
    rules = [a for a in rems if "--rule" in a]
    assert rules and all("[그만해]" in a[2] and a[a.index("--kind") + 1] == "decision" for a in rules)
    episodes = [a for a in rems if "--rule" not in a and a[a.index("--scope") + 1] == "house-runA"]
    assert any("도움됐다" in a[2] for a in episodes)
    assert all(a[a.index("--kind") + 1] in ("episode", "decision", "preference") for a in rems)
    mem = [d for st, d in events if st == "memory"]
    assert any(m["rule"] for m in mem) and all(m["source_ko"] == "memgineering · liot-demo" for m in mem)


def test_next_reason_input_carries_this_runs_house_memory(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    _, _, cheap, _ = cloud7
    inputs = cheap.reason_inputs()
    assert not inputs[0]["facts"].get("memories")  # 처음에는 기억이 없다
    later = [i for i in inputs if i["facts"].get("memories")]
    assert later and any("도움됐다" in m for i in later for m in i["facts"]["memories"])


def test_house_recall_scopes_and_resident_seed(
    cloud7: tuple[DaySim, Events, FakeCheap, FakeBrain],
) -> None:
    _, _, _, brain = cloud7
    scopes = {a[a.index("--scope") + 1] for a in brain.argvs if a[1] in ("recall", "remember")}
    assert scopes == {"house-runA", RESIDENT_SCOPE}
    seeds = [a for a in brain.remembers() if a[a.index("--scope") + 1] == RESIDENT_SCOPE]
    assert [a[2] for a in seeds] == list(day_sim.RESIDENT_PREFERENCES)


def test_other_runs_memories_do_not_mix(tmp_path: Path) -> None:
    brain = FakeBrain(leaky=True)  # 서버가 범위를 무시하고 다 돌려줘도
    first = DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path / "a", brain=brain),
                   house_id="runA")
    _run(first)
    a_texts = {n["title"] for n in brain.notes if n["scope"] == "house-runA"}
    assert a_texts
    cheap = FakeCheap()
    second = DaySim(seed=7, days=2, models="cloud",
                    cloud=_kit(tmp_path / "b", brain=brain, cheap=cheap), house_id="runB")
    _run(second)
    seen = [m for i in cheap.reason_inputs() for m in i["facts"].get("memories") or []]
    assert seen  # B 도 자기 기억은 읽는다
    assert all("(house-runB)" in m and "house-runA" not in m for m in seen)
    b_scopes = {a[a.index("--scope") + 1] for a in brain.argvs[len(brain.argvs) // 2:]
                if a[1] == "recall"}
    assert "house-runA" not in b_scopes


def test_memory_off_when_use_points_elsewhere(tmp_path: Path) -> None:
    brain = FakeBrain(brain="개인 브레인")
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path, brain=brain))
    events = _run(sim)
    assert [a[1] for a in brain.argvs] == ["use"]
    assert sum(1 for e in sim.log if e == ("ext", "memory")) == 1  # 확인(use) 한 번뿐
    status = next(d for st, d in events if st == "models.status")["status"]
    mem = next(s for s in status if s["part"] == "memory")
    assert mem["state"] == "fake" and "다른 브레인" in mem["reason_ko"]
    assert all(p["memory_used"] == [] for p in sim.proposals)


# ── fake 모드 · 요청 · 키 상태 ───────────────────────────────────────────────


def test_fake_mode_has_no_cloud_fields_or_external_calls() -> None:
    sim = DaySim(seed=7, days=1)
    events = _run(sim)
    assert not any(e[0] == "ext" for e in sim.log)
    assert not {"models", "memory", "luna"} & set(sim.summary())
    assert not any(st.startswith("models.") or st == "memory" for st, _ in events)
    assert not any("reason_source" in d for st, d in events if st == "proposal")


def test_cloud_mode_needs_a_kit_and_models_is_validated() -> None:
    with pytest.raises(ValueError):
        DaySim(models="cloud")
    with pytest.raises(LiveRequestError) as exc:
        day_sim.parse_day_request({"models": "gpt"})
    assert exc.value.field == "models"
    assert day_sim.parse_day_request({"models": "cloud"}).models == "cloud"
    assert day_sim.parse_day_request({}).models == "fake"


def _secrets_file(tmp: Path, mode: int) -> Path:
    path = tmp / "secrets.env"
    path.write_text(f"JEV_API_KEY={KEY}\nOPENAI_API_KEY=\n# ANTHROPIC_API_KEY={KEY}\n")
    path.chmod(mode)
    return path


def test_key_status_has_names_and_presence_only(tmp_path: Path) -> None:
    runs = DayRuns(secrets_path=_secrets_file(tmp_path, 0o600))
    status = runs.key_status()
    assert KEY not in json.dumps(status)
    assert status["keys"] == [{"name": n, "present": n == "JEV_API_KEY"} for n in KNOWN_KEYS]
    assert (status["present"], status["total"], status["status"]) == (1, 4, "ok")


def test_key_status_refuses_a_wide_file(tmp_path: Path) -> None:
    runs = DayRuns(secrets_path=_secrets_file(tmp_path, 0o644))
    status = runs.key_status()
    assert status["status"] == "refused" and status["present"] == 0
    assert KEY not in json.dumps(status)


def test_day_runs_cloud_start_uses_the_kit_factory(tmp_path: Path) -> None:
    built: list[Any] = []

    def factory(secrets: Any) -> CloudKit:
        built.append(secrets.names())
        return _kit(tmp_path)

    runs = DayRuns(secrets_path=_secrets_file(tmp_path, 0o600), kit_factory=factory)
    out = runs.start({"days": 1, "models": "cloud"})
    runs.join(30.0)
    view = runs.view(out["run_id"])
    assert out["models"] == "cloud" and view is not None
    assert built == [["JEV_API_KEY"]]
    assert view["summary"]["models"]["mode"] == "cloud"
    sim = runs._sims[out["run_id"]]
    assert sim.house_id == out["run_id"]


def test_fake_start_never_reads_the_secrets_file(tmp_path: Path) -> None:
    def factory(secrets: Any) -> CloudKit:  # pragma: no cover - 불리면 실패
        raise AssertionError("fake run built a cloud kit")

    runs = DayRuns(secrets_path=tmp_path / "missing.env", kit_factory=factory)
    out = runs.start({"days": 1})
    runs.join(30.0)
    assert "models" not in out



# ── 관제실 API ──────────────────────────────────────────────────────────────


def _http(port: int, method: str, path: str,
          body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = json.loads(resp.read().decode())
    conn.close()
    return resp.status, out


def test_keys_route_and_cloud_run_over_http(tmp_path: Path) -> None:
    import threading

    from liot import demo_server

    runs = DayRuns(secrets_path=_secrets_file(tmp_path, 0o600), kit_factory=lambda s: _kit(tmp_path))
    httpd = demo_server.make_server("127.0.0.1", 0, day_runs=runs)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        port = httpd.server_port
        status, keys = _http(port, "GET", "/api/day/keys")
        assert status == 200 and KEY not in json.dumps(keys)
        assert set(keys) == {"ok", "status", "reason_ko", "keys", "present", "total"}
        assert all(set(k) == {"name", "present"} for k in keys["keys"])
        status, bad = _http(port, "POST", "/api/day/run", {"models": "gpt"})
        assert status == 400 and bad["field"] == "models"
        status, started = _http(port, "POST", "/api/day/run", {"days": 1, "models": "cloud"})
        assert status == 200 and started["models"] == "cloud"
        runs.join(30.0)
        status, view = _http(port, "GET", f"/api/day/runs/{started['run_id']}")
        assert status == 200 and view["summary"]["models"]["usage"]["calls"] > 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_broken_model_config_is_503_and_creates_no_run(tmp_path: Path) -> None:
    from liot.cheap_llm import ModelConfigError

    def factory(secrets: Any) -> CloudKit:
        raise ModelConfigError("bad")

    runs = DayRuns(secrets_path=tmp_path / "missing.env", kit_factory=factory)
    with pytest.raises(LiveRequestError) as exc:
        runs.start({"days": 1, "models": "cloud"})
    assert exc.value.status == 503 and runs.runs.run_ids() == []


# ── 부품 묶음·설정 ────────────────────────────────────────────────────────────


def test_build_cloud_kit_uses_only_present_keys(tmp_path: Path) -> None:
    from liot.day_cloud import build_cloud_kit
    from liot.secrets_env import Secrets

    secrets = Secrets({"JEV_API_KEY": KEY, "ANTHROPIC_API_KEY": KEY})
    kit = build_cloud_kit(secrets, memory=MemoryClient(home=HOME, runner=FakeBrain()),
                          today=lambda: TODAY, usage_path=tmp_path / "usage.json")
    assert isinstance(kit.judge, JevBackend) and kit.cheap is not None
    parts = {st["part"]: st for st in kit.status}
    assert parts["judge"]["state"] == "cloud" and parts["reason"]["state"] == "cloud"
    assert parts["luna"]["state"] == "fake"  # 출하 설정은 unavailable
    assert parts["memory"]["state"] == "pending"
    assert KEY not in json.dumps(kit.status)


def test_build_cloud_kit_without_keys_is_all_fake(tmp_path: Path) -> None:
    from liot.day_cloud import build_cloud_kit
    from liot.secrets_env import Secrets

    kit = build_cloud_kit(Secrets(status="missing", reason_ko="비밀 파일이 없습니다."),
                          memory=MemoryClient(home=HOME, runner=FakeBrain()), today=lambda: TODAY,
                          usage_path=tmp_path / "usage.json")
    assert kit.judge is None and kit.cheap is None
    states = {st["part"]: (st["state"], st["reason_ko"]) for st in kit.status}
    assert states["judge"] == ("fake", "비밀 파일이 없습니다.")
    assert states["reason"][0] == "fake" and states["resident"][0] == "fake"
    sim = DaySim(seed=7, days=1, models="cloud", cloud=kit)
    _run(sim)
    assert not any(e == ("ext", "judge") for e in sim.log)
    assert all(p["reason_source"] == "template" for p in sim.proposals)
    assert (tmp_path / "usage.json").exists()  # 시험은 사용자 home 의 사용량 파일을 쓰지 않는다


@pytest.mark.parametrize("text", [
    "{}",
    "day_cloud: {call_cap_per_run: 0, cost_cap_per_run_usd: 1, cost_cap_per_day_usd: 1, usage_path: u}",
    "day_cloud: {call_cap_per_run: 5, cost_cap_per_run_usd: 0, cost_cap_per_day_usd: 1, usage_path: u}",
    "day_cloud: {call_cap_per_run: 5, cost_cap_per_run_usd: 1, cost_cap_per_day_usd: 1}",
    "day_cloud: {call_cap_per_run: 5, cost_cap_per_run_usd: 1, cost_cap_per_day_usd: 1, usage_path: u}\n"
    "prices: {m: {input: -1, output: 0}}",
    "day_cloud: {call_cap_per_run: 5, cost_cap_per_run_usd: 1, cost_cap_per_day_usd: 1, usage_path: u}\n"
    "prices: [1]",
])
def test_bad_cloud_settings_are_rejected(tmp_path: Path, text: str) -> None:
    from liot.cheap_llm import ModelConfigError

    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelConfigError):
        load_cloud_settings(path)


def test_shipped_cloud_settings_and_prices() -> None:
    limits, prices = load_cloud_settings()
    assert limits.call_cap_per_run == 120
    assert (limits.cost_cap_per_run_usd, limits.cost_cap_per_day_usd) == (0.5, 2.0)
    assert limits.usage_path == Path.home() / ".config" / "liot" / "usage.json"
    assert set(prices) == {"claude-haiku-5-5", "gemini-3.8-flash", "gpt-6-luna", "jev-latest"}
    assert prices["jev-latest"].estimate is True


def test_unknown_price_and_missing_tokens_are_counted(tmp_path: Path) -> None:
    limits = CloudLimits(10, 1.0, 1.0, tmp_path / "u.json")
    meter = UsageMeter(limits, {}, today=lambda: TODAY)
    assert meter.allow()
    assert meter.record("reason", "mystery-model", seconds=0.1, sent_chars=100) == 0.0
    summary = meter.summary()
    assert summary["unknown_price_calls"] == 1 and summary["estimated_calls"] == 1
    assert summary["avg_seconds"] == 0.1


def test_unwritable_usage_file_trips(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    meter = UsageMeter(CloudLimits(10, 1.0, 1.0, blocker / "u.json"), {}, today=lambda: TODAY)
    assert meter.allow()
    meter.record("memory", None, seconds=0.0)
    assert meter.tripped == "usage_unwritable" and not meter.allow()
    assert meter.trip_reason_ko()


def test_tap_in_the_alert_step_writes_memory_only_after_the_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """거주자가 누를 차례인 단계에 안전 이상이 겹치면: 알림 → 그다음 기억 쓰기."""
    import dataclasses

    probe = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path / "probe"))
    _run(probe)
    vent = next(p for p in probe.proposals if p["scene"] == "ventilate")
    real_script = day_sim.build_script

    def script(seed: int, days: int) -> list[day_sim.DayPlan]:
        plans = real_script(seed, days)
        first = plans[0]
        other = GAS if first.safety == "leak" else LEAK
        k = next(i for i, s in enumerate(first.steps) if s.at_local.strftime("%H:%M") == vent["hhmm"])
        steps = list(first.steps)
        for i in (k + 1, k + 2):
            steps[i] = dataclasses.replace(steps[i], readings={**steps[i].readings, other: True})
        return [dataclasses.replace(first, steps=tuple(steps)), *plans[1:]]

    monkeypatch.setattr(day_sim, "build_script", script)
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path / "run"))
    events = _run(sim)
    alert = next(s for s in sim.safety_events if s["kind"] == "first_alert")
    seg = next(seg for seg in _segments(sim.log)
               if ("send", sim._tracked[alert["id"]].pid) in [e for _, e in seg])
    send_at = next(i for i, e in seg if e == ("send", sim._tracked[alert["id"]].pid))
    memory_calls = [i for i, e in seg if e == ("ext", "memory")]
    assert memory_calls, "the persona tap must land in the alert step"
    assert send_at < min(memory_calls)
    taps = [d for st, d in events if st == "feedback" and d["id"] == vent["id"]]
    assert taps and taps[0]["status"] == "200"


def test_safety_reading_is_applied_before_other_readings_of_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 단계에 다른 센서 값이 먼저 와서 외부 호출을 부르더라도, 안전 값을 먼저 반영한다."""
    import dataclasses

    from liot.service import LiotService as real

    holder: list[DaySim] = []

    class Probe(real):
        async def handle_reading(self, reading: Any) -> Any:
            if reading.entity_id not in (LEAK, GAS) and holder:
                holder[0].log.append(("ext", "probe"))  # 다른 값이 외부 호출을 불렀다고 치자
            return await super().handle_reading(reading)

    real_script = day_sim.build_script

    def script(seed: int, days: int) -> list[day_sim.DayPlan]:
        plans = real_script(seed, days)
        first = plans[0]
        sensor = LEAK if first.safety == "leak" else GAS
        k = next(i for i, s in enumerate(first.steps) if s.readings[sensor])
        steps = list(first.steps)
        # 경보 단계에 움직임 값도 바뀌게 한다(대본 순서상 움직임이 안전 센서보다 앞이다).
        steps[k] = dataclasses.replace(
            steps[k], readings={**steps[k].readings,
                                day_sim.MOTION: not steps[k - 1].readings[day_sim.MOTION]})
        return [dataclasses.replace(first, steps=tuple(steps)), *plans[1:]]

    monkeypatch.setattr(day_sim, "LiotService", Probe)
    monkeypatch.setattr(day_sim, "build_script", script)
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path))
    holder.append(sim)
    _run(sim)
    alert = next(s for s in sim.safety_events if s["kind"] == "first_alert")
    pid = sim._tracked[alert["id"]].pid
    seg = next(seg for seg in _segments(sim.log) if ("send", pid) in [e for _, e in seg])
    send_at = next(i for i, e in seg if e == ("send", pid))
    probes = [i for i, e in seg if e == ("ext", "probe")]
    assert probes and send_at < min(probes)
