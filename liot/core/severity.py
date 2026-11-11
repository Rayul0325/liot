"""상황 파급도(Severity) 값 모델.

행동 확인 강도(``RiskTier``)와 별개의 축이다. ``RiskTier`` 는 "이 행동을 실행하기
전에 얼마나 세게 확인받나"이고, 파급도는 "이 상황을 아무도 손대지 않으면 얼마나
큰 피해가 나나"다. 둘을 한 값으로 섞지 않는다.

파급도 4단계(오름차순):
    RECORD(0, 기록만) < NOTICE(1, 주의) < WARNING(2, 경고) < CRITICAL(3, 위험)

핵심 불변식:
  - 파급도도 자동 승격만 허용: ``escalate_severity(cur, target) == max(cur, target)``.
  - 표+거주자 값(severity_floor) 아래로는 어떤 모델·지시도 내리지 못한다.

표·매핑·계산기(``SeverityTable`` / ``load_severity_table``):
  - 두 축은 CAP 1.2 를 **우리 식으로 각색**한 것이다(CAP 원문 정의 그대로가 아니다).
      피해 유형(DamageType): life | injury | property | inconvenience
      피해까지 시간(TimeToHarm, 아무도 손대지 않으면):
          minutes(CAP Immediate) | hour(CAP Expected, 1시간 안) | later(CAP Future)
  - 두 축의 점수를 곱하지 않는다. 서열 척도끼리 곱하면 순위가 뒤집힐 수 있다
    (Cox 2008, FMEA RPN 비판, AIAG-VDA 2019 가 RPN 을 조회표로 바꾼 근거).
    그래서 조회표 한 칸을 그대로 쓴다.
  - 표·상황 매핑·이상/정상 규칙은 ``config/severity/situations.yaml`` 에만 있다.
    파이썬 상수로 표를 박아 두지 않는다. ``config/rules/`` 에 두면 Gate1 규칙으로
    로드되므로 절대 그리 옮기지 않는다.
  - 로드 실패·형식 오류 → fail-closed: 모든 안전 카테고리가 CRITICAL 이고
    ``load_failed`` / ``load_error`` 로 실패를 드러내며 logging 경고를 남긴다.
  - 거주자 취약(+1): 한 단계만 올리고 CRITICAL 에서 멈춘다(겹치지 않는다).
  - 센서 확실도는 파급도를 내리지 못한다(여기서 할 일 없음 — 내리는 경로 자체가 없다).

판정 보류(감시 불가):
  - 정규화 값이 문자열 ``unavailable``·``unknown``(HA 가 기기와 연결이 끊겼거나 재시작
    중일 때 보내는 상태)이면 이상도 정상도 아닌 '판정 보류'다(``is_unmonitorable``).
    이 값으로는 위험 상황을 열지도 닫지도 않는다 — 처리는 서비스가 따로 한다.
  - 그 밖의 정규화 불가 값(깨진 문자열·빈 값·NaN·None)은 지금처럼 이상(fail-closed)이다.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Literal

import yaml

from .risk import is_safety_category

__all__ = [
    "SeverityLevel",
    "SeveritySource",
    "SEVERITY_SOURCE_ORDER",
    "escalate_severity",
    "DamageType",
    "TimeToHarm",
    "DAMAGE_TYPES",
    "TIMES_TO_HARM",
    "damage_rank",
    "SeverityResult",
    "severity_sort_key",
    "AbnormalRule",
    "UNMONITORABLE_VALUES",
    "is_unmonitorable",
    "Situation",
    "SeverityConfigError",
    "SeverityTable",
    "load_severity_table",
    "DEFAULT_SITUATIONS_PATH",
]

logger = logging.getLogger(__name__)

# 파급도가 어디서 왔는지. 기록 순서는 SEVERITY_SOURCE_ORDER 를 따른다.
SeveritySource = Literal["table", "resident", "model"]
SEVERITY_SOURCE_ORDER: tuple[SeveritySource, ...] = ("table", "resident", "model")


class SeverityLevel(IntEnum):
    """상황 파급도. IntEnum 이므로 정수 비교로 대소를 비교한다."""

    RECORD = 0
    NOTICE = 1
    WARNING = 2
    CRITICAL = 3

    @classmethod
    def from_str(cls, value: str | SeverityLevel | int) -> SeverityLevel:
        """문자열/정수/SeverityLevel 을 SeverityLevel 로 정규화한다.

        대소문자와 양옆 공백을 무시한다. 알 수 없는 값은 ``ValueError``.
        """
        if isinstance(value, SeverityLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().upper()
        try:
            return cls[key]
        except KeyError as exc:
            raise ValueError(f"unknown severity level: {value!r}") from exc

    @property
    def label_ko(self) -> str:
        """한국어 라벨."""
        return _LABEL_KO[self]

    @property
    def ntfy_priority(self) -> str:
        """이 파급도의 ntfy 푸시 세기(문자열 '2'..'5')."""
        return _NTFY_PRIORITY[self]


_LABEL_KO: dict[SeverityLevel, str] = {
    SeverityLevel.RECORD: "기록만",
    SeverityLevel.NOTICE: "주의",
    SeverityLevel.WARNING: "경고",
    SeverityLevel.CRITICAL: "위험",
}

_NTFY_PRIORITY: dict[SeverityLevel, str] = {
    SeverityLevel.RECORD: "2",
    SeverityLevel.NOTICE: "3",
    SeverityLevel.WARNING: "4",
    SeverityLevel.CRITICAL: "5",
}


def escalate_severity(
    cur: SeverityLevel | str | int, target: SeverityLevel | str | int
) -> SeverityLevel:
    """파급도는 자동 승격만 가능하다. 결과 = max(cur, target)."""
    cur_s = SeverityLevel.from_str(cur)
    target_s = SeverityLevel.from_str(target)
    return cur_s if cur_s >= target_s else target_s


# ── 표의 두 축 ─────────────────────────────────────────────────────────────
# CAP 1.2 를 우리 식으로 각색한 축이다. 튜플 순서가 곧 정렬 순서다
# (피해 유형: 같은 파급도 안에서 life > injury > property > inconvenience).
DamageType = Literal["life", "injury", "property", "inconvenience"]
TimeToHarm = Literal["minutes", "hour", "later"]
DAMAGE_TYPES: tuple[DamageType, ...] = ("life", "injury", "property", "inconvenience")
TIMES_TO_HARM: tuple[TimeToHarm, ...] = ("minutes", "hour", "later")

DEFAULT_SITUATIONS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "severity" / "situations.yaml"
)


def damage_rank(d: DamageType | None) -> int:
    """피해 유형 정렬 키. 작을수록 앞(life=0 … inconvenience=3).

    피해 유형을 모르는 경우(None — 매핑 없음·fail-closed)는 맨 뒤(4)로 보낸다.
    알 수 없는 문자열은 ``ValueError``.
    """
    if d is None:
        return len(DAMAGE_TYPES)
    try:
        return DAMAGE_TYPES.index(d)
    except ValueError as exc:
        raise ValueError(f"unknown damage type: {d!r}") from exc


@dataclass(frozen=True)
class SeverityResult:
    """한 상황의 파급도 계산 결과.

    - floor: 표+거주자 값(어떤 모델·지시도 이 아래로 내리지 못하는 하한).
    - sources: SEVERITY_SOURCE_ORDER 순서. 'table' 은 항상 있고, 'resident' 는
      취약 거주자 올림이 실제로 단계를 올렸을 때만, 'model' 은 후속 작업이 붙인다.
    - damage/time_to_harm: 매핑 행이 있을 때만. 매핑 없음·fail-closed 면 None.
    """

    floor: SeverityLevel
    sources: tuple[SeveritySource, ...]
    damage: DamageType | None
    time_to_harm: TimeToHarm | None


def severity_sort_key(result: SeverityResult) -> tuple[int, int]:
    """결과 정렬 키: 파급도 높은 것 먼저, 같은 단계에선 피해 유형 순(life 먼저)."""
    return (-int(result.floor), damage_rank(result.damage))


# 판정 보류 값: HA 가 기기 상태를 읽지 못할 때 보내는 연결 상태 문자열(정규화 뒤 소문자).
# 이 값은 '위험'도 '정상'도 아니다. 여기 없는 문자열은 지금처럼 이상(fail-closed)이다.
UNMONITORABLE_VALUES: frozenset[str] = frozenset({"unavailable", "unknown"})


def is_unmonitorable(norm_value: object) -> bool:
    """정규화 값이 '판정 보류'(감시 불가)인가. 카테고리·표와 무관하다.

    정규화를 마친 값(소문자·양옆 공백 제거)을 그대로 비교한다. 비슷한 문자열이나
    문자열이 아닌 값은 보류가 아니다.
    """
    return isinstance(norm_value, str) and norm_value in UNMONITORABLE_VALUES


@dataclass(frozen=True)
class AbnormalRule:
    """이상/정상 판정 규칙.

    - kind='truthy': 정규화 값이 True 면 이상, False 면 정상.
    - kind='above' : 수치가 threshold 초과면 이상, 이하면 정상.
    판정 보류 값(``is_unmonitorable``)은 이상이 아니다(정상도 아니다 — 호출자가 먼저 거른다).
    그 밖에 어느 쪽으로도 판정 못 하는 값은 이상이다(fail-closed).
    """

    kind: Literal["truthy", "above"]
    threshold: float | None = None

    def is_abnormal(self, norm_value: object) -> bool:
        """정규화 값이 이상이면 True. 판정 불가 값도 True(fail-closed).

        판정 보류 값(unavailable·unknown)은 False 다. False 가 곧 '정상'은 아니므로
        안전 경로는 ``is_unmonitorable`` 을 먼저 확인해야 한다.
        """
        if is_unmonitorable(norm_value):
            return False
        if self.kind == "truthy":
            # 정상은 False 와 숫자 0(정규화기가 정수 0 을 0.0 으로 바꾼다) 뿐.
            # True·0 아닌 수는 이상, 그 외(정규화 불가)도 이상(fail-closed).
            if norm_value is False:
                return False
            is_zero = (
                isinstance(norm_value, (int, float))
                and not isinstance(norm_value, bool)
                and norm_value == 0
            )
            return not is_zero
        # 'above': bool 은 int 의 하위형이지만 온도 값이 아니므로 판정 불가로 본다.
        if (
            self.threshold is None
            or isinstance(norm_value, bool)
            or not isinstance(norm_value, (int, float))
        ):
            return True
        # 정상은 '임계 이하'가 참일 때뿐. 초과는 이상이고, NaN 은 어느 비교도 참이
        # 아니라 판정 불가 → 이상. (`> threshold` 로 쓰면 NaN 이 정상으로 샌다.)
        return not norm_value <= self.threshold


_TRUTHY_RULE = AbnormalRule(kind="truthy")


@dataclass(frozen=True)
class Situation:
    """상황 매핑 한 행: 안전 카테고리 → (피해 유형, 피해까지 시간) + 이상 규칙."""

    category: str
    damage: DamageType
    time_to_harm: TimeToHarm
    abnormal: AbnormalRule


class SeverityConfigError(ValueError):
    """situations.yaml 이 형식을 어겼을 때. 로더가 잡아 fail-closed 로 바꾼다."""


def _normalize_category(cat: str | None) -> str:
    """카테고리 정규화(risk.py 와 같은 방식: 양옆 공백 제거 + 소문자)."""
    return (cat or "").strip().lower()


class SeverityTable:
    """파급도 조회표 + 상황 매핑. ``load_severity_table`` 로 만든다.

    로드에 실패한 표(``load_failed``)는 모든 안전 카테고리를 CRITICAL 로 돌려준다.
    """

    def __init__(
        self,
        *,
        cells: Mapping[tuple[DamageType, TimeToHarm], SeverityLevel],
        situations: Mapping[str, Situation],
        disabled: frozenset[str],
        source: str,
        load_error: str | None = None,
    ) -> None:
        self._cells: dict[tuple[DamageType, TimeToHarm], SeverityLevel] = dict(cells)
        self._situations: dict[str, Situation] = dict(situations)
        self._disabled = disabled
        self._source = source
        self._load_error = load_error

    @classmethod
    def failed(cls, *, source: str, reason: str) -> SeverityTable:
        """fail-closed 표: 칸·매핑이 없고, 안전 카테고리는 전부 CRITICAL."""
        return cls(cells={}, situations={}, disabled=frozenset(), source=source, load_error=reason)

    # ── 상태 ──
    @property
    def load_failed(self) -> bool:
        """설정 로드에 실패해 fail-closed 상태인지."""
        return self._load_error is not None

    @property
    def load_error(self) -> str | None:
        """로드 실패 사유(성공이면 None). 후속 작업이 감사 로그에 남긴다."""
        return self._load_error

    @property
    def source(self) -> str:
        """설정 파일 경로."""
        return self._source

    @property
    def disabled_categories(self) -> frozenset[str]:
        """설정에 있지만 판정에 쓰지 않는 예약 행(enabled: false)."""
        return self._disabled

    # ── 조회 ──
    def cell(self, damage: DamageType, time_to_harm: TimeToHarm) -> SeverityLevel:
        """표 한 칸. 없는 칸(fail-closed 표 포함)은 ``KeyError``."""
        return self._cells[(damage, time_to_harm)]

    def cells(self) -> dict[tuple[DamageType, TimeToHarm], SeverityLevel]:
        """표 전체(사본)."""
        return dict(self._cells)

    def situation(self, category: str) -> Situation | None:
        """활성 매핑 행. 없거나 예약(비활성) 행이면 None."""
        return self._situations.get(_normalize_category(category))

    # ── 계산 ──
    def compute(self, category: str, *, vulnerable_resident: bool) -> SeverityResult | None:
        """카테고리의 파급도 하한을 계산한다.

        - 비안전 카테고리 → None(상황이 열리지 않는다).
        - fail-closed 표 또는 매핑 행 없는 안전 카테고리 → CRITICAL.
        - 매핑 행 있음 → 표의 (피해 유형, 시간) 칸.
        - ``vulnerable_resident`` 면 한 단계만 올리고 CRITICAL 에서 멈춘다.
        """
        cat = _normalize_category(category)
        if not is_safety_category(cat):
            return None

        sit = self._situations.get(cat)
        damage: DamageType | None = None
        time_to_harm: TimeToHarm | None = None
        if sit is None:
            base = SeverityLevel.CRITICAL
        else:
            damage, time_to_harm = sit.damage, sit.time_to_harm
            base = self._cells.get((damage, time_to_harm), SeverityLevel.CRITICAL)

        floor = base
        sources: list[SeveritySource] = ["table"]
        if vulnerable_resident and base < SeverityLevel.CRITICAL:
            floor = escalate_severity(base, SeverityLevel(int(base) + 1))
            sources.append("resident")
        return SeverityResult(
            floor=floor,
            sources=tuple(s for s in SEVERITY_SOURCE_ORDER if s in sources),
            damage=damage,
            time_to_harm=time_to_harm,
        )

    def is_abnormal(self, category: str, norm_value: object) -> bool:
        """정규화 값이 이 카테고리에서 이상인지. 판정 불가 값은 이상(fail-closed).

        판정 보류 값(unavailable·unknown)은 False(이상도 정상도 아님) — fail-closed
        표에서도 같다. 매핑 행이 없으면(런타임 등록·fail-closed 표) 불리언 규칙을 쓴다.
        """
        sit = self._situations.get(_normalize_category(category))
        rule = sit.abnormal if sit is not None else _TRUTHY_RULE
        return rule.is_abnormal(norm_value)


# ── 로더 ──────────────────────────────────────────────────────────────────

_ROW_KEYS: frozenset[str] = frozenset({"damage", "time", "enabled", "abnormal_when"})
# 문자열 → Literal 값 좁히기용 조회(축 정의 튜플에서 파생 — 값은 따로 적지 않는다).
_DAMAGE_BY_NAME: dict[str, DamageType] = {d: d for d in DAMAGE_TYPES}
_TIME_BY_NAME: dict[str, TimeToHarm] = {t: t for t in TIMES_TO_HARM}


def _parse_level(value: Any, where: str) -> SeverityLevel:
    # bool/int 는 받지 않는다(True 가 NOTICE 로 읽히는 사고 방지). 이름 문자열만.
    if not isinstance(value, str):
        raise SeverityConfigError(f"{where}: severity must be a level name, got {value!r}")
    try:
        return SeverityLevel.from_str(value)
    except ValueError as exc:
        raise SeverityConfigError(f"{where}: {exc}") from exc


def _parse_table(raw: Any) -> dict[tuple[DamageType, TimeToHarm], SeverityLevel]:
    if not isinstance(raw, dict):
        raise SeverityConfigError("'table' must be a mapping")
    rows = {_normalize_category(str(k)): v for k, v in raw.items()}
    if set(rows) != set(DAMAGE_TYPES) or len(rows) != len(raw):
        raise SeverityConfigError(
            f"'table' rows must be exactly {list(DAMAGE_TYPES)}, got {sorted(rows)}"
        )
    cells: dict[tuple[DamageType, TimeToHarm], SeverityLevel] = {}
    for damage in DAMAGE_TYPES:
        row = rows[damage]
        if not isinstance(row, dict):
            raise SeverityConfigError(f"table.{damage} must be a mapping")
        cols = {_normalize_category(str(k)): v for k, v in row.items()}
        if set(cols) != set(TIMES_TO_HARM) or len(cols) != len(row):
            raise SeverityConfigError(
                f"table.{damage} columns must be exactly {list(TIMES_TO_HARM)}, got {sorted(cols)}"
            )
        for time_to_harm in TIMES_TO_HARM:
            cells[(damage, time_to_harm)] = _parse_level(
                cols[time_to_harm], f"table.{damage}.{time_to_harm}"
            )
    return cells


def _parse_abnormal(raw: Any, where: str) -> AbnormalRule:
    if raw is None or raw == "truthy":
        return _TRUTHY_RULE
    if isinstance(raw, dict) and set(raw) == {"above"}:
        threshold = raw["above"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
        ):
            raise SeverityConfigError(f"{where}.abnormal_when.above must be a finite number")
        return AbnormalRule(kind="above", threshold=float(threshold))
    raise SeverityConfigError(
        f"{where}.abnormal_when must be 'truthy' or {{above: <number>}}, got {raw!r}"
    )


def _parse_situations(raw: Any) -> tuple[dict[str, Situation], frozenset[str]]:
    if not isinstance(raw, dict):
        raise SeverityConfigError("'situations' must be a mapping")
    active: dict[str, Situation] = {}
    disabled: set[str] = set()
    seen: set[str] = set()
    for key, row in raw.items():
        cat = _normalize_category(str(key))
        where = f"situations.{cat}"
        if not cat or cat in seen:
            raise SeverityConfigError(f"{where}: empty or duplicate category")
        seen.add(cat)
        if not isinstance(row, dict):
            raise SeverityConfigError(f"{where} must be a mapping")
        unknown = set(row) - _ROW_KEYS
        if unknown:
            raise SeverityConfigError(f"{where}: unknown keys {sorted(unknown)}")
        damage = _DAMAGE_BY_NAME.get(_normalize_category(str(row.get("damage", ""))))
        time_to_harm = _TIME_BY_NAME.get(_normalize_category(str(row.get("time", ""))))
        if damage is None:
            raise SeverityConfigError(f"{where}.damage must be one of {list(DAMAGE_TYPES)}")
        if time_to_harm is None:
            raise SeverityConfigError(f"{where}.time must be one of {list(TIMES_TO_HARM)}")
        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SeverityConfigError(f"{where}.enabled must be a boolean")
        abnormal = _parse_abnormal(row.get("abnormal_when"), where)
        if not enabled:
            disabled.add(cat)
            continue
        active[cat] = Situation(
            category=cat,
            damage=damage,
            time_to_harm=time_to_harm,
            abnormal=abnormal,
        )
    return active, frozenset(disabled)


def _parse_document(doc: Any) -> tuple[
    dict[tuple[DamageType, TimeToHarm], SeverityLevel], dict[str, Situation], frozenset[str]
]:
    if not isinstance(doc, dict):
        raise SeverityConfigError("top-level document must be a mapping")
    if "table" not in doc or "situations" not in doc:
        raise SeverityConfigError("both 'table' and 'situations' are required")
    cells = _parse_table(doc["table"])
    situations, disabled = _parse_situations(doc["situations"])
    return cells, situations, disabled


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """같은 매핑에 같은 키가 두 번 나오면 ``SeverityConfigError`` (fail-closed).

    ``yaml.safe_load`` 는 중복 키를 조용히 "뒤 값이 이긴다"로 읽는다. 표 행·열이나 상황 행,
    임계값이 실수로 두 번 적히면 뒤의 낮은 값이 앞의 값을 덮어 하한이 내려갈 수 있다.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:  # 해시 불가 키는 여기서 TypeError → 로더가 fail-closed 로 받는다
                raise SeverityConfigError(
                    f"duplicate key {key!r} (line {key_node.start_mark.line + 1})"
                )
            seen.add(key)
        mapping: dict[Any, Any] = super().construct_mapping(node, deep=deep)
        return mapping


def load_severity_table(path: str | None = None) -> SeverityTable:
    """situations.yaml 을 읽어 ``SeverityTable`` 을 만든다. 예외를 던지지 않는다.

    path 가 None 이면 저장소의 ``config/severity/situations.yaml``. 파일이 없거나
    읽을 수 없거나 형식이 틀리면 fail-closed 표(모든 안전 카테고리 CRITICAL)를
    돌려주고 ``load_failed=True``·사유를 노출하며 logging 경고를 남긴다.
    """
    p = Path(path) if path is not None else DEFAULT_SITUATIONS_PATH
    try:
        with p.open("r", encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=_UniqueKeyLoader)  # noqa: S506 — SafeLoader 파생
        cells, situations, disabled = _parse_document(doc)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 조용해지지 않고 fail-closed 로.
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "severity table load failed (fail-closed: every safety category CRITICAL) "
            "path=%s reason=%s",
            p,
            reason,
        )
        return SeverityTable.failed(source=str(p), reason=reason)
    return SeverityTable(cells=cells, situations=situations, disabled=disabled, source=str(p))
