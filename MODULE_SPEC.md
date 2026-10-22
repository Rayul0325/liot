# LIOT Core Kernel — MODULE_SPEC

다른 레이어/에이전트가 맞물리기 위해 **정확히 이 시그니처에 의존**한다.
이 문서가 코어의 단일 진실원(SoT)이다. 추측 금지: 시그니처가 다르면 이 문서가 맞다.

- Python 3.11+ (3.13 실행). 의존성: `pydantic` v2, `pyyaml`, `aiosqlite`, `pytest`, `pytest-asyncio`만.
- 단일 프로세스 asyncio. 코어는 네트워크 호출을 하지 않는다.
- 모든 코어 모듈은 `from __future__ import annotations` 사용.

---

## import 경로 요약

```python
from liot.core.risk import RiskTier, escalate_only, SAFETY_CRITICAL_CATEGORIES
from liot.core.contracts import (
    Layer, SensorReading, CandidateAction, ContextSnapshot, Judgment,
    Proposal, FeedbackEvent, NotificationPayload, EventMessage, compute_snapshot_hash,
    # 다중 후보 파이프라인
    NOOP_CAPABILITY, make_noop_candidate, is_noop,
    ActionCandidate, CandidateSet, ExternalContext, EvaluationResult,
)
from liot.core.invariants import (
    InvariantError, TopologyViolation, IsolationError, EgressViolation, MonotonicityError,
    assert_monotonic, assert_executor_confined, assert_execution_gated, ExecutorProtocol,
)
from liot.core.event_bus import InProcessBus, ALLOWED_EDGES, Handler
from liot.core.scoped_repo import ScopedRepo, assert_no_null_tenants, DEFAULT_HOME_ID
from liot.core.egress_gate import (
    Destination, ALLOWLIST, NTFY_ALLOWED_KEYS, PII_PATTERNS,
    AliasMap, pseudonymize_recursive, emit_external,
)
from liot.core.clock import Clock, TimeBucket
from liot.db.database import connect, init_db
from liot.db.migrations import run_migrations, current_version, LATEST_VERSION, SCHEMA_SQL_PATH
```

---

## `liot.core.risk`

```python
class RiskTier(IntEnum):
    AMBIENT = 0; BREATH = 1; DUSK = 2; EMBER = 3   # 심각도 오름차순. 정수 비교 가능.

    @classmethod
    def from_str(cls, value: str | RiskTier | int) -> RiskTier
        # 대소문자/공백 무시. 정수/RiskTier 도 수용. 미지값 -> ValueError.

    @property
    def label_ko(self) -> str        # AMBIENT='잔잔' BREATH='숨결' DUSK='땅거미' EMBER='불씨'
    @property
    def glyph(self) -> str           # AMBIENT='●' BREATH='✓' DUSK='?' EMBER='🛡'
    @property
    def required_confirmation(self) -> str  # 'none' / 'light' / 'explicit' / 'double'

def escalate_only(cur: RiskTier|str|int, target: RiskTier|str|int) -> RiskTier
    # == max(cur, target). 강등 불가. 입력은 from_str 로 정규화.

SAFETY_CRITICAL_CATEGORIES: frozenset[str]
    # = {'gas','smoke','door','leak','high_temp','lock','water_main','oven'}  (8개)
```

매핑: 위험도 ↔ confirmation: AMBIENT→none, BREATH→light, DUSK→explicit, EMBER→double.

---

## `liot.core.contracts` (pydantic v2, 전부 `ConfigDict(extra="forbid")`)

