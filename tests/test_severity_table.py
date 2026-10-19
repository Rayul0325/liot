"""파급도 표·상황 매핑·계산기 — 설정 파일 계약값·승격·fail-closed 고정.

표 칸 값과 상황 매핑은 ``config/severity/situations.yaml`` 이 원천이고, 여기서는
그 값이 계약(스펙 인용)과 같은지를 리터럴로 고정한다. 기대값을 설정에서 다시 읽어
비교하면 동어반복이 되므로 테스트 쪽 값은 전부 손으로 적는다.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from liot.core.risk import _reset_safety_extensions_for_tests, register_safety_category
from liot.core.severity import (
    DEFAULT_SITUATIONS_PATH,
    SeverityLevel,
    SeverityResult,
    SeverityTable,
    damage_rank,
    load_severity_table,
    severity_sort_key,
)

C = SeverityLevel.CRITICAL
W = SeverityLevel.WARNING
N = SeverityLevel.NOTICE
R = SeverityLevel.RECORD

# 스펙 인용: 피해 유형 × 방치 시 피해까지 시간 → 파급도(12칸).
TABLE_CONTRACT: list[tuple[str, str, SeverityLevel]] = [
    ("life", "minutes", C),
    ("life", "hour", C),
    ("life", "later", W),
    ("injury", "minutes", C),
    ("injury", "hour", W),
    ("injury", "later", W),
    ("property", "minutes", W),
    ("property", "hour", W),
    ("property", "later", N),
    ("inconvenience", "minutes", N),
    ("inconvenience", "hour", N),
    ("inconvenience", "later", R),
]

# 스펙 인용: 안전 카테고리 8종의 상황 매핑과 그 결과 파급도.
MAPPING_CONTRACT: list[tuple[str, str, str, SeverityLevel]] = [
    ("gas", "life", "minutes", C),
    ("smoke", "life", "minutes", C),
    ("oven", "injury", "hour", W),
    ("high_temp", "injury", "hour", W),
    ("leak", "property", "minutes", W),
    ("water_main", "property", "hour", W),
    ("door", "property", "minutes", W),
    ("lock", "property", "hour", W),
]

BOOLEAN_CATEGORIES = ["gas", "smoke", "leak", "water_main", "oven", "door", "lock"]


@pytest.fixture(autouse=True)
def _isolate_safety_extensions():
    """런타임 안전 카테고리 등록이 다른 테스트로 새지 않게 앞뒤로 비운다."""
    _reset_safety_extensions_for_tests()
    yield
    _reset_safety_extensions_for_tests()


@pytest.fixture
def table() -> SeverityTable:
    """저장소 기본 설정 파일로 로드한 표."""
    t = load_severity_table()
    assert t.load_failed is False, t.load_error
    return t


# ── 기본 경로 ──────────────────────────────────────────────────────────────


def test_default_path_is_config_severity_not_rules() -> None:
    # config/rules/ 의 yaml 은 전부 Gate1 규칙으로 로드되므로 거기 두면 안 된다.
    assert DEFAULT_SITUATIONS_PATH.parts[-3:] == ("config", "severity", "situations.yaml")
    assert "rules" not in DEFAULT_SITUATIONS_PATH.parts
    assert DEFAULT_SITUATIONS_PATH.is_file()


# ── 표 12칸 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("damage", "time", "expected"), TABLE_CONTRACT)
def test_table_cell(table: SeverityTable, damage: str, time: str, expected: SeverityLevel) -> None:
    assert table.cell(damage, time) is expected  # type: ignore[arg-type]


def test_table_has_exactly_twelve_cells(table: SeverityTable) -> None:
    assert len(TABLE_CONTRACT) == 12
    assert len(table.cells()) == 12


# ── 상황 매핑 8행 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("category", "damage", "time", "expected"), MAPPING_CONTRACT)
def test_mapping_row(
    table: SeverityTable, category: str, damage: str, time: str, expected: SeverityLevel
) -> None:
    res = table.compute(category, vulnerable_resident=False)
    assert res == SeverityResult(
        floor=expected, sources=("table",), damage=damage, time_to_harm=time  # type: ignore[arg-type]
    )


def test_category_is_normalized(table: SeverityTable) -> None:
    res = table.compute("  GAS ", vulnerable_resident=False)
    assert res is not None
    assert res.floor is C
    assert res.damage == "life"


def test_reserved_inactivity_row_is_disabled(table: SeverityTable) -> None:
    # 설정에는 예약 행으로 존재하지만 이번 주기 판정에는 쓰지 않는다.
    assert "inactivity" in table.disabled_categories
    assert table.situation("inactivity") is None
    # 비안전 카테고리라 상황이 열리지 않는다.
    assert table.compute("inactivity", vulnerable_resident=False) is None
    # 런타임 안전 등록이 돼도 예약 행(life/hour)을 쓰지 않고 '매핑 없음' 경로로 간다.
    register_safety_category("inactivity")
    res = table.compute("inactivity", vulnerable_resident=False)
    assert res == SeverityResult(floor=C, sources=("table",), damage=None, time_to_harm=None)


# ── 거주자 취약 +1 ────────────────────────────────────────────────────────


def test_vulnerable_resident_raises_one_level(table: SeverityTable) -> None:
    res = table.compute("leak", vulnerable_resident=True)
    assert res == SeverityResult(
        floor=C, sources=("table", "resident"), damage="property", time_to_harm="minutes"
    )


def test_vulnerable_raise_is_capped_at_critical(table: SeverityTable) -> None:
    # 이미 CRITICAL 이면 올라갈 칸이 없다 — 상한 고정, 'resident' 는 기록하지 않는다.
    res = table.compute("gas", vulnerable_resident=True)
    assert res == SeverityResult(
        floor=C, sources=("table",), damage="life", time_to_harm="minutes"
    )


def test_vulnerable_raise_does_not_stack(table: SeverityTable) -> None:
    # 특성이 몇 개든 호출자는 bool 하나만 넘기고, 올림은 한 번(WARNING→CRITICAL)뿐이다.
    base = table.compute("oven", vulnerable_resident=False)
    raised = table.compute("oven", vulnerable_resident=True)
    assert base is not None and raised is not None
    assert int(raised.floor) - int(base.floor) == 1
    assert raised.sources == ("table", "resident")


def test_vulnerable_raise_from_notice_and_record(tmp_path: Path) -> None:
    # 기본 매핑엔 NOTICE/RECORD 행이 없으므로 임시 설정으로 +1 이 정확히 한 칸인지 본다.
    path = _write_config(
        tmp_path,
        situations="""
  gas:   { damage: inconvenience, time: later, abnormal_when: truthy }
  smoke: { damage: inconvenience, time: hour,  abnormal_when: truthy }
