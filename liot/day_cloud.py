"""하루 시뮬레이터 cloud 모드 부품 — 상한·비용 계량과 실제 모델·기억 묶음. 시뮬레이터 전용.

설계 핵심:
  - **상한 셋**(``config/models.yaml`` 의 ``day_cloud``): 실행당 외부 호출 수, 실행당 추정 비용,
    하루(실제 날짜) 누적 추정 비용. 하루 합계는 ``~/.config/liot/usage.json`` 에 날짜별로 남긴다.
    하나라도 닿으면 계량기가 '넘침'으로 바뀌고, 그 실행의 남은 외부 호출은 전부 가짜로 간다.
  - **호출 전에 자리를 잡는다**: ``allow()`` 가 호출 수를 먼저 올린다(두 스레드가 동시에 마지막
    한 자리를 쓰지 않게). 비용은 호출 뒤 ``record()`` 가 더한다.
  - **사용량 파일을 못 읽거나 못 쓰면 닫힌다**: 실제 돈이 드는 쪽이므로 모르면 가짜로 간다.
  - **가격**: ``prices`` 표(출처 주석 포함)로 추정한다. 표에 없는 모델은 0 원이고 '미상'으로 센다.
    토큰 수를 응답이 주지 않으면 보낸 글자 수로 어림하고 '추정'으로 센다.
  - **묶음(CloudKit)**: Jev 판정기·싼 모델·luna 그림자·기억 클라이언트. 키가 없으면 그 부품만
    빠지고(가짜로 대체) 이유를 ``status`` 에 남긴다. 키 값은 어디에도 남기지 않는다.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from liot.cheap_llm import (
    MAX_TOKEN_COUNT,
    MODELS_PATH,
    PROVIDER_KEY_NAMES,
    CheapLLM,
    ModelConfigError,
    load_cheap_config,
)
from liot.layers.l3_judgment.jev_backend import JevBackend
from liot.layers.l3_judgment.llm_judge import LLMBackend
from liot.luna_shadow import LunaShadow, load_luna_config
from liot.memory_client import MemoryClient
from liot.secrets_env import Secrets

__all__ = [
    "CloudKit",
    "CloudLimits",
    "Price",
    "UsageMeter",
    "build_cloud_kit",
    "load_cloud_settings",
]


@dataclass(frozen=True)
class Price:
    """USD / 1M 토큰. ``estimate`` 는 2차 출처라 '추정'."""

    input: float
    output: float
    estimate: bool = False


@dataclass(frozen=True)
class CloudLimits:
    call_cap_per_run: int
    cost_cap_per_run_usd: float
    cost_cap_per_day_usd: float
    usage_path: Path


def _positive(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ModelConfigError(f"{where} 는 양수여야 합니다.")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ModelConfigError(f"{where} 는 양수여야 합니다.")
    return value


def _non_negative(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ModelConfigError(f"{where} 는 0 이상이어야 합니다.")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ModelConfigError(f"{where} 는 0 이상이어야 합니다.")
    return value


def load_cloud_settings(path: Path = MODELS_PATH) -> tuple[CloudLimits, dict[str, Price]]:
    """``day_cloud`` 상한과 ``prices`` 표를 읽는다."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("day_cloud"), dict):
        raise ModelConfigError("day_cloud 절이 없습니다.")
    section = raw["day_cloud"]
    cap = section.get("call_cap_per_run")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ModelConfigError("call_cap_per_run 은 1 이상 정수여야 합니다.")
    usage = section.get("usage_path")
    if not isinstance(usage, str) or not usage.strip():
        raise ModelConfigError("usage_path 가 없습니다.")
    limits = CloudLimits(
        call_cap_per_run=cap,
        cost_cap_per_run_usd=_positive(section.get("cost_cap_per_run_usd"), "cost_cap_per_run_usd"),
        cost_cap_per_day_usd=_positive(section.get("cost_cap_per_day_usd"), "cost_cap_per_day_usd"),
        usage_path=Path(os.path.expanduser(usage.strip())),
    )
    prices: dict[str, Price] = {}
    table = raw.get("prices") or {}
    if not isinstance(table, dict):
        raise ModelConfigError("prices 는 모델 id → 가격 표여야 합니다.")
    for model, row in table.items():
        if not isinstance(row, dict):
            raise ModelConfigError(f"{model} 가격 형식이 틀렸습니다.")
        prices[str(model)] = Price(
            input=_non_negative(row.get("input"), f"{model}.input"),
            output=_non_negative(row.get("output"), f"{model}.output"),
            estimate=row.get("estimate") is True,
        )
    return limits, prices


