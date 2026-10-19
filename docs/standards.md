# 규칙

어기면 빌드·테스트가 깨지거나, 안전 불변식이 무너지거나, 구조가 어긋나는 것만 적는다.

## 검증 관문

- 커밋 전에 저장소 루트에서 `make check` 가 0 으로 끝나야 한다. 순서는 ruff → mypy(strict, `liot`·`scripts`·`tests` 전체) → pytest + 커버리지 하한 → 비밀 스캔이고, CI 와 같은 검사다.
- 커버리지 하한은 `pyproject.toml` 의 `fail_under` 한 곳에만 둔다. Makefile·훅·CI 에 `--cov-fail-under` 를 따로 적지 않는다.
- git 워크트리에서 검사할 때는 `PYTHONPATH=$PWD make check VENV=/Users/rayul/liot/.venv` 로 돌린다. `PYTHONPATH` 없이 통과한 결과는 메인 체크아웃 코드에 대한 결과라 증거가 아니다.
- 가드(상수·조건·우회 분기)를 새로 넣거나 바꾸면 뮤테이션 확인을 한다: 그 가드를 지우거나 뒤집었을 때 **정확히 그 가드의 테스트만** 실패해야 한다. 아무 테스트도 안 깨지면 가드에 테스트가 없는 것이고, 여럿이 깨지면 격리가 안 된 것이다.
- 기대값은 리터럴로 고정한다. 코드의 상수를 그대로 가져와 비교하는 테스트(동어반복)는 금지다 — 상수를 바꿔도 초록으로 남는다.
- 2단계 판정을 거치는 테스트는 `await service.drain_phase2()` 로 끝을 기다린다. `asyncio.sleep` 으로 기다리지 않는다. DB 를 닫기 전에 반드시 drain 한다.
- 테스트는 외부 네트워크를 부르지 않는다. 실제 모델·HA·ntfy 없이 가짜 백엔드·가짜 연결·가짜 transport 로 돈다. 루프백 가짜 서버는 허용한다.
- `bench/` 는 `make check` 밖이다. 벤치 코드를 바꿨으면 `run_bench.py check` 를 따로 돌린다.

## 안전 불변식을 지키는 구조 규칙

내용 자체(무엇이 위험인지)는 도메인 규칙의 몫이다. 여기서는 코드가 지켜야 하는 형태와 그 검증만 적는다.

- 위험도 합성은 `escalate_only`, 파급도 합성은 `escalate_severity` 만 쓴다. `min`·대입·직접 비교로 최종값을 정하는 코드를 새로 쓰지 않는다. 기존 불변식 테스트(특히 알림 대상 Safety-Never-Silent)는 수정 없이 통과해야 한다.
- 알림 게이트에 새 조건을 넣을 때는 우회 조건 `시의성 OR 불씨 OR 위험` 을 줄이지 않는다. 파급도 없는 제안의 판정 결과는 바뀌지 않아야 하고, 그 회귀 테스트가 있어야 한다.
- 상황 알림의 1단계는 모델·네트워크를 기다리지 않는다. 2단계는 추적되는 백그라운드 태스크로만 띄운다(`await` 로 기다리면 다음 안전 신호가 그 뒤에 줄 선다). 2단계 결과 판단 행은 성공·실패와 무관하게 저장한다.
- 탭 한 번 예외는 `ONE_TAP_SAFE_ACTIONS` frozenset 리터럴 하나로만 표현한다. `SAFETY_EXECUTABLE` 을 재사용하지 않는다. `ONE_TAP_SAFE_ACTIONS ⊆ SAFETY_EXECUTABLE` 은 테스트와 `assert_execution_gated` 가 함께 검사한다. 등록·확장으로 늘리는 경로를 만들지 않는다.
- 구체 Executor 클래스(이름에 `Executor`, 추상·Protocol 제외)는 `liot/layers/l4_execution/executor.py` 에만 둔다. `assert_executor_confined` 가 `liot` 전체를 스캔해 다른 곳에서 찾으면 실패한다.
- `FORBIDDEN_OPEN`(`lock.unlock`, `valve.open`)은 어떤 실행 화이트리스트에도 들어가지 않는다. 확장분을 주입하는 코드는 `effective_comfort` 를 거쳐 그 집합을 빼야 한다.
- 거주자 특성은 계약 모델(`Judgment`·`Proposal`·알림 페이로드)에 필드로 추가하지 않는다. 판단 경로로는 '취약 거주자가 있는가' 불리언만 넘긴다.
- 보호자 전달·119 안내를 결정하는 코드는 상황 테이블의 `severity_floor` 만 본다. 최종 파급도(모델 상향 포함)를 보호자 조건에 쓰지 않는다.
- 전화를 걸거나 신고 API 를 부르는 코드를 넣지 않는다.

## 외부 송신 규칙

