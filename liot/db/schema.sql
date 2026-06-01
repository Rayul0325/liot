-- LIOT Phase1 스키마.
--
-- 규칙:
--   * 모든 테넌트 테이블은 home_id TEXT NOT NULL DEFAULT 'singleton' 을 갖는다.
--   * 모든 판단/제안/피드백/감사 행은 추적 가능해야 한다(trace_id, snapshot_hash 등).
--   * forward-only. 컬럼 삭제/이름변경 금지(migrations.py 는 추가만).
--
-- 이 파일은 schema_version 0 -> 1 의 "기준 스키마"다. 이후 변경은
-- migrations.py 의 단계로만 추가한다.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 스키마 버전: forward-only 마이그레이션 진행 상태.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,        -- 적용된 마이그레이션 번호
    applied_at  TEXT    NOT NULL,        -- 적용 시각(UTC ISO8601)
    note        TEXT
);

-- ---------------------------------------------------------------------------
-- 사용자 선호도(L2 preference_store 가 읽고 씀).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preferences (
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    key         TEXT NOT NULL,
    value       TEXT,                    -- JSON 직렬화된 값
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (home_id, key)
);

-- ---------------------------------------------------------------------------
-- 센서 읽기(L1 normalizer 산출물의 영속 사본).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sensor_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    entity_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    raw_value   TEXT,                    -- 원문 JSON
    norm_value  TEXT,                    -- 정규화 값 JSON
    unit        TEXT,
    confidence  REAL NOT NULL DEFAULT 1.0,
    ts_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_home_entity_ts
    ON sensor_readings (home_id, entity_id, ts_utc);