```python
class Layer(str, Enum): L1="L1"; L2="L2"; L3="L3"; L4="L4"; USER="USER"

class SensorReading(BaseModel):
    entity_id: str; source: str; raw_value: Any; norm_value: Any
    unit: str|None = None; confidence: float = 1.0; ts_utc: str; home_id: str = "singleton"

class CandidateAction(BaseModel):
    capability: str; action: str; params: dict[str,Any] = {}; entity_id: str; category: str

class ContextSnapshot(BaseModel):
    snapshot_id: str; trace_id: str; home_id: str = "singleton"
    trigger: dict[str,Any]; structured: dict[str,Any]
    nl_summary: str; summary_template_sha: str; created_at: str
    snapshot_hash: str = ""   # 빈 값이면 model_validator(after)가 structured 에서 sha256 계산

class Judgment(BaseModel):
    judgment_id: str; trace_id: str; home_id: str; snapshot_hash: str
    action: CandidateAction; risk_tier: RiskTier; confidence: float; rationale: str
    gate1_verdict: str; gate2_verdict: str|None = None
    bundle_id: str; model_id: str; degraded: bool = False

class Proposal(BaseModel):
    proposal_id: str; judgment_id: str; home_id: str; risk_tier: RiskTier
    action: CandidateAction; rationale: str; required_confirmation: str
    perishable: bool = False; expires_at: str|None = None
    state: str = "pending"; created_at: str

class FeedbackEvent(BaseModel):
    proposal_id: str; trace_id: str; user_action: str
    stop_scope: str|None = None; comment: str|None = None; latency_ms: int|None = None

class NotificationPayload(BaseModel):   # device/room/rationale 필드 없음 (의도적)
    channel: str; proposal_id: str; risk_tier: RiskTier; glyph: str
    label: str; signed_deeplink: str; topic: str
    severity_label: str | None = None   # 상황 알림의 파급도 라벨(ntfy 세기 결정)

class GuardianNotificationPayload(BaseModel):   # 기기·방·근거·거주자 특성 필드 없음
    channel: str; situation_label: str; home_alias: str; at: str
    signed_ack_link: str; severity_label: str

class EventMessage(BaseModel):
    trace_id: str; home_id: str; layer_from: Layer; layer_to: Layer
    type: str; ref_id: str; partition_key: str; ts: str

def compute_snapshot_hash(structured: dict[str,Any]) -> str
    # sha256(hex). canonical JSON: sort_keys=True, 컴팩트 구분자, default=str. 키 순서 무관.

# ── 다중 후보 파이프라인(생성→평가→선택) ──────────────────────────────
NOOP_CAPABILITY = "noop"                       # 무행동 예약 capability
def make_noop_candidate(entity_id="__none__") -> CandidateAction
def is_noop(candidate: CandidateAction) -> bool

class ActionCandidate(BaseModel):              # 점수 붙은 후보
    candidate: CandidateAction
    expected_effect/user_preference_fit/energy_cost/disturbance/safety_risk/final_score: float = 0.0
    reason: str = ""            # 설명 전용(점수 가중 0, egress 불출)
    origin: str = ""            # "rule:<id>" | "line_a:ml" | "line_b:llm" | "llm" | "noop"
    line_confidence: float = 0.0  # 제안 라인 자신감. v1 텔레메트리/타이브레이크 전용
                                  # (final_score 미개입 → 결정론 아비터=evaluator 무변경)

class CandidateSet(BaseModel):                 # 무행동 후보 필수(validator 강제)
    trace_id: str; home_id: str = "singleton"; snapshot_hash: str
    candidates: list[ActionCandidate]; selected_index: int = 0
    # selected(속성) / selected_action() / selected_is_noop()

class ExternalContext(BaseModel):              # 외부 데이터(외기) 조회 결과
    outdoor_temp_c: float|None; outdoor_aqi: int|None   # aqi 는 실험실 입력 호환용
    pm25: float|None (≥0, ㎍/㎥ — 공기질 단일 척도, 없고 aqi 만 있으면 EPA 구간표로 유도)
    cloud_cover: float|None (0~1)
    source: str = "fake"; fetched_at: str; degraded: bool = False
    def to_structured(self) -> dict            # fetched_at 제외(재현성) — 틱 트리거에 병합
    def rule_facts(self) -> dict               # 능동 규칙 external: 조건용. degraded 면 {"degraded": True}

class EvaluationResult(BaseModel):             # 평가·선택 산출물(감사/영속)
    scored: list[ActionCandidate]; selected_index: int; rationale: str
    # selected(속성) / selected_action() / selected_is_noop()
```

`ContextSnapshot.snapshot_hash` 는 `compute_snapshot_hash(structured)` 와 동일하게 채워진다.
직접 해시를 만들 때도 이 함수를 써라(직렬화 규칙 일치 보장).

---

## `liot.core.invariants`

```python
class InvariantError(Exception)            # 베이스
class TopologyViolation(InvariantError)    # event_bus
class IsolationError(InvariantError)       # scoped_repo
class EgressViolation(InvariantError)      # egress_gate
class MonotonicityError(InvariantError)    # risk 단조

def assert_monotonic(old, new) -> RiskTier   # new<old -> MonotonicityError, 아니면 new 반환
def assert_executor_confined(package_name="liot.layers.l4_execution",
                             *, sanctioned_module="liot.layers.l4_execution.executor") -> None
    # 구체 Executor(이름에 'Executor' 포함, 추상/Protocol 제외)는 전용 모듈에만.
    # 다른 모듈에서 발견되면 InvariantError. 패키지 부재면 통과.
def assert_execution_gated() -> None
    # 불변식 8 정적 검증: (1) 기본 ExecutionPolicy 비활성(옵트인 OFF)
    # (2) 안전 임계는 어떤 티어에서도 AUTO 불가 (3) '여는 방향'(unlock/valve
    # open)은 어떤 실행 화이트리스트에도 없음 (4) 개입 정책(조건 A/B)은 어떤
    # 티어에서도 기준선(required_confirmation)보다 약한 확인 불가(개입 하한).

@runtime_checkable
class ExecutorProtocol(Protocol):
    async def execute(self, *args, **kwargs) -> object: ...
    # 추상 인터페이스를 정의하고 싶을 때 베이스로 사용 (confined 검사가 허용).
```

L4 작성자 주의: **구체 Executor 클래스는 `l4_execution/executor.py` 밖 금지**.
다른 모듈에 두면 `assert_executor_confined()` 가 실패한다. 인터페이스가 필요하면
`ExecutorProtocol` 또는 추상 메서드가 남은 ABC 로만 선언하라.