def _bounded(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_TOKEN_COUNT else None


_TRIP_KO = {
    "calls": "실행당 외부 호출 상한({cap}회)에 닿아 남은 단계는 가짜 모델로 돕니다.",
    "run_cost": "실행당 추정 비용 상한(${cap})에 닿아 남은 단계는 가짜 모델로 돕니다.",
    "day_cost": "오늘 추정 비용 상한(${cap})에 닿아 남은 단계는 가짜 모델로 돕니다.",
    "usage_unreadable": "사용량 파일을 읽지 못해 비용을 셀 수 없어 가짜 모델로 돕니다.",
    "usage_unwritable": "사용량 파일을 쓰지 못해 비용을 셀 수 없어 가짜 모델로 돕니다.",
}


class UsageMeter:
    """한 실행의 외부 호출 계량기. 여러 스레드(2단계 판정)에서 불러도 된다."""

    def __init__(
        self,
        limits: CloudLimits,
        prices: dict[str, Price],
        *,
        today: Callable[[], str] = lambda: date.today().isoformat(),
    ) -> None:
        self.limits = limits
        self.prices = prices
        self._today = today
        self._lock = threading.Lock()
        self.calls = 0
        self.run_usd = 0.0
        self.tripped: str | None = None
        self.unknown_price_calls = 0
        self.estimated_calls = 0
        # 종류별 집계: 호출 수·지연 합·토큰·비용.
        self.by_kind: dict[str, dict[str, Any]] = {}
        try:
            self._day_book = self._read_book()
        except (OSError, ValueError):
            self._day_book = {}
            self.tripped = "usage_unreadable"

    # ── 사용량 파일 ─────────────────────────────────────────────────────

    def _read_book(self) -> dict[str, dict[str, float]]:
        path = self.limits.usage_path
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("usage book is not an object")
        book: dict[str, dict[str, float]] = {}
        for day, row in data.items():
            if not isinstance(row, dict):
                raise ValueError("usage row is not an object")
            usd = row.get("usd", 0.0)
            calls = row.get("calls", 0)
            if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd):
                raise ValueError("usage usd is not a number")
            if isinstance(calls, bool) or not isinstance(calls, int):
                raise ValueError("usage calls is not an integer")
            book[str(day)] = {"usd": float(usd), "calls": calls}
        return book

    def _write_book(self) -> None:
        path = self.limits.usage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self._day_book, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def day_usd(self) -> float:
        return float(self._day_book.get(self._today(), {}).get("usd", 0.0))

    # ── 계량 ────────────────────────────────────────────────────────────

    def _check(self) -> None:
        if self.tripped is not None:
            return
        if self.calls >= self.limits.call_cap_per_run:
            self.tripped = "calls"
        elif self.run_usd >= self.limits.cost_cap_per_run_usd:
            self.tripped = "run_cost"
        elif self.day_usd() >= self.limits.cost_cap_per_day_usd:
            self.tripped = "day_cost"

    def allow(self) -> bool:
        """외부 호출 한 번의 자리를 잡는다. 상한에 닿았으면 False(그 뒤로도 늘 False)."""
        with self._lock:
            self._check()
            if self.tripped is not None:
                return False
            self.calls += 1
            return True

    def record(
        self,
        kind: str,
        model: str | None,
        *,
        seconds: float | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        sent_chars: int = 0,
    ) -> float:
        """끝난 호출 하나의 비용을 더한다(USD 추정). 하루 합계도 파일에 쓴다."""
        with self._lock:
            estimated = False
            # 응답이 준 토큰 수는 믿을 범위(0~MAX_TOKEN_COUNT)일 때만 쓴다. 큰 정수 × 가격이
            # OverflowError 로 판정 경로를 깨지 않게, 범위 밖은 모르는 값으로 친다.
            tokens_in = _bounded(input_tokens)
            if tokens_in is None:
                tokens_in = max(0, sent_chars) // 2  # 한글 위주 본문: 대략 2글자당 1토큰
                estimated = sent_chars > 0 or input_tokens is not None
            tokens_out = _bounded(output_tokens) or 0
            if output_tokens is not None and _bounded(output_tokens) is None:
                estimated = True
            price = self.prices.get(model or "")
            if price is None:
                usd = 0.0
                if model is not None and kind != "memory":
                    self.unknown_price_calls += 1
            else:
                usd = (tokens_in * price.input + tokens_out * price.output) / 1_000_000
                estimated = estimated or price.estimate
            if estimated:
                self.estimated_calls += 1
            self.run_usd += usd
            row = self.by_kind.setdefault(
                kind, {"calls": 0, "seconds": 0.0, "input_tokens": 0, "output_tokens": 0,
                       "usd": 0.0}
            )
            row["calls"] += 1
            row["seconds"] = round(row["seconds"] + (seconds or 0.0), 4)
            row["input_tokens"] += tokens_in
            row["output_tokens"] += tokens_out
            row["usd"] += usd
            today = self._today()
            day = self._day_book.setdefault(today, {"usd": 0.0, "calls": 0})
            day["usd"] += usd
            day["calls"] = int(day["calls"]) + 1
            try:
                self._write_book()
            except OSError:
                if self.tripped is None:
                    self.tripped = "usage_unwritable"
            self._check()
            return usd

    def trip_reason_ko(self) -> str | None:
        if self.tripped is None:
            return None
        cap: object = {
            "calls": self.limits.call_cap_per_run,
            "run_cost": self.limits.cost_cap_per_run_usd,
            "day_cost": self.limits.cost_cap_per_day_usd,
        }.get(self.tripped, "")
        return _TRIP_KO[self.tripped].format(cap=cap)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            seconds = sum(r["seconds"] for r in self.by_kind.values())
            done = sum(r["calls"] for r in self.by_kind.values())
            return {
                "calls": self.calls,
                "call_cap": self.limits.call_cap_per_run,
                "run_usd": round(self.run_usd, 6),
                "run_cap_usd": self.limits.cost_cap_per_run_usd,
                "day_usd": round(self.day_usd(), 6),
                "day_cap_usd": self.limits.cost_cap_per_day_usd,
                "avg_seconds": None if done == 0 else round(seconds / done, 4),
                "by_kind": {k: {**v, "usd": round(v["usd"], 6)} for k, v in self.by_kind.items()},
                "unknown_price_calls": self.unknown_price_calls,
                "estimated_calls": self.estimated_calls,
                "tripped": self.tripped,
            }


