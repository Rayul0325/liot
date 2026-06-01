"""디바이스 카탈로그 + 행동 위험도 매핑.

위험도 단위 = ``(capability, action, 파라미터 방향)``.

핵심 규칙:
  * 미지정 action 은 **default-deny** 로 ``RiskTier.EMBER`` (최고 위험).
    카탈로그에 없는 (capability, action) 을 만나면 절대 낮게 보지 않는다.
  * 방향 비대칭:
      - ``lock``  : ``lock`` -> BREATH(숨결) / ``unlock`` -> EMBER(불씨)
      - ``climate.set_temperature`` : 올리기(raise) -> BREATH / 내리기(lower) -> DUSK
      - ``valve`` : ``close`` -> BREATH / ``open`` -> EMBER
      - ``cover`` : ``close`` -> BREATH / ``open`` -> DUSK

이 모듈은 실행 코드가 아니다(위험도 조회만). 'Executor' 라는 이름의
클래스를 만들지 않는다(Phase1 불변식).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from liot.core.risk import RiskTier

__all__ = ["DeviceCatalog", "load_device_catalog", "DEFAULT_CATALOG_PATH"]

# config/device_catalog.yaml 의 기본 경로 (repo 루트 기준).
# 이 파일: liot/layers/l4_execution/device_catalog.py
#   parents[0]=l4_execution [1]=layers [2]=liot(pkg) [3]=repo 루트
DEFAULT_CATALOG_PATH: Path = (
    Path(__file__).resolve().parents[3] / "config" / "device_catalog.yaml"
)

# 방향 추론에 쓰는 토큰. action 이름이나 params 에서 방향을 읽는다.
_INCREASE_TOKENS = ("raise", "increase", "up", "higher", "open", "on")
_DECREASE_TOKENS = ("lower", "decrease", "down", "cool", "close", "off")


class DeviceCatalog:
    """(capability, action, 방향) -> RiskTier 매핑을 제공하는 카탈로그.

    실행은 하지 않는다. 위험도 조회와 디바이스 메타 참조만 담당한다.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw or {}
        self._capabilities: dict[str, Any] = self._raw.get("capabilities", {}) or {}
        # entity_id -> {capability, category}
        self._devices: dict[str, dict[str, Any]] = {}
        for dev in self._raw.get("devices", []) or []:
            eid = dev.get("entity_id")
            if eid:
                self._devices[str(eid)] = dict(dev)

    # ------------------------------------------------------------------
    # 디바이스 메타 조회
    # ------------------------------------------------------------------
    def capability_for(self, entity_id: str) -> str | None:
        """entity_id 의 capability(없으면 None)."""
        dev = self._devices.get(entity_id)
        return None if dev is None else dev.get("capability")

    def category_for(self, entity_id: str) -> str | None:
        """entity_id 의 category(없으면 None)."""
        dev = self._devices.get(entity_id)
        return None if dev is None else dev.get("category")

    def known_entity(self, entity_id: str) -> bool:
        return entity_id in self._devices

    # ------------------------------------------------------------------
    # 위험도 조회 (핵심)
    # ------------------------------------------------------------------
    def risk_for(
        self,
        capability: str,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> RiskTier:
        """(capability, action, 파라미터 방향) 에 대한 위험도.

        규칙:
          1) capability/action 이 카탈로그에 없으면 **default-deny -> EMBER**.
          2) action 항목에 ``directional`` 이 있으면 params/action 으로 방향을
             추론해 increase/decrease 위험도를 적용한다. 추론 실패 시 ``tier``.
          3) 그 외에는 항목의 ``tier`` 를 그대로 사용한다.
        """
        params = params or {}
        cap_map = self._capabilities.get(capability)
        if not isinstance(cap_map, dict):
            return RiskTier.EMBER  # 미지 capability -> default-deny

        action_spec = cap_map.get(action)
        if not isinstance(action_spec, dict):
            return RiskTier.EMBER  # 미지 action -> default-deny

        directional = action_spec.get("directional")
        if isinstance(directional, dict):
            direction = self._infer_direction(action, params, directional)
            if direction == "increase" and "increase" in directional:
                return RiskTier.from_str(directional["increase"])
            if direction == "decrease" and "decrease" in directional:
                return RiskTier.from_str(directional["decrease"])
            # 방향 추론 실패 -> 기본 tier (보수적 기본값).

        base = action_spec.get("tier")
        if base is None:
            return RiskTier.EMBER  # tier 누락 -> default-deny
        return RiskTier.from_str(base)

    @staticmethod
    def _infer_direction(
        action: str,
        params: dict[str, Any],
        directional: dict[str, Any],
    ) -> str | None:
        """params/action 으로 'increase'/'decrease' 방향을 추론한다.

        우선순위:
          1) params 에 ``directional['param']`` 값이 있으면 그 값의
             부호/숫자 또는 명시 방향(on/off, open/close)으로 판단.
          2) action 이름의 토큰(raise/lower/open/close...).
        실패하면 None.
        """
        param_name = directional.get("param")
        if param_name is not None and param_name in params:
            val = params[param_name]
            # 명시적 delta 형(예: {"temperature": "+2"} 또는 {"temperature": -1})
            if isinstance(val, (int, float)):
                if val > 0:
                    return "increase"
                if val < 0:
                    return "decrease"
            elif isinstance(val, str):
                s = val.strip().lower()
                if s.startswith("+") or any(t in s for t in _INCREASE_TOKENS):
                    return "increase"
                if s.startswith("-") or any(t in s for t in _DECREASE_TOKENS):
                    return "decrease"

        # delta/direction 명시 파라미터
        for key in ("direction", "delta", "step"):
            if key in params:
                v = params[key]
                if isinstance(v, (int, float)):
                    if v > 0:
                        return "increase"
                    if v < 0:
                        return "decrease"
                elif isinstance(v, str):
                    s = v.strip().lower()
                    if any(t in s for t in _INCREASE_TOKENS):
                        return "increase"
                    if any(t in s for t in _DECREASE_TOKENS):
                        return "decrease"

        # action 이름에서 추론
        a = action.strip().lower()
        if any(t in a for t in _INCREASE_TOKENS):
            return "increase"
        if any(t in a for t in _DECREASE_TOKENS):
            return "decrease"
        return None


def load_device_catalog(path: str | Path | None = None) -> DeviceCatalog:
    """YAML 카탈로그를 로드해 DeviceCatalog 를 만든다.

    path 미지정 시 ``config/device_catalog.yaml`` 을 사용한다.
    파일이 없으면 빈 카탈로그(모든 action default-deny=EMBER).
    """
    p = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    if not p.exists():
        return DeviceCatalog({})
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raw = {}
    return DeviceCatalog(raw)