---

## `liot.core.event_bus`

```python
ALLOWED_EDGES: frozenset[tuple[Layer,Layer]]
    # = {(L1,L2),(L2,L3),(L3,L4),(L4,L2)}   그 외 전부 금지.

Handler = Callable[[EventMessage], Awaitable[None]]   # async 핸들러

class InProcessBus:
    def subscribe(self, layer_to: Layer, handler: Handler) -> None
    async def publish(self, msg: EventMessage) -> None
        # edge=(msg.layer_from,msg.layer_to) 가 ALLOWED_EDGES 밖이면 TopologyViolation.
        # 허용 시 layer_to 구독자들을 등록 순서대로 순차 await.
    async def publish_many(self, messages: list[EventMessage]) -> None
    def subscriber_count(self, layer_to: Layer) -> int
```

---

## `liot.core.scoped_repo`

```python
DEFAULT_HOME_ID = "singleton"

class ScopedRepo:
    def __init__(self, home_id: str|None)   # None/공백 -> IsolationError
    @property
    def home_id(self) -> str
    def scope_where(self, extra: str|None=None) -> str        # "WHERE home_id = ?" [+ " AND (extra)"]
    def scope_params(self, *extra) -> tuple                   # (home_id, *extra)  — 첫 값 항상 home_id
    async def fetchall(self, db, table, *, columns="*", extra_where=None,
                       extra_params=(), order_by=None, limit=None) -> list
        # table 은 테넌트 테이블 화이트리스트여야 함(아니면 IsolationError). home_id 정확일치 자동.
    async def insert(self, db, table: str, values: dict) -> None
        # values['home_id'] 는 이 스코프로 강제 덮어씀.

async def assert_no_null_tenants(db) -> None   # 테넌트 테이블에 NULL/공백 home_id 행 있으면 IsolationError
```

테넌트 테이블: `preferences, sensor_readings, context_snapshots, judgments, proposals,
feedback, notifications, audit_log, trust_state, candidate_evaluations`.

---

## `liot.core.egress_gate` — 모든 외부 송신의 단일 통로

```python
class Destination(str, Enum): LLM="LLM"; NTFY="NTFY"; KMA="KMA"
ALLOWLIST: frozenset[Destination]    # = {LLM, NTFY, KMA}
NTFY_ALLOWED_KEYS: frozenset[str]
    # = {'channel','proposal_id','risk_tier','glyph','label','signed_deeplink','topic',
    #    'severity_label','situation_label','home_alias','at'}   # 11개 — 정확한 집합을 테스트가 고정
PII_PATTERNS: dict[str, re.Pattern]  # phone_kr, email, rrn_kr, geo_precise

class AliasMap:
    def alias_for(self, real: str, kind: str="PERSON") -> str   # kind=="ROOM" -> ROOM_A.. / else PERSON_1..
    def real_for(self, alias: str) -> str|None
    def register(self, real: str, alias: str) -> None

def pseudonymize_recursive(payload, alias: AliasMap)
    # dict 키가 room/area/zone/space -> ROOM, person/user/name/device/occupant/owner -> PERSON 가명화.

def emit_external(dest: Destination|str, payload: dict, trace_id: str, alias: AliasMap|None=None) -> dict
    # 1) dest not in ALLOWLIST -> EgressViolation
    # 2) LLM=재귀 가명화 / NTFY=화이트리스트 키만 / KMA=좌표(lat/lon/lng/...) 0.1도 격자 일반화
    # 3) 정제 후 PII_PATTERNS 매치 잔존 -> EgressViolation
    # 4) 정제된 payload 반환 (네트워크 송신은 하지 않음)
```

ntfy 로 나가는 페이로드에는 **device/room/rationale 가 절대 포함되지 않는다**
(화이트리스트 키만 통과). 외부로 좌표를 보낼 땐 KMA 로만, 그것도 격자 일반화된다.

---

## `liot.core.clock`

```python
TimeBucket = str   # 'morning' | 'day' | 'evening' | 'night'

class Clock:
    def __init__(self, fixed: datetime|str|None=None, trust_ok: bool=True)
        # fixed: 고정 시각(테스트). tz-naive 면 UTC 로 간주. ISO8601 'Z' 허용.
    trust_ok: bool                       # 시각 신뢰 여부(공개 속성)
    def now_utc(self) -> str             # 'YYYY-MM-DDTHH:MM:SS+00:00' (마이크로초 절삭)
    def now_dt(self) -> datetime         # tz-aware UTC
    def time_bucket(self, when: datetime|str|None=None) -> TimeBucket
        # UTC 시(hour) 기준: 06-11 morning / 11-17 day / 17-22 evening / 그 외 night
    def set_fixed(self, fixed: datetime|str|None) -> None
```

---

## `liot.db`

### `schema.sql` (버전 1 기준 스키마)

