"""E8 뮤테이션 빈틈 — 파급도 표 로더(severity.py 394~425)의 검증 가드.

`experiments/e8_mutation/survivor_classes.json` 의 안전 관련 빈틈 6개(394, 395, 404, 405, 421,
425). 로더는 읽기 실패를 조용히 넘기지 않고 fail-closed 표(모든 안전 카테고리 CRITICAL,
수치 임계값은 이상)로 돌려준다(docs/business-rules.md). 이 파일은 "받아들이는가"를 시험한다 —
예외 종류가 아니라, 잘못된 설정이 정상 표로 로드되지 않는다는 것과 사유가 그 가드의 것이라는 것.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_baseline_good_file_loads(tmp_path: Path) -> None:
    t = load_severity_table(_write(tmp_path))
    assert t.load_failed is False, t.load_error


# ── 표 행(394~395) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        "  fire: { minutes: CRITICAL, hour: CRITICAL, later: CRITICAL }",  # 모르는 행이 남음
        "  Life: { minutes: RECORD, hour: RECORD, later: RECORD }",  # 정규화하면 같은 행(중복)
        "  ' life ': { minutes: RECORD, hour: RECORD, later: RECORD }",  # 공백 변형 중복
    ],
)
def test_extra_or_duplicate_table_row_is_refused(tmp_path: Path, extra: str) -> None:
    lines = [f"  {k}: {v}" for k, v in _ROWS.items()] + [extra]
    _failed(load_severity_table(_write(tmp_path, table_lines=lines)), "'table' rows must be exactly")


def test_missing_table_row_is_refused_by_the_row_guard(tmp_path: Path) -> None:
    lines = [f"  {k}: {v}" for k, v in _ROWS.items() if k != "injury"]
    _failed(load_severity_table(_write(tmp_path, table_lines=lines)), "'table' rows must be exactly")


# ── 표 열(404~405) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "life_row",
    [
        "{ minutes: CRITICAL, hour: CRITICAL, later: WARNING, week: RECORD }",  # 모르는 열
        "{ minutes: CRITICAL, Hour: CRITICAL, hour: RECORD, later: WARNING }",  # 정규화 중복
    ],
)
def test_extra_or_duplicate_table_column_is_refused(tmp_path: Path, life_row: str) -> None:
    lines = [f"  {k}: {v}" for k, v in _ROWS.items()]
    lines[0] = f"  life: {life_row}"
    _failed(load_severity_table(_write(tmp_path, table_lines=lines)), "table.life columns must be exactly")


def test_missing_table_column_is_refused_by_the_column_guard(tmp_path: Path) -> None:
    lines = [f"  {k}: {v}" for k, v in _ROWS.items()]
    lines[2] = "  property: { minutes: WARNING, hour: WARNING }"
    _failed(
        load_severity_table(_write(tmp_path, table_lines=lines)),
        "table.property columns must be exactly",
    )


# ── 임계값(421, 425): 유한한 수만. inf 임계값은 '이상'이 영영 안 나와 안전 알림을 침묵시킨다 ──


@pytest.mark.parametrize("bad", [".inf", "-.inf", ".nan", "true", "false", "'40'", "[40]", "null2"])
def test_non_finite_or_non_numeric_threshold_is_refused(tmp_path: Path, bad: str) -> None:
    sit = f"  high_temp: {{ damage: injury, time: hour, abnormal_when: {{ above: {bad} }} }}\n"
    _failed(load_severity_table(_write(tmp_path, situations=sit)), "must be a finite number")


def test_inf_threshold_would_have_silenced_high_temp(tmp_path: Path) -> None:
    # 거부되지 않았다면 어떤 온도도 이상이 아니다. 거부돼 fail-closed 이므로 이상이다.
    sit = "  high_temp: { damage: injury, time: hour, abnormal_when: { above: .inf } }\n"
    t = load_severity_table(_write(tmp_path, situations=sit))
    assert t.load_failed is True
    assert t.is_abnormal("high_temp", 1000.0) is True


@pytest.mark.parametrize("good", ["40", "40.5", "-5", "0"])
def test_finite_number_threshold_is_accepted(tmp_path: Path, good: str) -> None:
    sit = f"  high_temp: {{ damage: injury, time: hour, abnormal_when: {{ above: {good} }} }}\n"
    t = load_severity_table(_write(tmp_path, situations=sit))
    assert t.load_failed is False, t.load_error
    assert t.is_abnormal("high_temp", float(good) + 1) is True
    assert t.is_abnormal("high_temp", float(good)) is False