""",
    )
    t = load_severity_table(str(path))
    assert t.load_failed is False, t.load_error
    gas = t.compute("gas", vulnerable_resident=True)
    smoke = t.compute("smoke", vulnerable_resident=True)
    assert gas is not None and gas.floor is N and gas.sources == ("table", "resident")
    assert smoke is not None and smoke.floor is W and smoke.sources == ("table", "resident")


# ── 매핑 없음 / 비안전 ────────────────────────────────────────────────────


def test_unmapped_runtime_safety_category_is_critical(table: SeverityTable) -> None:
    register_safety_category("co2")
    res = table.compute("co2", vulnerable_resident=False)
    assert res == SeverityResult(floor=C, sources=("table",), damage=None, time_to_harm=None)
    # 매핑이 없으면 불리언 이상 규칙을 쓴다.
    assert table.is_abnormal("co2", True) is True
    assert table.is_abnormal("co2", False) is False
    assert table.is_abnormal("co2", "weird") is True


def test_non_safety_category_is_none(table: SeverityTable) -> None:
    assert table.compute("light", vulnerable_resident=False) is None
    assert table.compute("light", vulnerable_resident=True) is None
    assert table.compute("", vulnerable_resident=False) is None


# ── fail-closed ───────────────────────────────────────────────────────────


def _assert_fail_closed(t: SeverityTable) -> None:
    assert t.load_failed is True
    assert t.load_error
    for cat in ["gas", "smoke", "door", "leak", "high_temp", "lock", "water_main", "oven"]:
        res = t.compute(cat, vulnerable_resident=False)
        assert res is not None, cat
        assert res.floor is C, cat
        assert res.sources == ("table",)
    assert t.compute("light", vulnerable_resident=False) is None


def test_missing_file_fails_closed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="liot.core.severity"):
        t = load_severity_table(str(tmp_path / "nope.yaml"))
    _assert_fail_closed(t)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_fail_closed_covers_runtime_safety_categories(tmp_path: Path) -> None:
    t = load_severity_table(str(tmp_path / "nope.yaml"))
    register_safety_category("co2")
    res = t.compute("co2", vulnerable_resident=True)
    assert res is not None and res.floor is C


def test_fail_closed_is_abnormal_without_thresholds(tmp_path: Path) -> None:
    # 임계값을 못 읽었으면 수치는 정상 판정 근거가 없다 → 이상(fail-closed).
    t = load_severity_table(str(tmp_path / "nope.yaml"))
    assert t.is_abnormal("high_temp", 20.0) is True
    assert t.is_abnormal("gas", True) is True
    assert t.is_abnormal("gas", False) is False


@pytest.mark.parametrize(
    "body",
    [
        "{{ not: yaml",  # YAML 문법 오류
        "- just\n- a list\n",  # 최상위가 매핑이 아님
        "",  # 빈 파일
        # 표 칸 누락(property.later 없음)
        """
