# LIOT — 능동형 스마트홈 AI

LIOT 는 집 안팎의 신호·이력·외부 데이터를 종합해 **행동 후보를 여럿 만들고
(무행동 포함), 평가·선택한 뒤, 위험도에 맞는 개입 방식으로 사람에게 닿는**
능동형 스마트홈 AI 다. 실제로 실행되고 `pytest` 가 통과하는 4-Layer
파이프라인이며, 개념적으로는 8단계를 구현한다:

```
버스:   L1 Sensing → L2 Context → L3 Judgment(Gate1 안전게이트 + Gate2 LLM) → L4 Execution
개념:   감지 → 트리거 → 맥락 확보(이력·외기) → 후보 생성(+무행동) → 평가·선택
        → 안전 게이트 → 개입 방식 결정(조건 A/B) → 알림/실행(옵트인)
```

개념 8단계 중 후보 생성·평가·선택은 버스 L2 내부의 협력 객체
(`proactive`/`candidate_llm`/`evaluator`/`external_provider`/`feedback_store`)로
구현된다 — 버스 위상(불변식 1)은 그대로다.

위험도 4단계(심각도 오름차순): **ambient(잔잔 ●) < breath(숨결 ✓) < dusk(땅거미 ?) < ember(불씨 🛡)**.

> 기본 동작 = **제안만**. 실행은 옵트인(`LIOT_EXECUTION_ENABLED`, 기본 OFF)이며
> 구체 `Executor` 는 전용 모듈에만 존재한다 — `assert_executor_confined()` +
> `assert_execution_gated()` 가 런타임에 검증한다(불변식 8).

---

## 런타임이 강제하는 8대 불변식

1. **단방향 파이프라인** — EventBus 는 `(L1→L2, L2→L3, L3→L4, L4→L2)` 만 허용한다.
   그 외 publish 는 `TopologyViolation`.
2. **Gate2(LLM)는 Gate1을 무를 수 없다** — 최종 위험도 = `escalate_only(gate1, gate2)` = `max(...)`.
   **2′. LLM 은 안전 floor 를 낮출 수 없다** — LLM 이 만든 후보도 같은 결정론
   게이트를 지나며(출처 무관), 금지 후보는 후보 집합에서 폐기되고, 평가기의
   `safety_risk` 는 LLM 제출값을 무시하고 카탈로그 값으로 덮는다.
3. **실행 옵트인 게이트** — 기본은 제안-만(실행 0). 실행은 (a) 활성화
   (`LIOT_EXECUTION_ENABLED`) + (b) 화이트리스트 + (c) 티어 권한을 모두 만족할
   때만. 구체 Executor 는 전용 모듈에만(`assert_executor_confined`).
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
8. **Execution-Authorized-Only + 개입 하한** — 안전 임계는 어떤 티어에서도
   자동 실행 불가, '여는 방향'(unlock/valve open)은 어떤 실행 화이트리스트에도
   없다. 어떤 개입 정책(조건 A/B)도 티어 기준선보다 약한 확인을 못 낸다.
   `assert_execution_gated()` 가 정적으로 검증.

---

## 디렉터리