테이블: `schema_version, preferences, sensor_readings, context_snapshots, judgments,
proposals, feedback, notifications, audit_log, llm_cache, trust_state`.

- 모든 테넌트 테이블: `home_id TEXT NOT NULL DEFAULT 'singleton'`.
- `judgments`: `action_json`, `risk_tier`(INTEGER 0..3), `snapshot_hash`, `gate1_verdict`,
  `gate2_verdict`(NULL 가능), `bundle_id`, `model_id`, `degraded`(0/1) — 재현 키 보존.
- `proposals`: `action_json`, `required_confirmation`, `perishable`, `expires_at`, `state`.
- `feedback`: `user_action`, `stop_scope`, `comment`, `latency_ms`.
- `audit_log`: append-only (`actor`, `event_type`, `ref_id`, `detail`).
- `llm_cache`: PK `(home_id, prompt_hash, model_id)`.

> 주의: 컨트랙트의 `Judgment.action` / `Proposal.action` 은 `CandidateAction` 객체지만
> DB 컬럼은 `action_json`(직렬화 문자열)이다. 저장 시 `model.action.model_dump_json()`,
> 로드 시 `CandidateAction.model_validate_json(...)` 를 쓴다.

### `migrations.py`

```python
SCHEMA_SQL_PATH: Path                 # schema.sql 절대경로
LATEST_VERSION: int                   # = 3
async def current_version(db) -> int                 # 없으면 0
async def run_migrations(db) -> int                  # forward-only, 멱등. 적용 후 최신 버전 반환.
```

버전 2: `candidate_evaluations` — 틱별 후보 평가 로그(연구/감사).
컬럼: `home_id, trace_id, snapshot_hash, scored_json(list[ActionCandidate] 직렬화),
selected_index, rationale, created_at`. PK `(home_id, trace_id)`.
writer 는 `LiotService.run_tick`(무행동 선택도 기록).

### `database.py`

```python
async def connect(path: str, durable: bool=False) -> aiosqlite.Connection
    # row_factory=aiosqlite.Row, journal_mode=WAL, foreign_keys=ON,
    # synchronous = FULL(durable=True) / NORMAL(durable=False). 호출자가 close.
async def init_db(path: str, durable: bool=False) -> aiosqlite.Connection
    # connect 후 run_migrations 까지 적용한 연결 반환.
```

---

## 불변식 ↔ 코어 매핑 (테스트가 검증할 지점)

1. 단방향 파이프라인 → `event_bus.ALLOWED_EDGES` + `TopologyViolation`.
2. Gate2 는 Gate1 을 무를 수 없다 → 최종 `risk_tier = escalate_only(gate1, gate2)` (`max`).
   2′. LLM 은 안전 floor 를 낮출 수 없다 → LLM 후보도 `validate_proactive_candidate`
   통과 필수 + SafetyGate 는 출처 무관 + 평가기 `safety_risk` 는 카탈로그가 덮어씀
   (`tests/test_llm_floor.py`).
3. 실행 옵트인 게이트 → `assert_executor_confined()`(구체 Executor 는 전용 모듈만)
   - `assert_execution_gated()`(기본 OFF·안전 AUTO 불가·여는 방향 불가·개입 하한).
4. 재현 가능한 판단 → `Judgment.snapshot_hash` + `bundle_id` + `rationale` 동시 보존
   (DB `judgments` 동일 컬럼). `compute_snapshot_hash` 가 결정론적.
   외부 데이터는 `ExternalContext.to_structured()` 로 틱 트리거에 병합돼 해시에 고정.
5. 승격만, 강등 불가 → `escalate_only` + `assert_monotonic`(`MonotonicityError`).
6. Safety-Never-Silent → `SAFETY_CRITICAL_CATEGORIES` 의 트리거는 LLM 없이
   Gate1 결정론만으로 즉시 EMBER 제안 생성 (`gate2_verdict=None` 허용, `degraded` 무관).
7. 능동 = 컴포트 전용 + BREATH 상한 → `PROACTIVE_ALLOWED` + `validate_proactive_candidate`
   - app 상한(초과 = 침묵). 무행동(NoOp) 후보는 `CandidateSet` validator 가 강제.
8. Execution-Authorized-Only + 개입 하한 → `ExecutionPolicy`(옵트인·티어 권한)
   - 실행기 화이트리스트 재검 + `InterventionPolicy`(조건 A/B, 기준선 이하 금지).

---

## 능동 파이프라인 확장 (L2) — 신규 모듈 시그니처

능동(tick) 앞단을 **트리거 감지 → 데이터+선호 임베딩 → 2갈래 제안(ML/LLM) →
결정론 아비터**로 재구성하고, **알림 응답이 선호 임베딩을 갱신하는 개인화 루프**와
**자연어 지시 오버라이드**를 붙인다. 전체 흐름은 `docs/pipeline-overview.md`(Mermaid).

