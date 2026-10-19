"""SeverityLevel 값 모델 — 값 집합·라벨·ntfy 세기·승격 전용 고정."""

from __future__ import annotations

import pytest

from liot.core.severity import (
    SEVERITY_SOURCE_ORDER,
    SeverityLevel,
    escalate_severity,
)


def test_value_set_is_exactly_four_ordered_levels() -> None:
    assert [(s.name, int(s)) for s in SeverityLevel] == [
        ("RECORD", 0),
        ("NOTICE", 1),
        ("WARNING", 2),
        ("CRITICAL", 3),
    ]


def test_korean_labels_and_ntfy_priority() -> None:
    assert [s.label_ko for s in SeverityLevel] == ["기록만", "주의", "경고", "위험"]
    assert [s.ntfy_priority for s in SeverityLevel] == ["2", "3", "4", "5"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" critical ", SeverityLevel.CRITICAL), ("Notice", SeverityLevel.NOTICE), (2, SeverityLevel.WARNING)],
)
def test_from_str_normalizes(raw: str | int, expected: SeverityLevel) -> None:
    assert SeverityLevel.from_str(raw) is expected


def test_from_str_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        SeverityLevel.from_str("urgent")


def test_escalate_severity_never_lowers() -> None:
    assert escalate_severity(SeverityLevel.CRITICAL, SeverityLevel.RECORD) is SeverityLevel.CRITICAL
    assert escalate_severity("notice", "warning") is SeverityLevel.WARNING
    assert escalate_severity(1, 1) is SeverityLevel.NOTICE


def test_source_order_is_table_resident_model() -> None:
    assert SEVERITY_SOURCE_ORDER == ("table", "resident", "model")