```
liot/
  core/         risk, contracts, invariants, event_bus, scoped_repo, egress_gate, clock
  db/           schema.sql, migrations, database
  layers/
    l1_sensing/  base, fake_source, ha_bridge, state_cache, normalizer
    l2_context/  context_builder, preference_store, proactive, proactive_rules,
                 candidate_llm(LLM 후보), evaluator(평가·선택),
                 external_provider(외기), feedback_store(선호 신호)
    l3_judgment/ rules_loader, guardrail(=SafetyGate), prompt_registry, llm_judge
    l4_execution/ device_catalog, risk_router, intervention(조건 A/B),
                 proposal_service, notifier, executor(옵트인)
  app.py        파이프라인 글루(LiotApp) + 능동 틱(tick)
  demo.py       실행 가능한 데모(후보 평가 트레이스 포함)
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

# 데모 실행 (light / thermostat / 가스누출 / 능동 틱 — 후보 평가 트레이스 출력)
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
- **LLM 의 역할 = 후보 제안·설명(옵션), 결정은 결정론**: LLM 후보 백엔드
  (`candidate_llm`, 기본 미장착)가 후보를 *제안*할 수 있으나 pydantic 구조
  검증 + `validate_proactive_candidate` 두 관문을 통과해야 후보 집합에 든다.
  위험도는 기존 Gate2 가 올릴 수만 있고, breath 위로 승격하면 그 제안은
  침묵한다(불변식 7·2′) — 인젝션된 LLM 의 최악 결과는 침묵이다.
- **알림 피로 게이팅(생성 시)**: (타깃, 액션)별 쿨다운 60분 + 시간대당
  1건 + 하루 3건(`limits:`, 데이터 주도). L4 notifier 의 표출 억제와
  별개·더 보수적 — 수용률(Proposal-Accept-Rate ≥ 60%)이 North Star.
- **재현성(불변식 4)**: 능동 결정 자체가 스냅샷의 `trigger` 가 되어
  `snapshot_hash` 가 판단된 후보를 고정한다. 출처는
  `params["__origin__"] = proactive:rule:<id>@<ruleset_hash 8자리>` 로
  영속된다(explicit:external / safety:auto 와 함께 3경로 전부 출처 보유).
- **신선도**: 능동 제안은 `expires_at`(규칙별 ttl)을 갖는다. `perishable`
  은 False — L4 에서 perishable 은 예산 _우회_ 의미라 컴포트에 쓰면
  역효과(잔소리)다.
- **egress 경화**: LLM payload 에는 방향성 스칼라 params 만 투영되고
  (`__origin__`/타깃 식별자 불출), LLM 목적지 한정으로 entity_id 형태
  문자열이 남으면 송신 자체를 거부한다(fail-closed).

검증: `tests/test_proactive.py` (로더 deny-by-default / 발의·침묵 결정론 /
LLM 폭주 → 침묵 + 안전 경로 무손상 / egress 불출 / occupancy fact 부활).

---

## 다중 후보 파이프라인 — 생성 → 평가 → 선택 → 개입

새 기획의 핵심: AI 가 더 많은 기기를 만지는 게 아니라, **여러 후보 중에서
상황에 맞는 행동을 고르고**(아무것도 안 하기 포함) **위험도에 맞게 개입
수준을 조절**한다.

- **후보 집합(`CandidateSet`)**: 매 틱, 매칭된 규칙 후보 전부 + (옵션) LLM
  후보 + **무행동(NoOp, 항상 포함 — pydantic validator 가 구조적으로 강제)**.
- **평가·선택(`CandidateEvaluator`)**: 효과·선호·에너지·방해도·안전위험을
  가중 합산해 최고점을 고른다. `safety_risk` 는 카탈로그 결정론 값으로
  **LLM 제출값을 덮어쓴다**. 무행동 기준선(0.35)을 못 넘으면 침묵.
- **외부 데이터(`ExternalDataProvider`)**: 외기 AQI·기온이 점수에 반영된다
  (바깥 나쁨 → 환기 감점·청정기 가점). 산출물은 스냅샷 트리거에 병합돼
  `snapshot_hash` 에 고정된다(불변식 4). 기본은 결정론 Fake — **실제
  기상청/공기질 API 호출은 예정**(env `LIOT_EXTERNAL_ENABLED` 스텁은
  degraded 를 돌려주고, degraded 면 평가기가 보수적으로 무행동을 고른다).
- **거절 학습(`FeedbackStore`)**: 서비스가 틱마다 거절 이력·수용률 스냅샷을
  주입 — 최근 거절된 류의 후보는 선호가 감점돼 무행동에 밀린다(조르지 않음).
- **평가 기록**: 매 틱의 후보·점수·선택(무행동 포함)이
  `candidate_evaluations` 테이블에 남는다(연구 로그·감사).
- **개입 조건 A/B(`intervention`)**: `LiotApp(condition="A"|"B")`.
  A=단일 승인(전부 explicit, 단 EMBER 는 double 유지), B=위험도 차등(기본).
  **후보 생성·평가·선택은 조건을 보지 못한다** — 동일 선택, 개입만 차등
  (실험 오염 방지). 어떤 조건도 티어 기준선보다 약한 확인을 못 낸다.
- **미구현(예정)**: 실 기상청/공기질 HTTP 연동 · 실모델 LLM 후보 생성 ·
  평가 가중치 학습 · 조건 A/B 실험 로그 집계.

검증: `tests/test_candidate_generation.py` · `test_evaluator.py` ·
`test_external_provider.py` · `test_external_snapshot_and_persist.py` ·
`test_intervention_ab.py` · `test_llm_floor.py`.

---

## 능동 파이프라인 확장 (2026-07 · 진행 중) — 트리거·2갈래·아비터·개인화·자연어 지시

능동(tick) 앞단을 더 명시적인 구조로 확장한다. 전체 흐름 다이어그램·핵심 요약은
**`docs/pipeline-overview.md`**(Mermaid, 타인 설명용).

```
① 트리거 감지(규칙 상시 + LLM 옵트인) → ② 전체 데이터 수집 + 선호 임베딩
  → ③ 2갈래 제안(Line A ML / Line B LLM, 각각 행동+가중치)
  → ④ 병합 후보풀(+무행동) → ⑤ 결정론 아비터(가중합·최고점)
  → ⑥ Gate1 → ⑦ Gate2 → ⑧ L4 개입 → 알림
알림 응답(수락/거부/무시) ─┐
       └── 선호 임베딩 갱신(유튜브식 EMA+감쇠) ──┘  → 다음 판단이 점점 개인화