> **관통 원칙**: 모델(ML 라인·LLM 라인·LLM 트리거·임베딩·지시)은 전부 **선호/제안**
> 층위다. `safety_risk` 는 항상 catalog 결정론(`evaluator._safety_risk`)이고, 안전
> 카테고리/EMBER 는 임베딩·지시로 낮출 수 없다(불변식 2′ 확장). 아비터는 기존
> `CandidateEvaluator` **무변경** 재사용 — 라인 자신감은 점수 미개입(텔레메트리).
> 모든 실모델(ML/LLM/임베딩 학습)은 인터페이스 뒤 결정론 Fake + env 슬롯(`예정`).

```python
# liot.layers.l2_context.event_detector  — ① 트리거 감지
@dataclass(frozen=True)
class TriggerDecision:  fire: bool; reason: str = ""; source: str = "rule"  # "rule"|"llm"

@runtime_checkable
class EventDetector(Protocol):
    source: str
    def detect(self, cur: Mapping, prev: Mapping|None = None) -> TriggerDecision: ...

class RuleEventDetector:        source="rule"   # Phase4: should_snapshot/propose 게이팅 재사용
class FakeLLMEventDetector:     source="llm"    # 저빈도 옵트인(env, 기본 off), 결정론
def build_event_detector() -> EventDetector    # 기본 규칙 감지. 실 LLM 감지=예정

# liot.layers.l2_context.feature_vector  — ② 선호 임베딩(개인화)
NEUTRAL_PREF = 0.5;  PREF_EMBED_SCOPE = "pref_embed"
def cell_key(time_bucket: str, occupancy: str, candidate: CandidateAction) -> str
    # = "{time_bucket}|{occupancy}|{capability}:{action}"

@runtime_checkable
class PreferenceEmbedder(Protocol):
    def preference_for(self, candidate: CandidateAction, facts: Mapping) -> float: ...  # 0~1

class FakePreferenceEmbedder:   # 영속 셀맵(PreferenceStore scope="pref_embed") 조회. 없으면 중립.
    def __init__(self, cells: Mapping[str,float]|None = None)
    # Phase3: 알림 응답으로 EMA 갱신 cell += 0.3(target−cell) + 시간감쇠 cell += 0.05(0.5−cell)/일
    #         (수락 target=1.0 / 거부 0.0 / 무시·만료 0.30)

# liot.layers.l2_context.proposal_lines  — ③ 2갈래 제안
@runtime_checkable
class ProposalLine(Protocol):
    name: str
    def propose(self, facts: Mapping, cur: Mapping, *, embedder: PreferenceEmbedder|None=None,
                available_actions: Sequence[Mapping]|None=None) -> list[ActionCandidate]: ...

class MLProposalLine:   name="line_a:ml"    # 규칙 후보 + EvalWeights 휴리스틱 재사용
class LLMProposalLine:  name="line_b:llm"   # available_actions 소비 결정론 Fake. 실모델=예정
# 두 라인 모든 후보는 validate_proactive_candidate 통과 필수 → 병합기(+NoOp) → CandidateSet
# (snapshot_hash 는 facts+cur+embedding 전부 덮어야 재현성/불변식4 유지)

# liot.layers.l2_context.nl_directive  — 자연어 지시 오버라이드(대화형)
@dataclass(frozen=True)
class UserDirective:  capability; entity_id=""; constraint="prefer"  # keep_on|keep_off|forbid|prefer
                      until=""; source="chat"  # chat|voice; raw_text=""

@runtime_checkable
class DirectiveParser(Protocol):
    def parse(self, text: str, *, source: str = "chat") -> list[UserDirective]: ...

class FakeDirectiveParser:   # 결정론 파싱. 실 LLM 파싱=예정

class DirectiveStore:        # 세션 스코프 in-memory 활성 지시
    def __init__(self, parser: DirectiveParser | None = None, *, clock: Clock | None = None)
    def add_text(self, text: str, *, source: str = "chat", until: str = "") -> list[UserDirective]
        # until: ISO8601 시각(tz 없으면 UTC). 형식 오류면 저장 없이 ValueError. 기간 표기 미지원.
    def active(self, now: datetime | str | None = None) -> list[UserDirective]
        # until 시각부터 만료(경계 포함) → 제외. now 미지정 시 주입 Clock. 조회는 목록을 지우지 않음.
    def all(self) -> list[UserDirective]   # 만료분까지 담은 사본. 기준 시각을 모르는 호출자용
    def clear(self) -> None
# 활성 directive 는 comfort 후보만 억제(안전 카테고리·EMBER 는 억제 불가). Phase5.

# liot.layers.l4_execution.device_catalog  — Line B 입력
class DeviceCatalog:
    def available_actions(self) -> list[dict]   # {entity_id, capability, action, category, tier}
        # _devices × _capabilities 교차, tier 는 risk_for 권위. 후보 '생성 상한'(권위 아닌 필터).
```

### 구현 현황 (2026-08-29 실측)

각 행의 '근거'는 배선 지점이다. 표를 고칠 때는 근거부터 확인한다.

