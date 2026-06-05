# LIOT — 능동형 스마트홈 AI (Phase1)

LIOT 는 집의 신호를 읽어 **위험도에 따라 사람에게 "제안"** 하는 능동형
스마트홈 AI 다. Phase1 은 **제안-만(suggestion-only)** 수직 슬라이스로,
실제로 실행되고 `pytest` 가 통과하는 4-Layer 위험도 파이프라인을 구현한다.

```
L1 Sensing  →  L2 Context  →  L3 Judgment(Gate1 가드레일 + Gate2 LLM)  →  L4 Execution Safety
```

위험도 4단계(심각도 오름차순): **ambient(잔잔 ●) < breath(숨결 ✓) < dusk(땅거미 ?) < ember(불씨 🛡)**.

> Phase1 범위 = **제안만**. 액추에이터를 실제로 구동하는 코드는 존재하지 않는다.
> L4 에는 구체 `Executor` 클래스가 없으며, `assert_executor_absent()` 가 이를 런타임에 검증한다.

---

## 런타임이 강제하는 7대 불변식

1. **단방향 파이프라인** — EventBus 는 `(L1→L2, L2→L3, L3→L4, L4→L2)` 만 허용한다.
   그 외 publish 는 `TopologyViolation`.
2. **Gate2(LLM)는 Gate1을 무를 수 없다** — 최종 위험도 = `escalate_only(gate1, gate2)` = `max(...)`.
3. **제안-만 모델** — 액추에이터 실행 코드/구체 Executor 0개. `assert_executor_absent()` 가 검증.
4. **재현 가능한 판단** — 모든 `Judgment` 에 `snapshot_hash` + `bundle_id` + `rationale` 동시 저장.
5. **승격만, 강등 불가** — `escalate_only(cur, target) = max` + 런타임 단조 가드(`assert_monotonic`).
6. **Safety-Never-Silent** — 안전 임계 카테고리
   (`gas/smoke/door/leak/high_temp/lock/water_main/oven`)는 LLM 가용성과 무관하게
   Gate1 결정론만으로 즉시 ember 제안을 만든다.
7. **능동 제안 = 컴포트 전용 + breath 상한 (Proactive-Safe-Silent)** — 능동
   생성기가 발의하는 후보는 (a) `PROACTIVE_ALLOWED` 허용 목록 안이어야 하고
   (lock.unlock/valve.open/cover.open 등 위험 방향은 **선언 자체가 거부**),
   (b) 안전 카테고리 엔티티를 타깃할 수 없으며, (c) 카탈로그 위험도·게이트
   판정·LLM 승격 어느 쪽이든 breath 를 넘으면 **표출하지 않는다**(억제 =
   침묵, 강등 아님). 불변식 6과 쌍을 이루는 비대칭: 안전은 침묵이 위험하고,
   컴포트는 침묵이 안전하다. 로드(`proactive_rules`)·생성(`validate_proactive_candidate`)·
   판단 후(app 상한) 3중 deny-by-default 로 강제하며, 인젝션된 LLM 의 최악
   결과는 '침묵'이다.

---

## 디렉터리

```
liot/
  core/         risk, contracts, invariants, event_bus, scoped_repo, egress_gate, clock
  db/           schema.sql, migrations, database
  layers/
    l1_sensing/  base, fake_source, ha_bridge, state_cache, normalizer
    l2_context/  context_builder, preference_store, proactive, proactive_rules
    l3_judgment/ rules_loader, guardrail, prompt_registry, llm_judge
    l4_execution/ device_catalog, risk_router, proposal_service, notifier
  app.py        파이프라인 글루(LiotApp) + 능동 틱(tick)
  demo.py       실행 가능한 데모
config/
  device_catalog.yaml
  rules/        emergency.yaml, night_quiet.yaml   (Gate1: 하한 승격만)
  proactive/    comfort.yaml                       (능동: 컴포트 발의만)
tests/          불변식 / Gate1 / risk_router / egress / e2e / proactive
```

---

## 설치 / 실행 / 테스트 (uv)

