"""파급도 표 로더: YAML 리터럴 중복 키는 거부한다(fail-closed).

`yaml.safe_load` 는 같은 매핑의 중복 키를 조용히 "뒤 값이 이긴다"로 읽는다. 표 행/열이나 상황
행이 실수로 두 번 적히면 뒤의 낮은 값이 앞의 값을 덮어 파급도 하한이나 임계값이 내려갈 수 있다 —
"하한은 오르기만 한다"(CLAUDE.md 하드 게이트 2)와 '표를 읽지 못하면 위험'(docs/business-rules.md)에
따라 읽기 실패(= fail-closed)로 다룬다. 정규화 후 중복(대소문자·공백)은 test_severity_loader_gaps.py.
"""

from __future__ import annotations

from pathlib import Path

from liot.core.severity import SeverityLevel, SeverityTable, load_severity_table

_ROWS = {
    "life": "{ minutes: CRITICAL, hour: CRITICAL, later: WARNING }",
    "injury": "{ minutes: CRITICAL, hour: WARNING, later: WARNING }",
    "property": "{ minutes: WARNING, hour: WARNING, later: NOTICE }",
    "inconvenience": "{ minutes: NOTICE, hour: NOTICE, later: RECORD }",
}
_GAS = "  gas: { damage: life, time: minutes }\n"


def _write(tmp_path: Path, *, table_lines: list[str] | None = None, situations: str = _GAS) -> str:
    lines = table_lines if table_lines is not None else [f"  {k}: {v}" for k, v in _ROWS.items()]
    body = "table:\n" + "\n".join(lines) + "\nsituations:\n" + situations
    p = tmp_path / "situations.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _failed(t: SeverityTable, needle: str) -> None:
    """fail-closed 표이고 사유에 needle 이 들어 있다(= 그 가드가 막았다)."""
    assert t.load_failed is True
    assert t.load_error is not None and needle in t.load_error, t.load_error
    res = t.compute("gas", vulnerable_resident=False)
    assert res is not None and res.floor is SeverityLevel.CRITICAL
    assert t.is_abnormal("high_temp", 1.0) is True  # 임계값을 못 읽었으니 수치는 이상


def test_duplicate_table_row_key_in_yaml_is_refused(tmp_path: Path) -> None:
    # 같은 'life' 행이 두 번 — 뒤의 낮은 값이 앞의 CRITICAL 을 덮어 하한이 내려가는 사고.
    lines = [f"  {k}: {v}" for k, v in _ROWS.items()]
    lines.append("  life: { minutes: RECORD, hour: RECORD, later: RECORD }")
    _failed(load_severity_table(_write(tmp_path, table_lines=lines)), "duplicate")


def test_duplicate_table_column_key_in_yaml_is_refused(tmp_path: Path) -> None:
    lines = [f"  {k}: {v}" for k, v in _ROWS.items()]
    lines[0] = "  life: { minutes: CRITICAL, minutes: RECORD, hour: CRITICAL, later: WARNING }"
    _failed(load_severity_table(_write(tmp_path, table_lines=lines)), "duplicate")


def test_duplicate_situation_key_in_yaml_is_refused(tmp_path: Path) -> None:
    sit = _GAS + "  gas: { damage: inconvenience, time: later }\n"
    _failed(load_severity_table(_write(tmp_path, situations=sit)), "duplicate")


def test_duplicate_above_key_in_yaml_is_refused(tmp_path: Path) -> None:
    sit = "  high_temp: { damage: injury, time: hour, abnormal_when: { above: 40, above: 1000 } }\n"
    _failed(load_severity_table(_write(tmp_path, situations=sit)), "duplicate")




def test_unhashable_mapping_key_in_yaml_is_refused(tmp_path: Path) -> None:
    sit = _GAS + "  ? [a, b]\n  : { damage: life, time: minutes }\n"
    t = load_severity_table(_write(tmp_path, situations=sit))
    assert t.load_failed is True
    assert t.compute("gas", vulnerable_resident=False).floor is SeverityLevel.CRITICAL  # type: ignore[union-attr]


def test_distinct_keys_and_repeated_values_are_not_duplicates(tmp_path: Path) -> None:
    # 값이 같은 것은 중복이 아니다 — 같은 키만 중복.
    sit = _GAS + "  smoke: { damage: life, time: minutes }\n"
    t = load_severity_table(_write(tmp_path, situations=sit))
    assert t.load_failed is False, t.load_error