- 외부로 나가는 모든 페이로드는 `emit_external` 을 지난다. transport·모델 클라이언트는 정제된 dict 만 받는다. 정제 전 객체를 transport 에 넘기는 코드는 금지다.
- NTFY 허용 키 집합과 알림 페이로드 필드 집합은 정확한 집합으로 테스트에 고정돼 있다(`test_ntfy_allowed_keys_gained_only_severity_label_and_summary_keys`, `test_notification_payload_has_no_device_room_rationale`, `test_guardian_payload_exact_field_set`). 키를 더할 때는 그 테스트를 의도적으로 고친다. 기기·방·근거·특성을 뜻하는 키는 더하지 않는다.
- 모델로 보내는 후보 파라미터는 숫자 키(delta·step·temperature·brightness)의 숫자와 닫힌 방향 어휘만 투영한다. 임의 문자열 파라미터를 투영하지 않는다.
- 비밀값(서명 키·HA 토큰·ntfy 토큰·보호자 토픽)은 저장소·로그·예외 메시지에 넣지 않는다. 환경변수 예시에는 `***`·`<...>` 를 쓴다.

## 모듈 경계

| 모듈 | 임포트해도 되는 것 | 임포트하면 안 되는 것 |
|---|---|---|
| `liot/core` | 표준 라이브러리, pydantic, yaml | 레이어·db·service (불변식 검사기의 지연 임포트만 예외) |
| `liot/db` | core | 레이어·app·service |
| `liot/layers/*` | core. L2 는 L4 의 기기 카탈로그만 | db, app, service, 다른 레이어의 로직 |
| `liot/app.py` | core, 레이어 | db, service, 네트워크 |
| `liot/service.py` | app, core, db, 레이어 | demo_server |

- 레이어 사이 데이터는 `liot/core/contracts.py` 의 pydantic 모델로만 흐른다. 모든 계약은 `extra="forbid"` 를 유지한다.
- 버스 엣지는 `ALLOWED_EDGES` 넷(L1→L2, L2→L3, L3→L4, L4→L2)뿐이다. 새 엣지를 추가하지 않는다.
- 비동기 DB 조회는 서비스가 하고, L2 평가기에는 동기 스냅샷만 넘긴다.

## 설정·데이터

- `config/rules/` 에는 Gate1 규칙 파일만 둔다. 그 폴더의 모든 `*.yaml`·`*.yml` 이 규칙셋으로 합쳐진다. `rules` 항목에 `set_floor` 가 없으면 로드가 실패해 앱 생성이 깨지고, `rules` 키가 없는 파일은 빈 규칙셋으로 조용히 합쳐져 규칙셋 버전(판단의 재현 키)만 바뀐다.
- 파급도 표·상황 매핑·이상 규칙은 `config/severity/situations.yaml` 에만 둔다. 같은 값을 파이썬 상수로 복제하지 않는다. 표 12칸과 매핑 행은 테스트가 칸마다 고정한다.
- Gate1 규칙의 `match` 키는 열거 키(action_category·time_bucket·occupancy·source·trust·capability·action)만 쓴다. 강등 키(demote·to_tier·set_ceiling 등)는 로더가 거부한다.
- 스키마 변경은 `liot/db/migrations.py` 의 `_MIGRATIONS` 에 새 버전을 덧붙이는 것으로만 한다. `schema.sql`(버전 1)은 고치지 않는다. 컬럼 삭제·이름 변경은 금지다. 각 단계는 멱등이어야 한다(`IF NOT EXISTS`, 없을 때만 컬럼 추가).
- 테넌트 데이터를 다루는 새 테이블은 `home_id TEXT NOT NULL DEFAULT 'singleton'` 과 빈 문자열 금지 CHECK 를 둔다.

## 환경 설정

- 새 환경변수의 불리언 해석은 `liot/core/env.py` 의 `env_flag` 를 쓴다(참 = 1·true·yes·on, 그 밖은 거짓).
- 옵트인 기능의 기본값은 꺼짐이다(실행, LLM 트리거 감지, 개발 키 수신구).
- DB 기본 경로는 `liot/serve.py` 의 `DEFAULT_DB_PATH` 한 곳에만 둔다. 관제실 서버는 같은 `resolve_db_path` 를 쓴다.
- 결정 모델 제한시간처럼 숫자 설정은 양의 유한수가 아니면 기동 시 `ValueError` 로 거부한다.

## 커밋·브랜치

- 의미 단위로 커밋하고, 메시지는 한국어 요약으로 쓴다(`feat(scope): …`, `fix(scope): …`, `docs: …`, `test(scope): …`).
- push·배포는 소유자의 명시 승인 없이 하지 않는다. CI 는 main/master push 와 PR 에서 돈다.
- 되돌릴 수 없는 삭제를 하지 않는다. ollama 백엔드 코드는 기본값에서 빠졌어도 지우지 않는다.
- 결정 모델의 기본값(`LIOT_DECISION_MODEL` 기본값·문서상 기본 모델)은 벤치 라벨이 소유자 검토로 확정되기 전에는 바꾸지 않는다.