| Phase | 내용                                                                            | 상태      | 근거                                                                       |
| ----- | ------------------------------------------------------------------------------- | --------- | -------------------------------------------------------------------------- |
| 0     | 인터페이스 골격(Protocol+Fake 스텁) + `line_confidence` + `available_actions()` | ✅ 완료   | —                                                                          |
| 1     | 선호 임베딩 조회 + 2갈래 제안 라인 실동작 → 병합 `CandidateSet`                 | ✅ 완료   | `app.py` `augment_candidate_set(...)` · `tests/test_proposal_lines.py`     |
| 2     | 아비터(evaluator 재사용) + `app.tick()` 재작성 + 데모 트레이스                  | ✅ 완료   | `app.py` `evaluator.evaluate(...)` → `last_candidate_evaluation`           |
| 3     | 개인화 루프(피드백→임베딩 EMA+감쇠, `_refresh_feedback` 훅)                     | ✅ 완료   | `service.py` `_refresh_feedback()` · `feature_vector.apply_signal`         |
| 4     | 트리거 감지 배선(규칙 상시 + LLM 옵트인)                                        | ✅ 완료   | `app.py` `_event_detector.detect(cur)` · `tests/test_event_detector.py`    |
| 5     | 자연어 지시 오버라이드(`/api/directive` + `until` 만료 + 음성 옵션)             | ◐ 부분    | 채팅·`until`=`demo_server.py` `/api/directive`(시뮬레이션 시각 기준 만료) · `tests/test_nl_directive.py`. **음성 입력 미구현**(아래) |
| 6     | 데모 시각화 + 문서·불변식 스윕                                                  | ✅ 완료   | `web/assets/app.js` 파이프라인 시각화 · CI 불변식 게이트                   |

> **Phase 5 의 잔여분**: `nl_directive` 는 `source: chat | voice` 를 *데이터
> 필드로만* 정의한다. 음성 입력 경로(STT·마이크 UI)는 코드에 없다 — 지시는
> 채팅으로만 들어온다. 'voice' 값이 존재한다는 이유로 구현됐다고 읽지 말 것.

> 스펙-우선: 이 시그니처가 목표 계약이다. 구현이 이 문서를 따른다(변경 시 이
> 문서를 먼저 갱신).

> **데모 리뉴얼(2026-07-10)**: 관제실 데모(`web/app.html`·`demo_server`)가 후보
> 생성(규칙 + 제네릭 런타임 후보) → 아비터(`CandidateEvaluator`) 융합 → 개인화
> 피드백 루프(`/api/feedback` → 임베딩 EMA) → 자연어 지시(`/api/directive`) →
> 시각화를 **결정론 Fake 위에서 실동작**시킨다. 여기에 런타임 등록
> (`RuntimeRegistry`)과 카탈로그 파생 허용목록(아래 §신설)이 더해졌다.
> `proposal_lines`(Line B)·`event_detector`(트리거)는 **`app.tick()` 에 정식
> 배선을 마쳤다**(위 표 Phase 1·4). 남은 것은 실 ML/LLM/임베딩 *학습* 으로,
> 인터페이스 뒤 결정론 Fake + env 슬롯으로 대체돼 있다.

---

## `liot.registry` — 런타임 등록(RuntimeRegistry) · 카탈로그 파생 허용목록

관제실 데모가 센서·기기·행동·규칙을 런타임에 등록하는 **단일 원천**. 앱은 생성
시점 카탈로그 스냅샷을 쓰므로, `demo_server` 는 요청마다 `LiotApp` 을 새로 만들어
등록 직후 반영한다.

```
# liot.registry
class RuntimeRegistry:
    def add_sensor(meta: dict) -> dict          # 센서 등록(정규화·검증 → coerce 규칙 확정)
    def add_device(...) -> dict                 # 기기 + capability 등록
    def add_action(...) -> dict                 # 기기 capability 에 action(+tier) 등록
    def add_rule(spec: dict) -> dict            # 런타임 능동 규칙(닫힌 비교자·enum 매치만)
    def to_catalog() -> DeviceCatalog           # 베이스라인 ∪ 등록분 병합 카탈로그
    def proactive_allowed() -> frozenset[tuple[str,str]]     # = derive_proactive_allowed(to_catalog())
    def executable_extras() -> frozenset[tuple[str,str]]     # = proactive_allowed(); executor 가 FORBIDDEN_OPEN 재차감
    def proactive_ruleset() -> ProactiveRuleSet             # config 베이스라인(必) ∪ 런타임 규칙, limits 는 베이스라인 승계
    def categories() -> dict[str,str]           # entity_id → category (LiotApp(categories=...) 주입)
    def snapshot()/restore(snap)/reset()        # 세션 스코프 상태(프리셋 원자성·전체 초기화)

# liot.layers.l2_context.proactive_rules  — 카탈로그 파생 허용목록(하드코딩 상수 제거)
SAFETY_ACTUATOR_CAPABILITIES: frozenset[str] = {"lock","valve","oven"}  # 데이터로 못 넓히는 하드 블록리스트
def derive_proactive_allowed(catalog) -> frozenset[tuple[str,str]]
    # (cap,action) ∈ 허용  ⟺  cap ∉ SAFETY_ACTUATOR_CAPABILITIES  AND  catalog.risk_for(cap,action,{}) ≤ BREATH
    # 열거원 = capability_actions()(raw 맵), NOT available_actions()(기기 조인) — 출하 6튜플 동치 유지

# extend-only 안전 결합(넓히기만 가능, 내장 하한 불변)
liot.core.risk.register_safety_category(cat: str) -> str          # 안전 카테고리 추가(제거 API 없음)
liot.layers.l3_judgment.guardrail.register_category_floor(cat, tier) -> RiskTier   # 카테고리 하한 승격만
```