```bash
# 의존성 설치 (개발 도구 포함)
uv pip install -e ".[dev]"

# 데모 실행 (일상 light / 야간 thermostat lower / 가스누출 emergency)
uv run python -m liot.demo

# 테스트
uv run python -m pytest -q
```

`pip` 만 쓴다면:

```bash
python3 -m pip install -e ".[dev]"
python3 -m liot.demo
python3 -m pytest -q
```

---

## 기술 제약

- Python 3.11+ (3.13 실행). 의존성: `pydantic` v2, `pyyaml`, `aiosqlite`,
  `pytest`, `pytest-asyncio` 만.
- 단일 프로세스 asyncio. 테스트는 네트워크를 호출하지 않는다.
- LLM 기본 = 결정론적 `FakeLLM`. 환경변수 `LIOT_LLM_BACKEND=ollama` 설정 시
  실제 ollama(gemma 계열) 백엔드 사용, 미설정 시 FakeLLM.
- Home Assistant 브리지(`ha_bridge`)는 실제 WebSocket 연결을 지원한다
  (extra `[ha]`; 아래 "Home Assistant 실연동" 참고). 테스트는 mock 연결 사용.
- 모든 외부 송신은 `core/egress_gate.emit_external` 단일 통로를 통과한다.
  ntfy 페이로드에는 디바이스명·방이름·rationale 가 절대 포함되지 않는다.
  **LLM 송신도** `OllamaBackend` 가 `emit_external(LLM, …)` 를 통과시켜
  raw entity_id·이름이 가명 토큰으로 치환된 뒤 모델로 나간다(SE-D3).

---

## Gate2 실연결 (ollama · gemma via `ssh home`)

로컬 우선 원칙에 맞춰 외부 API 대신 홈 서버의 로컬 모델을 쓴다. ollama 가
`127.0.0.1` 에만 바인딩돼 있어도 기존 SSH 별칭으로 호출하므로 포트 노출이
필요 없다.

```bash
make smoke      # 설치 모델 자동 감지(gemma*) 후 실제 판정. home 오프라인이면 degraded 시연.
make demo-live  # 전체 앱을 실제 모델에 붙여 데모(= LIOT_LLM_BACKEND=ollama python -m liot.demo)
```

환경변수: `LIOT_OLLAMA_MODEL`(예 `gemma3:4b`), `LIOT_OLLAMA_TRANSPORT`(`ssh`|`http`),
`LIOT_OLLAMA_SSH_HOST`(기본 `home`), `LIOT_OLLAMA_HTTP_URL`, `LIOT_OLLAMA_TIMEOUT`.

**fail-closed 보장**: 모델 도달 불가·타임아웃·비-JSON·PII 차단(EgressViolation)은
모두 `LLMUnavailable` 로 강등되어 degraded fail-safe 로 떨어진다. 안전 임계
카테고리(가스/누수/잠금 등)는 LLM 상태와 무관하게 **EMBER 를 유지**한다
(불변식 6, Safety-Never-Silent). `tests/test_ollama_backend.py` 가 이를 검증.

---

## Home Assistant 실연동 (L1)

`HABridge` 가 HA WebSocket API(`auth_required → auth → auth_ok →
subscribe_events(state_changed)`)에 붙어 상태 변화를 `SensorReading` 으로
스트리밍한다. `HAConnection` Protocol 로 실제 연결(`WebsocketsHAConnection`,
extra `[ha]`)과 테스트용 `FakeHAConnection` 을 교체한다.

**핵심: 안전 카테고리는 domain 이 아니라 `device_class` 에 있다.**
`binary_sensor.kitchen_gas` 는 domain 이 'binary_sensor' 라 도메인 추론만으로는
안전이 아니다. `HABridge.entity_categories()` 가 `(domain, device_class)` →
LIOT 카테고리 맵을 만들어 `LiotApp(categories=...)` 로 주입하면 가스/연기/누수가
정확히 안전 카테고리로 분류된다.

```bash
# 실제 HA에 연결(장기 액세스 토큰 필요)
LIOT_HA_URL=ws://<host>:8123/api/websocket LIOT_HA_TOKEN=*** \
  .venv/bin/python scripts/ha_bridge_live.py
```