@dataclass
class CloudKit:
    """cloud 모드 부품 묶음. 빠진 부품(None)은 가짜로 대체된다."""

    meter: UsageMeter
    judge: LLMBackend | None = None
    cheap: CheapLLM | None = None
    luna: LunaShadow | None = None
    memory: MemoryClient | None = None
    # 부품별 상태: {"part", "state": "cloud"|"fake", "model", "reason_ko"} — 키 값은 없다.
    status: list[dict[str, Any]] = field(default_factory=list)
    call_timeout_s: float = 12.0


def build_cloud_kit(
    secrets: Secrets,
    *,
    config_path: Path = MODELS_PATH,
    memory: MemoryClient | None = None,
    today: Callable[[], str] = lambda: date.today().isoformat(),
    usage_path: Path | None = None,
) -> CloudKit:
    """비밀 파일의 키로 부품을 만든다. 키가 없거나 비밀 파일이 거부되면 그 부품은 가짜.

    ``usage_path`` 를 주면 설정의 사용량 파일 대신 그곳에 쓴다(시험용).
    """
    limits, prices = load_cloud_settings(config_path)
    if usage_path is not None:
        limits = CloudLimits(limits.call_cap_per_run, limits.cost_cap_per_run_usd,
                             limits.cost_cap_per_day_usd, usage_path)
    kit = CloudKit(meter=UsageMeter(limits, prices, today=today))
    refused = secrets.status != "ok"
    why_missing = secrets.reason_ko or "비밀 파일이 없습니다."

    def note(part: str, state: str, model: str | None, reason_ko: str | None) -> None:
        kit.status.append({"part": part, "state": state, "model": model, "reason_ko": reason_ko})

    jev_key = secrets.get("JEV_API_KEY")
    if jev_key:
        kit.judge = JevBackend(jev_key)
        note("judge", "cloud", kit.judge.model_id, None)
    else:
        note("judge", "fake", "fake-llm-v1",
             why_missing if refused else "JEV_API_KEY 가 없어 가짜 판정기를 씁니다.")

    cheap_cfg = load_cheap_config(config_path)
    cheap_key_name = PROVIDER_KEY_NAMES[cheap_cfg.provider]
    cheap_key = secrets.get(cheap_key_name)
    if cheap_key:
        kit.cheap = CheapLLM(cheap_cfg, cheap_key)
        note("reason", "cloud", f"{cheap_cfg.provider}:{cheap_cfg.model}", None)
        note("resident", "cloud", f"{cheap_cfg.provider}:{cheap_cfg.model}", None)
    else:
        reason = why_missing if refused else f"{cheap_key_name} 가 없어 규칙 문장·성향 표를 씁니다."
        note("reason", "fake", "template", reason)
        note("resident", "fake", "persona_table", reason)

    luna_cfg = load_luna_config(config_path)
    luna = LunaShadow(luna_cfg, secrets.get("OPENAI_API_KEY") or "")
    kit.luna = luna
    note("luna", "cloud" if luna_cfg.mode == "logprobs" else "fake", luna_cfg.model,
         None if luna_cfg.mode == "logprobs" else "luna 미접근 — 설정 방식이 logprobs 가 아닙니다.")

    kit.memory = memory if memory is not None else MemoryClient()
    note("memory", "pending", "memgineering:liot-demo", "실행 시작 때 브레인을 확인합니다.")
    return kit