table:
  life:          { minutes: CRITICAL, hour: CRITICAL, later: WARNING }
  injury:        { minutes: CRITICAL, hour: WARNING,  later: WARNING }
  property:      { minutes: WARNING,  hour: WARNING }
  inconvenience: { minutes: NOTICE,   hour: NOTICE,   later: RECORD }
situations:
  gas: { damage: life, time: minutes }
""",
    ],
)
def test_malformed_file_fails_closed(tmp_path: Path, body: str) -> None:
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    _assert_fail_closed(load_severity_table(str(p)))


@pytest.mark.parametrize(
    "situations",
    [
        "  gas: { damage: fire, time: minutes }\n",  # 알 수 없는 피해 유형
        "  gas: { damage: life, time: soon }\n",  # 알 수 없는 시간
        "  gas: { damage: life }\n",  # time 누락
        "  gas: { damage: life, time: minutes, enabled: maybe }\n",  # enabled 가 bool 아님
        "  gas: { damage: life, time: minutes, abnormal_when: sometimes }\n",
        "  high_temp: { damage: injury, time: hour, abnormal_when: { above: hot } }\n",
        "  gas: [life, minutes]\n",  # 행이 매핑이 아님
        "  gas: { damage: life, time: minutes, tme: hour }\n",  # 알 수 없는 키(오타)
        "  gas: { damage: life, time: minutes }\n  GAS: { damage: life, time: hour }\n",  # 중복
    ],
)
def test_malformed_row_fails_closed(tmp_path: Path, situations: str) -> None:
    _assert_fail_closed(load_severity_table(str(_write_config(tmp_path, situations=situations))))


@pytest.mark.parametrize("bad", ["URGENT", "3", "true"])
def test_unknown_severity_in_table_fails_closed(tmp_path: Path, bad: str) -> None:
    # 정수·불리언 표기는 받지 않는다(True 가 NOTICE 로 읽히는 사고 방지).
    path = _write_config(tmp_path, table_override=("life", "minutes", bad))
    _assert_fail_closed(load_severity_table(str(path)))


@pytest.mark.parametrize(
    "body",
    [
        "table: [1, 2]\nsituations: {}\n",  # 표가 매핑이 아님
        "table:\n  life: CRITICAL\nsituations: {}\n",  # 행 누락 + 행이 매핑이 아님
        "situations: {}\n",  # table 키 없음
    ],
)
def test_malformed_table_shape_fails_closed(tmp_path: Path, body: str) -> None:
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    _assert_fail_closed(load_severity_table(str(p)))


def test_table_row_not_mapping_fails_closed(tmp_path: Path) -> None:
    body = _write_config(tmp_path).read_text(encoding="utf-8").replace(
        "  property: { minutes: WARNING, hour: WARNING, later: NOTICE }", "  property: WARNING"
    )
    assert "  property: WARNING" in body
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    _assert_fail_closed(load_severity_table(str(p)))


def test_situations_not_mapping_fails_closed(tmp_path: Path) -> None:
    body = _write_config(tmp_path).read_text(encoding="utf-8").replace(
        "situations:\n  gas: { damage: life, time: minutes }\n", "situations: [gas]\n"
    )
    assert "situations: [gas]" in body
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    _assert_fail_closed(load_severity_table(str(p)))


# ── is_abnormal ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("category", BOOLEAN_CATEGORIES)
def test_boolean_abnormal(table: SeverityTable, category: str) -> None:
    assert table.is_abnormal(category, True) is True
    assert table.is_abnormal(category, False) is False


@pytest.mark.parametrize("category", BOOLEAN_CATEGORIES)
@pytest.mark.parametrize("value", ["unavailable", None, "", 3.0])
def test_boolean_unnormalizable_is_abnormal(
    table: SeverityTable, category: str, value: object
) -> None:
    assert table.is_abnormal(category, value) is True


def test_high_temp_threshold(table: SeverityTable) -> None:
    assert table.is_abnormal("high_temp", 55.1) is True
    assert table.is_abnormal("high_temp", 55.0) is False
    assert table.is_abnormal("high_temp", 55) is False
    assert table.is_abnormal("high_temp", 20.0) is False


@pytest.mark.parametrize("value", ["hot", None, True, False, float("nan")])
def test_high_temp_unnormalizable_is_abnormal(table: SeverityTable, value: object) -> None:
    assert table.is_abnormal("high_temp", value) is True


def test_is_abnormal_normalizes_category(table: SeverityTable) -> None:
    assert table.is_abnormal(" High_Temp ", 56.0) is True
    assert table.is_abnormal(" High_Temp ", 54.0) is False


def test_high_temp_threshold_is_tunable(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        situations="  high_temp: { damage: injury, time: hour, abnormal_when: { above: 40 } }\n",
    )
    t = load_severity_table(str(path))
    assert t.load_failed is False, t.load_error
    assert t.is_abnormal("high_temp", 40.5) is True
    assert t.is_abnormal("high_temp", 40.0) is False


# ── 정렬 키 ──────────────────────────────────────────────────────────────


def test_damage_rank_order() -> None:
    order = ["inconvenience", "property", "life", "injury"]
    assert sorted(order, key=damage_rank) == ["life", "injury", "property", "inconvenience"]  # type: ignore[arg-type]
    assert damage_rank("life") < damage_rank("injury") < damage_rank("property")
    assert damage_rank("property") < damage_rank("inconvenience")


def test_damage_rank_rejects_unknown_and_puts_none_last() -> None:
    assert damage_rank(None) > damage_rank("inconvenience")
    with pytest.raises(ValueError):
        damage_rank("fire")  # type: ignore[arg-type]


def test_severity_sort_key_level_first_then_damage() -> None:
    a = SeverityResult(floor=W, sources=("table",), damage="property", time_to_harm="minutes")
    b = SeverityResult(floor=W, sources=("table",), damage="injury", time_to_harm="hour")
    c = SeverityResult(floor=C, sources=("table",), damage="property", time_to_harm="minutes")
    d = SeverityResult(floor=C, sources=("table",), damage=None, time_to_harm=None)
    e = SeverityResult(floor=C, sources=("table",), damage="life", time_to_harm="minutes")
    got = sorted([a, b, c, d, e], key=severity_sort_key)
    # 높은 파급도 먼저, 같은 단계에선 life > injury > property, 피해 미상은 그 단계 맨 뒤.
    assert got == [e, c, d, b, a]


def test_result_is_frozen() -> None:
    r = SeverityResult(floor=W, sources=("table",), damage="life", time_to_harm="hour")
    with pytest.raises(AttributeError):
        r.floor = C  # type: ignore[misc]


# ── 도우미 ───────────────────────────────────────────────────────────────


_GOOD_TABLE = {
    "life": {"minutes": "CRITICAL", "hour": "CRITICAL", "later": "WARNING"},
    "injury": {"minutes": "CRITICAL", "hour": "WARNING", "later": "WARNING"},
    "property": {"minutes": "WARNING", "hour": "WARNING", "later": "NOTICE"},
    "inconvenience": {"minutes": "NOTICE", "hour": "NOTICE", "later": "RECORD"},
}


def _write_config(
    tmp_path: Path,
    *,
    situations: str = "  gas: { damage: life, time: minutes }\n",
    table_override: tuple[str, str, str] | None = None,
) -> Path:
    """올바른 표 + 주어진 situations 블록으로 임시 설정 파일을 쓴다."""
    cells = {d: dict(row) for d, row in _GOOD_TABLE.items()}
    if table_override is not None:
        d, t, v = table_override
        cells[d][t] = v
    lines = ["table:"]
    for d, row in cells.items():
        inner = ", ".join(f"{t}: {v}" for t, v in row.items())
        lines.append(f"  {d}: {{ {inner} }}")
    lines.append("situations:")
    body = "\n".join(lines) + "\n" + situations
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    return p