검증: `tests/test_ha_bridge.py` (단위 + 로컬 mock HA 서버에 진짜 websockets
왕복 + 가스 이벤트 → EMBER 제안 end-to-end).

### 센서/액추에이터 분리 (관찰 vs 제안)

읽기전용 센서가 모두 ember 로 과승격되던 알림 피로 문제를 해소했다. 제안은
다음 둘 중 하나일 때만 생성된다:

  - **명시적 action** 이 주어졌을 때(프로액티브 생성기/요청), 또는
  - **안전 임계 카테고리**(gas/smoke/leak/door/lock/...)일 때(자동 알림).

그 외 모든 관찰(temperature/humidity/motion 등 양성 센서, 액추에이터 상태변화)은
**맥락만 갱신하고 침묵**한다. 따라서 실제 HA 집의 수십 개 센서가 끊임없이 변해도
ember 폭주가 없고, 가스/누수/잠금 같은 안전 이벤트와 프로액티브 제안만 사용자에게
도달한다. `tests/test_disposition.py` 가 검증(양성 센서 20건 스트림 → 제안 0건,
가스 → ember 1건).

---

## 능동 생성기 (Phase2) — "AI 가 먼저 말을 건다"

`ProactiveGenerator`(L2)가 맥락(현재 상태 + 시간대 + 재실)을 읽고 도움이 될
액추에이터 행동을 **후보로 발의**한다. 후보는 기존 Gate1+Gate2+Router 를
그대로 통과한다 — 생성기는 "무엇을 고려할지"만 고르고, 위험도/확인 강도는
게이트가 정한다. 실행은 여전히 없다(불변식 3).

```bash
# 주기 진입점 — 센서 cadence 와 분리(스냅샷 양성 억제 불변)
proposal = await app.tick()   # 결정 없음/쿨다운/예산/검증실패 → None(침묵)
```

  - **Tier A 결정론 규칙**(`config/proactive/*.yaml`): 닫힌 비교자
    (`eq/ne/lt/le/gt/ge`)의 상태 술어 + enum 매치(`time_bucket`/`occupancy`).
    스크립트/표현식 불가. 로더가 `PROACTIVE_ALLOWED` 밖 선언을 거부한다.
  - **LLM 의 역할 = 거부권**: 능동 후보를 *발의*하는 LLM 은 없다. 기존
    Gate2 가 후보를 판정하고, breath 위로 승격하면 그 제안은 침묵한다
    (불변식 7) — 인젝션된 LLM 의 최악 결과는 침묵이다.
  - **알림 피로 게이팅(생성 시)**: (타깃, 액션)별 쿨다운 60분 + 시간대당
    1건 + 하루 3건(`limits:`, 데이터 주도). L4 notifier 의 표출 억제와
    별개·더 보수적 — 수용률(Proposal-Accept-Rate ≥ 60%)이 North Star.
  - **재현성(불변식 4)**: 능동 결정 자체가 스냅샷의 `trigger` 가 되어
    `snapshot_hash` 가 판단된 후보를 고정한다. 출처는
    `params["__origin__"] = proactive:rule:<id>@<ruleset_hash 8자리>` 로
    영속된다(explicit:external / safety:auto 와 함께 3경로 전부 출처 보유).
  - **신선도**: 능동 제안은 `expires_at`(규칙별 ttl)을 갖는다. `perishable`
    은 False — L4 에서 perishable 은 예산 *우회* 의미라 컴포트에 쓰면
    역효과(잔소리)다.
  - **egress 경화**: LLM payload 에는 방향성 스칼라 params 만 투영되고
    (`__origin__`/타깃 식별자 불출), LLM 목적지 한정으로 entity_id 형태
    문자열이 남으면 송신 자체를 거부한다(fail-closed).

검증: `tests/test_proactive.py` (로더 deny-by-default / 발의·침묵 결정론 /
LLM 폭주 → 침묵 + 안전 경로 무손상 / egress 불출 / occupancy fact 부활).