-- ---------------------------------------------------------------------------
-- 맥락 스냅샷(L2 산출물). snapshot_hash 로 판단 재현성을 보장.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id          TEXT NOT NULL,
    home_id              TEXT NOT NULL DEFAULT 'singleton',
    trace_id             TEXT NOT NULL,
    trigger              TEXT NOT NULL,  -- JSON
    structured           TEXT NOT NULL,  -- JSON, snapshot_hash 의 원본
    nl_summary           TEXT NOT NULL,
    summary_template_sha TEXT NOT NULL,
    snapshot_hash        TEXT NOT NULL,  -- sha256(structured)
    created_at           TEXT NOT NULL,
    PRIMARY KEY (home_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_trace ON context_snapshots (home_id, trace_id);

-- ---------------------------------------------------------------------------
-- 판단(L3 산출물).
--   * snapshot_hash + bundle_id + rationale 를 함께 보존 → 재현 가능성(불변식 4).
--   * risk_tier 는 max(gate1_tier, llm_tier). gate2 는 gate1 을 무를 수 없다(불변식 2).
--   * gate2_verdict 가 NULL 이면 LLM 미사용(결정론 경로 / Safety-Never-Silent).
--   * degraded=1 이면 LLM 불가 등으로 보수적으로 처리된 판단.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS judgments (
    judgment_id   TEXT NOT NULL,
    home_id       TEXT NOT NULL DEFAULT 'singleton',
    trace_id      TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,         -- 어떤 스냅샷에 대한 판단인가(재현 키)
    action_json   TEXT NOT NULL,         -- CandidateAction 직렬화
    risk_tier     INTEGER NOT NULL,      -- RiskTier 정수값(0..3)
    confidence    REAL NOT NULL,
    rationale     TEXT NOT NULL,         -- 사람이 읽는 판단 근거(내부 보관)
    gate1_verdict TEXT NOT NULL,         -- 결정론 가드레일 평결
    gate2_verdict TEXT,                  -- LLM 평결(없으면 NULL)
    bundle_id     TEXT NOT NULL,         -- 적용된 규칙 번들 식별자(재현 키)
    model_id      TEXT NOT NULL,         -- 사용된 LLM/판단기 식별자(재현 키)
    degraded      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (home_id, judgment_id)
);
CREATE INDEX IF NOT EXISTS idx_judgments_trace ON judgments (home_id, trace_id);

-- ---------------------------------------------------------------------------
-- 제안(L4 산출물). Phase1 의 최종 사용자 대면 산출물.
--   * Phase1 = 제안-만: 어떤 제안도 자동 실행되지 않는다.
--   * required_confirmation 은 risk_tier 에서 파생(none/light/explicit/double).
--   * perishable/expires_at: 시의성 있는 제안(예: 외출 직전)의 만료 처리.
--   * state: pending/accepted/dismissed/expired/superseded.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id           TEXT NOT NULL,
    home_id               TEXT NOT NULL DEFAULT 'singleton',
    judgment_id           TEXT NOT NULL,
    risk_tier             INTEGER NOT NULL,
    action_json           TEXT NOT NULL,
    rationale             TEXT NOT NULL, -- 내부 보관(외부 송신 페이로드엔 미포함)
    required_confirmation TEXT NOT NULL,
    perishable            INTEGER NOT NULL DEFAULT 0,
    expires_at            TEXT,
    state                 TEXT NOT NULL DEFAULT 'pending',
    created_at            TEXT NOT NULL,
    PRIMARY KEY (home_id, proposal_id)
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals (home_id, state);
CREATE INDEX IF NOT EXISTS idx_proposals_judgment ON proposals (home_id, judgment_id);

-- ---------------------------------------------------------------------------
-- 피드백(USER → 시스템). L4→L2 학습 루프의 입력.
--   * user_action: accepted/dismissed/snoozed/stop 등.
--   * stop_scope: 'this'/'category'/'entity'/'all' 등 중단 범위.
--   * latency_ms: 제안 노출부터 반응까지 지연(선호 학습 신호).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    proposal_id TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    user_action TEXT NOT NULL,
    stop_scope  TEXT,
    comment     TEXT,
    latency_ms  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_proposal ON feedback (home_id, proposal_id);

-- ---------------------------------------------------------------------------
-- 알림(외부 송신 기록). egress_gate 를 통과한 화이트리스트 키만 보존.
--   * device/room/rationale 는 구조적으로 저장하지 않는다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    home_id        TEXT NOT NULL DEFAULT 'singleton',
    proposal_id    TEXT NOT NULL,
    channel        TEXT NOT NULL,
    risk_tier      INTEGER NOT NULL,
    glyph          TEXT NOT NULL,
    label          TEXT NOT NULL,
    signed_deeplink TEXT NOT NULL,
    topic          TEXT NOT NULL,
    sent_at        TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 감사 로그(append-only). 누가/무엇이/언제 무엇을 했는지 추적.
--   * 보안·재현·디버깅의 단일 진실원. 절대 갱신/삭제하지 않는다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    trace_id    TEXT,
    actor       TEXT NOT NULL,           -- 'L1'/'L2'/'L3'/'L4'/'USER'/'SYSTEM'
    event_type  TEXT NOT NULL,
    ref_id      TEXT,                    -- 관련 엔티티 id
    detail      TEXT,                    -- JSON(외부 미송신, 내부 전용)
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log (home_id, trace_id);

-- ---------------------------------------------------------------------------
-- LLM 캐시(결정론적 재현 + 비용 절감). 키=프롬프트 해시.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_cache (
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    prompt_hash TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    response    TEXT NOT NULL,           -- JSON
    created_at  TEXT NOT NULL,
    PRIMARY KEY (home_id, prompt_hash, model_id)
);

-- ---------------------------------------------------------------------------
-- 신뢰 상태(시각/소스 신뢰). clock.trust_ok 등의 영속화.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_state (
    home_id     TEXT NOT NULL DEFAULT 'singleton',
    key         TEXT NOT NULL,           -- 예: 'clock', 'sensor:<entity>'
    trust_ok    INTEGER NOT NULL DEFAULT 1,
    reason      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (home_id, key)
);
