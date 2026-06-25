"""Phase 1 — ExternalDataProvider(인터페이스 + Fake + KMA 스텁).

하드웨어/키 없이 외부 맥락을 결정론적으로 공급하는지, 옵트인 env 가 없으면
Fake 로 폴백하는지, 실패 시 침묵이 아니라 degraded 로 표시하는지 고정한다.
"""

from __future__ import annotations

import pytest

from liot.core.clock import Clock
from liot.core.contracts import ExternalContext
from liot.layers.l2_context.external_provider import (
    ExternalDataProvider,
    FakeExternalProvider,
    KmaExternalProvider,
    build_external_provider,
)


def _clock() -> Clock:
    return Clock(fixed="2026-01-01T08:00:00+00:00")


async def test_fake_provider_deterministic() -> None:
    p = FakeExternalProvider(clock=_clock(), outdoor_temp_c=-3.0, outdoor_aqi=180)
    a = await p.fetch()
    b = await p.fetch()
    assert a == b
    assert a.outdoor_temp_c == -3.0
    assert a.outdoor_aqi == 180
    assert a.degraded is False
    assert a.source == "fake"


async def test_fake_provider_degraded() -> None:
    p = FakeExternalProvider(clock=_clock(), outdoor_temp_c=20.0, outdoor_aqi=10, degraded=True)
    ctx = await p.fetch()
    assert ctx.degraded is True
    # degraded 면 수치는 비운다(보수적 선택 유도).
    assert ctx.outdoor_temp_c is None
    assert ctx.outdoor_aqi is None


async def test_provider_satisfies_protocol() -> None:
    p: ExternalDataProvider = FakeExternalProvider(clock=_clock())
    assert isinstance(p, ExternalDataProvider)
    ctx = await p.fetch()
    assert isinstance(ctx, ExternalContext)


async def test_kma_stub_falls_back_to_fake_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIOT_EXTERNAL_ENABLED", raising=False)
    fallback = FakeExternalProvider(clock=_clock(), outdoor_temp_c=12.0, outdoor_aqi=40)
    p = KmaExternalProvider(clock=_clock(), fallback=fallback)
    ctx = await p.fetch()
    # 옵트인 꺼져 있으면 Fake 폴백값 그대로.
    assert ctx.source == "fake"
    assert ctx.outdoor_temp_c == 12.0
    assert ctx.degraded is False


async def test_kma_stub_degraded_when_enabled_but_unimplemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_EXTERNAL_ENABLED", "true")
    p = KmaExternalProvider(clock=_clock())
    ctx = await p.fetch()
    # 켜졌지만 실 HTTP 미구현 → 침묵 아닌 보수적 degraded.
    assert ctx.source == "kma"
    assert ctx.degraded is True


async def test_build_external_provider_default_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIOT_EXTERNAL_ENABLED", raising=False)
    p = build_external_provider(clock=_clock())
    assert isinstance(p, FakeExternalProvider)


async def test_build_external_provider_optin_is_kma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_EXTERNAL_ENABLED", "1")
    p = build_external_provider(clock=_clock())
    assert isinstance(p, KmaExternalProvider)