- **extend-only 불변**: 등록은 안전 하한을 **넓힐 수만** 있다. `register_safety_category`
  는 내장 `SAFETY_CRITICAL_CATEGORIES` frozenset 을 건드리지 않고 확장 set 에만
  더하며(is_safety_category = 내장 ∪ 확장), `register_category_floor` 는 하한을
  올리기만 한다. 낮추거나 제거하는 경로는 존재하지 않는다(R&E 핵심).
- **제네릭 후보 + 런타임 규칙**: `add_rule` 은 상태 술어(닫힌 비교자
  `eq/ne/lt/le/gt/ge`) + enum 매치(`time_bucket`/`occupancy`)만 받는다(스크립트·
  표현식 불가). tier ≤ breath 로 등록된 비-안전-액추에이터 행동은 파생 허용목록에
  자동 진입해 능동 제안 대상이 되고, 허용목록 통과 후에도 안전 카테고리 타깃 거부·
  capability 일치·상한 재검이 별도로 적용된다(다중 deny-by-default).

검증: `tests/test_registry.py` · `tests/test_demo_flow.py`(프리셋 등록·원자적
롤백·simulate 도달) · `tests/test_proactive.py::test_derive_proactive_allowed_equals_shipped_six_tuples`(출하 6튜플 동치) ·
`tests/test_invariants.py`(FORBIDDEN_OPEN 은 어떤 확장에도 누출 불가).

---

## `liot.layers.l4_execution` + `liot.service` — 피드백 링크(1회용 토큰) 계약

알림에 실려 나가는 수락/거절 링크의 **서명·수명** 규약. 2026-09-01·09-13 리뷰가
이 경계에서 결함 4건을 냈으므로(서명↔검증 비대칭·비-ASCII 500·2차 링크 조기 사망·
그 반작용으로 무제한 유효) 시그니처와 함께 **어느 쪽으로 실패해야 하는지**를 적는다.

```python
# liot.layers.l4_execution.notifier — 서명(정본)
def tier_int(value: RiskTier | str | int) -> int
    # 이름("ember")·숫자·숫자 문자열("3") 전부 수용, 0~3 밖은 ValueError.
    # 서명·검증이 **같은 함수**를 쓴다(한쪽만 고치면 비대칭이 되살아난다).
def sign_deeplink(secret, proposal_id, risk_tier, nonce="") -> str
    # liot://p/<pid>?t=<tier>&sig=<hmac>[&n=<nonce>] — 서명 대상 = "<pid>:<tier_int>"
    # nonce 는 서명 대상이 아니라 운반물(transport 가 읽어 액션 버튼 서명을 만든다).
def sign_feedback(secret, proposal_id, risk_tier, action, nonce="") -> str
    # 서명 대상 = "<pid>:<tier_int>:<action>:<nonce>" — action·nonce 바인딩.

# liot.layers.l4_execution.feedback_gateway — 검증
def compare_token(expected: str, given: object) -> bool
    # 상수 시간 비교. **비-ASCII 는 예외가 아니라 불일치**(hmac.compare_digest 가
    # TypeError 를 던져 수신구가 500 을 내면 403 과의 차이가 nonce 생존 신호가 된다).
def verify_feedback_sig(secret, proposal_id, tier, action, sig, nonce="") -> bool
def verify_deeplink_sig(secret, proposal_id, tier, sig) -> bool

# liot.layers.l4_execution.proposal_service
async def states_for(ids: Sequence[str]) -> dict[str, tuple[str, str | None]]
    # (state, expires_at) 을 한 번의 SELECT 로. 토큰 스윕이 토큰마다 get() 하지
    # 않게 하는 용도. expires_at 이 필요한 이유는 아래 수명 규칙 2번.
```

**링크 수명 규칙**(`liot.service`, `_nonce_is_live`):

1. `pending` 제안의 링크는 살아 있다. 만료는 `expire_due` 가 상태로 옮긴다
   (그래서 만료 시각과 다음 틱 사이에는 아직 유효 — 의도된 비대칭).
2. `accepted` + 이중확인 2차 대기(또는 2차 알림 미발송)의 링크는 **상한 둘**을
   모두 넘지 않아야 살아 있다: 제안 자신의 `expires_at`, 그리고 발급 후
   `_SECOND_CONFIRM_WINDOW`(15분). `expire_due` 는 `pending` 만 전이하므로 상태만
   보면 이 링크는 영원히 살아 있게 된다.
