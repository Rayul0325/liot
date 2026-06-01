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
)
from liot.core.invariants import (
    InvariantError, TopologyViolation, IsolationError, EgressViolation, MonotonicityError,
    assert_monotonic, assert_executor_absent, ExecutorProtocol,
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

class EventMessage(BaseModel):
    trace_id: str; home_id: str; layer_from: Layer; layer_to: Layer
    type: str; ref_id: str; partition_key: str; ts: str

def compute_snapshot_hash(structured: dict[str,Any]) -> str
    # sha256(hex). canonical JSON: sort_keys=True, 컴팩트 구분자, default=str. 키 순서 무관.
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
def assert_executor_absent(package_name="liot.layers.l4_execution") -> None
    # 패키지 전 서브모듈을 importlib/pkgutil 스캔. 이름에 'Executor' 포함된 구체 클래스 발견 시 InvariantError.
    # ABC(추상 메서드 잔존)/Protocol(_is_protocol) 기반 인터페이스는 허용. 패키지 부재면 통과.

@runtime_checkable
class ExecutorProtocol(Protocol):
    async def execute(self, *args, **kwargs) -> object: ...
    # 추상 인터페이스를 정의하고 싶을 때 베이스로 사용 (assert_executor_absent 가 허용).
```

L4 작성자 주의: **구체 Executor 클래스 금지**. 이름에 `Executor` 가 들어가는 클래스를
구현하면 `assert_executor_absent()` 가 실패한다. 인터페이스가 필요하면 `ExecutorProtocol`
또는 추상 메서드가 남은 ABC 로만 선언하라.

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
feedback, notifications, audit_log, trust_state`.

---

## `liot.core.egress_gate`  — 모든 외부 송신의 단일 통로

```python
class Destination(str, Enum): LLM="LLM"; NTFY="NTFY"; KMA="KMA"
ALLOWLIST: frozenset[Destination]    # = {LLM, NTFY, KMA}
NTFY_ALLOWED_KEYS: frozenset[str]
    # = {'channel','proposal_id','risk_tier','glyph','label','signed_deeplink','topic'}
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
LATEST_VERSION: int                   # = 1
async def current_version(db) -> int                 # 없으면 0
async def run_migrations(db) -> int                  # forward-only, 멱등. 적용 후 최신 버전 반환.
```

### `database.py`
```python
async def connect(path: str, durable: bool=False) -> aiosqlite.Connection
    # row_factory=aiosqlite.Row, journal_mode=WAL, foreign_keys=ON,
    # synchronous = FULL(durable=True) / NORMAL(durable=False). 호출자가 close.
async def init_db(path: str, durable: bool=False) -> aiosqlite.Connection
    # connect 후 run_migrations 까지 적용한 연결 반환.
```

---

## 6대 불변식 ↔ 코어 매핑 (테스트가 검증할 지점)

1. 단방향 파이프라인 → `event_bus.ALLOWED_EDGES` + `TopologyViolation`.
2. Gate2 는 Gate1 을 무를 수 없다 → 최종 `risk_tier = escalate_only(gate1, gate2)` (`max`).
3. Phase1 = 제안-만 → `assert_executor_absent()` (구체 Executor 0개).
4. 재현 가능한 판단 → `Judgment.snapshot_hash` + `bundle_id` + `rationale` 동시 보존
   (DB `judgments` 동일 컬럼). `compute_snapshot_hash` 가 결정론적.
5. 승격만, 강등 불가 → `escalate_only` + `assert_monotonic`(`MonotonicityError`).
6. Safety-Never-Silent → `SAFETY_CRITICAL_CATEGORIES` 의 트리거는 LLM 없이
   Gate1 결정론만으로 즉시 EMBER 제안 생성 (`gate2_verdict=None` 허용, `degraded` 무관).
