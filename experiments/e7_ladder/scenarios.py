"""H6.1 conformance: ladder scenarios on the fake clock against the spec in business-rules.md.

Spec times are written as LITERALS here (seconds after the situation opened), taken from
docs/business-rules.md '알림 사다리', not imported from the code under test:

  * first resident alert: 0 s
  * CRITICAL re-alert to the resident: every 60 s until ack/resolution
  * WARNING re-alert: 600, 1200, 1800 s (at most 3), then none
  * guardian forward (floor CRITICAL only): 90 s, all consenting guardians in the same tick
  * 119 guidance (floor CRITICAL only): 90 s + 300 s = 390 s after opening, even with 0 guardians
    (to the resident and to forwarded guardians who still consent)
  * a failed guardian forward / guidance send is retried 30 s later
  * resolution: 'resolved' to the resident at once, and to forwarded guardians who still consent

Clock drivers: ``event`` jumps to the next due time the service stored (ideal daemon);
``tick5`` steps 5 s at a time from t = 0 and ``tick5_p<phase>`` from t = phase (the daemon's real
ladder resolution is LADDER_RESOLUTION_S = 5 s and its ticks are not aligned with the moment a
situation opened, so lateness up to 5 s is possible by design).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from experiments.common.service_kit import (
    GAS,
    SECRET,
    FixedBackend,
    HighestBackend,
    Kit,
    TimedTransport,
    build_service,
    reading,
)
from liot.layers.l4_execution.notifier import sign_feedback, sign_guardian_ack

logging.getLogger("liot").setLevel(logging.CRITICAL)
DOOR = "binary_sensor.front_door"
STEP5 = 5.0
Event = tuple[str, str, float]  # (kind, target, seconds since opening)


@dataclass
class Scenario:
    sid: str
    title: str
    entity: str = GAS
    n_guardians: int = 0
    revoked_at_start: tuple[int, ...] = ()
    vulnerable: bool = False
    backend: str = "fixed"
    horizon: float = 700.0
    fail_guardian_windows: tuple[tuple[float, float], ...] = ()  # (from, until) seconds
    actions: list[tuple[float, str, Any]] = field(default_factory=list)  # (t, name, arg)
    expected: list[Event] = field(default_factory=list)
    note: str = ""


def crit_realerts(until: float, start: float = 60.0) -> list[Event]:
    out: list[Event] = []
    t = start
    while t <= until:
        out.append(("realert", "resident", t))
        t += 60.0
    return out


def warn_realerts(until: float) -> list[Event]:
    return [("realert", "resident", t) for t in (600.0, 1200.0, 1800.0) if t <= until]


def build_scenarios() -> list[Scenario]:
    S: list[Scenario] = []
    first: Event = ("alert", "resident", 0.0)

    S.append(Scenario(
        "S01", "위험 · 무응답 · 보호자 0명", horizon=700,
        expected=[first, *crit_realerts(700), ("guidance", "resident", 390.0)],
        note="보호자가 없어도 119 안내는 90+300초에 거주자에게 간다",
    ))
    S.append(Scenario(
        "S02", "위험 · 무응답 · 보호자 1명(동의)", n_guardians=1, horizon=700,
        expected=[first, *crit_realerts(700), ("forward", "g0", 90.0),
                  ("guidance", "resident", 390.0), ("guidance", "g0", 390.0)],
    ))
    S.append(Scenario(
        "S03", "위험 · 무응답 · 보호자 3명(동시)", n_guardians=3, horizon=700,
        expected=[first, *crit_realerts(700),
                  *[("forward", f"g{i}", 90.0) for i in range(3)],
                  ("guidance", "resident", 390.0),
                  *[("guidance", f"g{i}", 390.0) for i in range(3)]],
    ))
    S.append(Scenario(
        "S04", "거주자 확인 45초(전달 전)", n_guardians=1, horizon=700,
        actions=[(45.0, "resident_ack", None)],
        expected=[first],
        note="45초에 확인하면 재알림·전달·119 안내가 없다",
    ))
    S.append(Scenario(
        "S05", "거주자 확인 130초(전달 뒤·119 전)", n_guardians=1, horizon=700,
        actions=[(130.0, "resident_ack", None)],
        expected=[first, ("realert", "resident", 60.0), ("realert", "resident", 120.0),
                  ("forward", "g0", 90.0)],
    ))
    S.append(Scenario(
        "S06", "거주자 확인 500초(119 안내 뒤)", n_guardians=1, horizon=700,
        actions=[(500.0, "resident_ack", None)],
        expected=[first, *crit_realerts(500), ("forward", "g0", 90.0),
                  ("guidance", "resident", 390.0), ("guidance", "g0", 390.0)],
    ))
    S.append(Scenario(
        "S07", "보호자 확인 130초", n_guardians=1, horizon=700,
        actions=[(130.0, "guardian_ack", 0)],
        expected=[first, ("realert", "resident", 60.0), ("realert", "resident", 120.0),
                  ("forward", "g0", 90.0)],
    ))
    S.append(Scenario(
        "S08", "동의 철회 60초(전달 전) · 보호자 2명 중 1명", n_guardians=2, horizon=700,
        actions=[(60.0 + 7.0, "revoke", 1)],
        expected=[first, *crit_realerts(700), ("forward", "g0", 90.0),
                  ("guidance", "resident", 390.0), ("guidance", "g0", 390.0)],
        note="철회한 보호자(g1)에게는 아무것도 가지 않는다",
    ))
    S.append(Scenario(
        "S09", "동의 철회 200초(전달 뒤·119 전) · 보호자 2명 중 1명", n_guardians=2, horizon=700,
        actions=[(200.0, "revoke", 1)],
        expected=[first, *crit_realerts(700), ("forward", "g0", 90.0), ("forward", "g1", 90.0),
                  ("guidance", "resident", 390.0), ("guidance", "g0", 390.0)],
        note="전달은 이미 나갔고, 철회 뒤의 119 안내는 g1 에게 가지 않는다",
    ))
    S.append(Scenario(
        "S10", "위험 상황 해소 150초 · 보호자 1명", n_guardians=1, horizon=700,
        actions=[(150.0, "normal", None)],
        expected=[first, ("realert", "resident", 60.0), ("realert", "resident", 120.0),
                  ("forward", "g0", 90.0), ("resolved", "resident", 150.0),
                  ("resolved", "g0", 150.0)],
    ))
    S.append(Scenario(
        "S11", "취약 거주자 + 문 열림(경고→위험)", entity=DOOR, n_guardians=1, vulnerable=True,
        horizon=700,
        expected=[first, *crit_realerts(700), ("forward", "g0", 90.0),
                  ("guidance", "resident", 390.0), ("guidance", "g0", 390.0)],
        note="표 경고 + 취약 거주자 한 단계 가산 = 위험 하한: 위험 사다리와 전달",
    ))
    S.append(Scenario(
        "S12", "문 열림(경고) · 보호자 1명", entity=DOOR, n_guardians=1, horizon=2400,
        expected=[first, *warn_realerts(2400)],
        note="경고 상황은 600·1200·1800초 재알림 뒤 끝, 보호자 전달·119 없음",
    ))
    S.append(Scenario(
        "S13", "문 열림 · 모델만 위험으로 올림", entity=DOOR, n_guardians=1, backend="highest",
        horizon=400,
        expected=[first, ("alert", "resident", 0.0), *crit_realerts(400)],
        note="모델 상향은 재알림 주기를 1분으로 바꾸지만 보호자·119 는 없다",
    ))
    S.append(Scenario(
        "S14", "보호자 송신 실패(0~200초) 뒤 복구", n_guardians=1, horizon=700,
        fail_guardian_windows=((0.0, 200.0),),
        expected=[first, *crit_realerts(700), ("forward", "g0", 210.0),
                  ("guidance", "resident", 510.0), ("guidance", "g0", 510.0)],
        note="실패한 전달은 30초 간격 재시도: 90·120·150·180 실패, 210 성공(복구 200초). 119 안내는 전달 5분 뒤(510)",
    ))
    S.append(Scenario(
        "S15", "헌장 OFF · 보호자 1명", n_guardians=1, horizon=700,
        actions=[(0.0, "charter_off", None)],
        expected=[("forward", "g0", 90.0), ("guidance", "g0", 390.0)],
        note="문서화된 동작: 헌장 OFF 는 거주자 알림만 막고 보호자 전달은 막지 않는다",
    ))
    S.append(Scenario(
        "S16", "보호자 동의 없음(철회 상태) 1명", n_guardians=1, revoked_at_start=(0,), horizon=700,
        expected=[first, *crit_realerts(700), ("guidance", "resident", 390.0)],
        note="동의 없는 보호자에게는 전달·안내가 없다",
    ))
    return S


# ── driver ───────────────────────────────────────────────────────────────────
def _classify(topic: str, payload: dict[str, Any], gtopics: dict[str, str], seen_pids: set[str],
              t: float) -> tuple[str, str]:
    who = gtopics.get(topic, "resident")
    label = str(payload.get("situation_label", ""))
    if "proposal_id" in payload:
        pid = payload["proposal_id"]
        if not seen_pids:
            seen_pids.add(pid)
            return "alert", who
        if pid in seen_pids:
            return "realert", who  # a re-send of a proposal already sent
        seen_pids.add(pid)
        if payload.get("severity_label") == "주의":
            return "resolved", who
        return "alert", who  # a model follow-up proposal (new proposal id) at opening
    if "해소" in label:
        return "resolved", who
    if "119" in label:
        return "guidance", who
    return "forward", who


def _match(expected: list[Event], actual: list[Event]) -> dict[str, Any]:
    """Greedy match by (kind, target) in time order; error = actual - expected seconds."""
    exp = sorted(expected, key=lambda e: (e[0], e[1], e[2]))
    act = sorted(actual, key=lambda e: (e[0], e[1], e[2]))
    pairs: list[dict[str, Any]] = []
    used = [False] * len(act)
    missing: list[Event] = []
    for e in exp:
        best = None
        for i, a in enumerate(act):
            if used[i] or (a[0], a[1]) != (e[0], e[1]):
                continue
            if best is None or abs(a[2] - e[2]) < abs(act[best][2] - e[2]):
                best = i
        if best is None:
            missing.append(e)
        else:
            used[best] = True
            pairs.append({"kind": e[0], "target": e[1], "expected_s": e[2],
                          "actual_s": act[best][2], "error_s": act[best][2] - e[2]})
    extra = [a for i, a in enumerate(act) if not used[i]]
    errs = [abs(p["error_s"]) for p in pairs]
    return {
        "pairs": pairs, "missing": missing, "extra": extra,
        "max_abs_error_s": max(errs) if errs else 0.0,
    }


async def _next_due(kit: Kit) -> float | None:
    due: list[float] = []
    for lad in await kit.svc.safety.list_open_ladders():
        if lad.next_fire_at is not None:
            dt = _parse(lad.next_fire_at)
            due.append((dt - kit.t0).total_seconds())
    return min(due) if due else None


def _parse(text: str):  # type: ignore[no-untyped-def]
    from liot.layers.l4_execution.escalation import parse_iso

    return parse_iso(text)


async def run_scenario(sc: Scenario, driver: str) -> dict[str, Any]:
    backend = HighestBackend() if sc.backend == "highest" else FixedBackend()
    tr = TimedTransport()
    kit = await build_service(
        backend=backend, transport=tr, categories={GAS: "gas", DOOR: "door"},
    )
    clock_before = kit.clock
    tr.clock = clock_before
    svc = kit.svc
    try:
        rid = await svc.safety.add_resident(kit.home_id, "R0")
        if sc.vulnerable:
            await svc.safety.set_trait(rid, "age_75_plus", True, "resident")
        gids: list[str] = []
        gtopics: dict[str, str] = {}
        for i in range(sc.n_guardians):
            gid, topic = await svc.safety.add_guardian(kit.home_id, f"G{i}")
            gids.append(gid)
            gtopics[topic] = f"g{i}"
            if i not in sc.revoked_at_start:
                await svc.safety.grant_consent(kit.home_id, gid, rid)
        topic_of = {v: k for k, v in gtopics.items()}
        actions = sorted(sc.actions, key=lambda a: a[0])
        pending = list(actions)
        failed_seen = 0
        attempts: list[Event] = []
        now = 0.0
        # open the situation at t = 0 (charter action at t=0 happens first)
        while pending and pending[0][0] <= 0.0:
            await _do(kit, sc, pending.pop(0), gids, rid, tr)
        first_res = await svc.handle_reading(reading(sc.entity, "on"))
        await svc.drain_phase2()  # phase-2 follow-ups belong to t = 0
        send_times: list[float] = [0.0] * len(tr.stamps)  # driver time of every recorded send
        sit_id = (await svc.safety.get_open_situation(kit.home_id, "gas" if sc.entity == GAS
                                                      else "door", sc.entity))
        sid = sit_id.situation_id if sit_id else ""
        iters = 0
        while now < sc.horizon and iters < 20000:
            iters += 1
            if driver.startswith("tick5"):
                phase = float(driver.split("_p")[1]) if "_p" in driver else 0.0
                nxt = phase if now < phase and now == 0.0 and phase > 0.0 else now + STEP5
            else:
                due = await _next_due(kit)
                cands = [c for c in (due, pending[0][0] if pending else None) if c is not None]
                nxt = max(min(cands) if cands else sc.horizon, now + 1e-3)
            nxt = min(nxt, sc.horizon)
            now = nxt
            kit.clock.set_fixed(kit.t0 + timedelta(seconds=now))
            for w_from, w_until in sc.fail_guardian_windows:
                for t in gtopics:
                    if w_from <= now < w_until:
                        tr.fail_topics.add(t)
                    else:
                        tr.fail_topics.discard(t)
            while pending and pending[0][0] <= now:
                await _do(kit, sc, pending.pop(0), gids, rid, tr, sid=sid, topic_of=topic_of,
                          first=first_res)
            send_times += [now] * (len(tr.stamps) - len(send_times))
            await svc.ladder_tick()
            send_times += [now] * (len(tr.stamps) - len(send_times))
            if len(tr.failed) > failed_seen:
                for _ in range(len(tr.failed) - failed_seen):
                    attempts.append(("attempt_failed", "guardian", now))
                failed_seen = len(tr.failed)
            if driver == "event" and now >= sc.horizon:
                break
        await svc.drain_phase2()
        send_times += [now] * (len(tr.stamps) - len(send_times))
        actual: list[Event] = []
        seen_pids: set[str] = set()
        # exact driver time of each send (the service clock string is truncated to whole seconds)
        for st, t in zip(tr.stamps, send_times, strict=True):
            kind, who = _classify(st[2], st[3], gtopics, seen_pids, t)
            actual.append((kind, who, t))
        # a model follow-up proposal (S13) shows up as a second 'alert'
        res = _match(sc.expected, actual)
        return {
            "scenario": sc.sid, "title": sc.title, "driver": driver, "note": sc.note,
            "expected": sc.expected, "actual": actual, "failed_attempts": attempts,
            "match": res, "conformant": (not res["missing"] and not res["extra"]
                                         and res["max_abs_error_s"] < 1.0),
            "max_abs_error_s": res["max_abs_error_s"],
            "n_missing": len(res["missing"]), "n_extra": len(res["extra"]),
        }
    finally:
        await kit.close()


async def _do(kit: Kit, sc: Scenario, action: tuple[float, str, Any], gids: list[str], rid: str,
              tr: TimedTransport, sid: str = "", topic_of: dict[str, str] | None = None,
              first: Any = None) -> None:
    svc = kit.svc
    _t, name, arg = action
    if name == "revoke":
        await svc.safety.revoke_consent(kit.home_id, gids[arg], rid)
    elif name == "charter_off":
        await svc.prefs.set("charter_enabled", False)
    elif name == "normal":
        await svc.handle_reading(reading(sc.entity, "off"))
    elif name == "resident_ack" and first is not None:
        prop = first.proposal
        tier_int = int(prop.risk_tier)
        nonce = svc._nonce.get(prop.proposal_id, "")
        sig = sign_feedback(SECRET, prop.proposal_id, tier_int, "ok", nonce)
        await svc._route_feedback(
            f"GET /feedback?p={prop.proposal_id}&t={tier_int}&n={nonce}&sig={sig}&a=ok HTTP/1.1"
        )
    elif name == "guardian_ack":
        gid = gids[arg]
        sig = sign_guardian_ack(SECRET, sid, gid)
        await svc._handle_guardian_ack(sid, gid, sig)


async def run_all(driver: str) -> list[dict[str, Any]]:
    out = []
    for sc in build_scenarios():
        out.append(await run_scenario(sc, driver))
    return out

