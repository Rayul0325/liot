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

## 런타임이 강제하는 6대 불변식

1. **단방향 파이프라인** — EventBus 는 `(L1→L2, L2→L3, L3→L4, L4→L2)` 만 허용한다.
   그 외 publish 는 `TopologyViolation`.
2. **Gate2(LLM)는 Gate1을 무를 수 없다** — 최종 위험도 = `escalate_only(gate1, gate2)` = `max(...)`.
3. **제안-만 모델** — 액추에이터 실행 코드/구체 Executor 0개. `assert_executor_absent()` 가 검증.
4. **재현 가능한 판단** — 모든 `Judgment` 에 `snapshot_hash` + `bundle_id` + `rationale` 동시 저장.
5. **승격만, 강등 불가** — `escalate_only(cur, target) = max` + 런타임 단조 가드(`assert_monotonic`).
6. **Safety-Never-Silent** — 안전 임계 카테고리
   (`gas/smoke/door/leak/high_temp/lock/water_main/oven`)는 LLM 가용성과 무관하게
   Gate1 결정론만으로 즉시 ember 제안을 만든다.

---

## 디렉터리

```
liot/
  core/         risk, contracts, invariants, event_bus, scoped_repo, egress_gate, clock
  db/           schema.sql, migrations, database
  layers/
    l1_sensing/  base, fake_source, ha_bridge, state_cache, normalizer
    l2_context/  context_builder, preference_store
    l3_judgment/ rules_loader, guardrail, prompt_registry, llm_judge
    l4_execution/ device_catalog, risk_router, proposal_service, notifier
  app.py        파이프라인 글루(LiotApp)
  demo.py       실행 가능한 데모
config/
  device_catalog.yaml
  rules/        emergency.yaml, night_quiet.yaml
tests/          불변식 / Gate1 / risk_router / egress / e2e
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
- Home Assistant 브리지(`ha_bridge`)는 스켈레톤 스텁(연결 안 함).
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