자연어 지시("오늘 밤 불 켜놔줘", 채팅/음성) → 자율 후보 억제(안전 제외)
```

- **트리거 감지**(`event_detector`): 언제 판단을 시작할지 결정. 규칙 감지 상시 +
  LLM 감지 저빈도 옵트인(기본 off).
- **선호 임베딩**(`feature_vector`): 맥락×행동 셀맵 `{(시간대,재실,cap:action)→0~1}`
  을 `PreferenceStore`(scope `pref_embed`)에 영속. **알림 응답이 이 값을 계속
  갱신**(수락↑·거부↓·무시 약↓, 온라인 EMA + 시간감쇠) → 유튜브식 개인화. 두 제안
  라인이 이 임베딩을 소비한다.
- **2갈래 제안**(`proposal_lines`): **Line A(ML)** = 규칙 후보 + `EvalWeights`
  휴리스틱 재사용, **Line B(LLM)** = `device_catalog.available_actions()`(조작
  가능 행동 목록)를 받아 제안. **두 라인 모든 후보는 `validate_proactive_candidate`
  통과 필수** → NoOp 포함 병합 `CandidateSet`.
- **결정론 아비터**: 기존 `CandidateEvaluator` **무변경** 재사용. 두 라인 후보를
  가중합으로 겨뤄 최종 1개 선택 → 기존 Gate1/Gate2/L4 로 흐른다. 라인 자신감은
  텔레메트리(점수 미개입).
- **자연어 지시 오버라이드**(`nl_directive`): "오늘 밤 불 켜놔줘" 같은 지시를
  구조화 `UserDirective` 로 파싱해 자율 제안을 억제/제약. **comfort 후보만** 억제
  가능 — 안전 카테고리·EMBER 는 어떤 지시로도 못 낮춘다. 채팅(`/api/directive`)
  실동작 + 음성(Web Speech API STT/TTS) 옵션.

**안전 불변식(확장 2′)**: 모델·임베딩·지시는 전부 선호 층위. `safety_risk` 는
언제나 catalog 결정론이고, 가스/누수 등 안전 임계는 LLM 이 꺼져 있어도·사용자가
무시하라 해도 EMBER 를 유지한다(Safety-Never-Silent). `evaluator.py`·Gate1/Gate2/L4·
버스 엣지 무변경.

**구현 현황(2026-07-09)**: Phase 0(인터페이스 골격 + `line_confidence` +
`available_actions()`) ✅ 완료 · Phase 1~6(제안 라인·아비터·개인화 루프·트리거·
자연어 지시·시각화) ⏳ 진행. 실 ML/LLM/임베딩 학습은 인터페이스 뒤 결정론 Fake +
env 슬롯(예정). 상세 계약: `MODULE_SPEC.md` §능동 파이프라인 확장.

---

## 물리적 연결 레이어 (상시 데몬 · `liot-serve`)

`LiotApp`(두뇌, 네트워크 없음)을 둘러싸 실제 세계에 잇는 "몸통"이
`liot/service.py`(`LiotService`)다. 부수효과를 한곳에 모은다:

- **영속**: 모든 Proposal 을 SQLite 에 저장(재시작에도 상태 보존).
- **알림 실송신**: `Notifier.gate_pipeline` 발화 결정 → IMMEDIATE 면 정제
  payload 를 `NtfyTransport` 로 실제 ntfy POST. transport 는 `emit_external(NTFY)`
  를 통과한 _정제분만_ 받는다(미정제 송신 경로 원천 차단).
- **만료/틱**: 주기적 `expire_due()` + `app.tick()`(능동 제안).
- **피드백**: 서명 검증 HTTP 수신구(`/feedback?p=&t=&sig=&a=`) → 수락/거절
  → 상태 전이 → `accept_rate()`(North Star 측정, 출처별).

```bash
# 상시 데몬. 실 URL/토큰은 환경변수로 주입(코드에 시크릿 없음).
LIOT_HA_URL=ws://<host>:8123/api/websocket LIOT_HA_TOKEN=*** \
LIOT_NTFY_URL=https://ntfy.sh LIOT_NTFY_TOPIC=my-liot \
LIOT_NTFY_FEEDBACK_URL=http://<pi>:8099 \
LIOT_NOTIFY_SECRET=*** LIOT_LLM_BACKEND=ollama \
  liot-serve
```

- HA URL 미설정 시 센서 스트림 없이 **능동 틱만** 구동. ntfy URL 미설정 시
  발화 결정·정제까지만(sanitize-only, 실송신 없음).
- **실행 없음(불변식 3) 유지**: 데몬이 하는 외부 I/O 는 _알림 송신_ 뿐,
  액추에이터 호출이 아니다. HA 로는 읽기(stream)만.
- **fail-closed**: ntfy 송신 실패는 파이프라인을 죽이지 않고 '미전송'으로
  남긴다(제안은 pending → 다음에 재시도/다이제스트).

검증: `tests/test_service.py` (영속·발화·실 HTTP POST 왕복·fail-closed·
recently-rejected 억제·서명 검증/위조 거부·피드백 HTTP 왕복·수용률·데몬 루프).