3. 판정 시각은 호출자가 준다 — 스윕은 틱 시각, 클릭 경로는 클릭 시각. 스윕은
   틱에만 돌기 때문에 클릭 경로에서도 같은 검사를 한다.
4. 죽은 토큰은 `_drop_nonce` 가 nonce·발급시각·2차 대기 표식을 **한 덩어리로**
   버린다(표식만 남으면 다음 토큰이 상한 없이 되살아난다). 종결 피드백 경로는
   표식을 따로 내리고, 스윕은 '표식만 남은 pid' 까지 함께 돌아 자기교정한다.
5. 실행 권한은 **발송 성공한 2차 링크**에만 있다(`_awaiting_second`). 표식은 발급
   전에 세우고 발송 실패 시 내린다 — 비동기 transport 로 바뀌어도 발급과 표식
   사이에 스윕이 끼어들 창이 없게.

검증: `tests/test_execution_wiring.py`(2차 링크 생존·창 만료·제안 만료·표식 누수·
출하 카탈로그에 단일확인 실행 조합 0) · `tests/test_service.py`(서명↔검증 강제변환
일치·nonce 바인딩·비-ASCII 거부) · `tests/test_feedback_exposure.py`(dev 키 게이트).

## 상황 파급도·거주자·보호자 — 신규 모듈 계약 (2026-10)

```python
# liot.core.severity — 상황 파급도(행동 확인 강도 RiskTier 와 별개 축)
class SeverityLevel(IntEnum): RECORD=0; NOTICE=1; WARNING=2; CRITICAL=3   # .label_ko · .ntfy_priority
def escalate_severity(cur, target) -> SeverityLevel          # max — 승격만
SeveritySource = Literal["table", "resident", "model"]       # 기록 순서 고정
def load_severity_table(path=None) -> SeverityTable          # config/severity/situations.yaml
SeverityTable.compute(category, *, vulnerable_resident) -> SeverityResult | None   # 비안전 → None
SeverityTable.is_abnormal(category, norm_value) -> bool      # 정규화 불가 → 이상(fail-closed)
SeverityTable.load_failed / load_error                       # 실패 시 모든 안전 카테고리 CRITICAL

# liot.db.safety_repo.SafetyRepo(db) — 마이그레이션 3
#   거주자(사람 단위, 특성 4종 + entered_by) · 가구(lives_alone, home_alias)
#   보호자(비밀 토픽, 비활성화) · 동의(집×보호자, append-only 이벤트)
#   상황 에피소드(엔티티당 열린 것 1개, severity_floor 는 오르기만) · 사다리 상태 · 전달 이력 · 판단 저장

# liot.layers.l3_judgment.decision_backend — llama.cpp 결정 모델(LIOT_LLM_BACKEND=decision)
#   LIOT_DECISION_URL(기본 http://127.0.0.1:18765) · LIOT_DECISION_MODEL · LIOT_DECISION_TIMEOUT(요청당, 1.5s)
#   질문 2개(행동 4지선다 · 파급도 4지선다, severity_floor 있을 때만) — 글자 확률 argmax, 전부 없으면 LLMUnavailable
# LLMJudge.judge(..., severity_floor=None, severity_sources=(), situation_id=None)
#   severity = escalate_severity(floor, model); 하한 아래 답 → injection_suspect

# liot.layers.l4_execution.escalation — 사다리 규칙(상수 전부 tunable)
#   CRITICAL 재알림 1분 · WARNING 10분×3 · 보호자 전달 90초 · 119 안내 +5분(보호자 0명이면 90초+5분)
#   guardian_path(floor) — 보호자·119 는 severity_floor 기준, 재알림 주기는 최종 파급도 기준
# liot.layers.l4_execution.executor.ONE_TAP_SAFE_ACTIONS = {(valve, close), (lock, lock)}  # ⊆ SAFETY_EXECUTABLE
```

흐름(서비스): 안전 신호가 이상이면 다음 순서로 처리한다.
1. 에피소드를 연다.
2. 표+거주자로 제안을 만들어 **즉시 발송**한다.
3. 결정 모델 판정을 백그라운드에서 돌린다. 올렸으면 같은 `situation_id` 로 후속 제안을 보낸다.
4. 사다리를 시작한다.

정상 신호가 오면 다음 순서로 처리한다.
1. 에피소드를 닫는다.
2. 그 상황의 대기 제안을 만료시킨다(탭 한 번 링크 포함).
3. 거주자에게 해소 알림을 보낸다.
4. 사다리를 취소하고, 전달받은 보호자에게 "해소됨"을 보낸다.

검증: `tests/test_severity_*.py` · `test_safety_repo.py` · `test_migration_v3.py` · `test_decision_backend.py`
· `test_notifier_severity.py` · `test_one_tap_safe_actions.py` · `test_pipeline_situations.py`
· `test_escalation_ladder.py` · `test_demo_server_safety.py`. 시험 세트·후보 모델 측정은 `bench/risk/`.
