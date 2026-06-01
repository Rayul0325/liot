"""프롬프트 레지스트리 + 번들 매니페스트.

재현 가능한 판단(불변식 4)을 위해, LLM 에 보내는 프롬프트와 그것을 둘러싼
정책을 결정론적 식별자로 묶는다:

  - 각 프롬프트 템플릿은 ``prompt_sha`` (정규화된 본문의 sha256)를 갖는다.
  - ``BundleManifest`` 는 prompt_sha + ruleset_version + policy 를 묶어
    결정론적 ``bundle_id`` 를 만든다. 같은 입력이면 항상 같은 bundle_id.

Judgment 에 bundle_id 를 박아두면, 어떤 프롬프트·규칙셋·정책 조합으로
그 판단이 나왔는지 사후에 정확히 재구성할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PromptTemplate",
    "PromptRegistry",
    "BundleManifest",
    "compute_prompt_sha",
    "DEFAULT_JUDGE_PROMPT",
]


def _norm_text(text: str) -> str:
    """프롬프트 본문 정규화: 줄 단위 strip + 통일 개행. 공백 잡음 제거."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").strip().split("\n")]
    return "\n".join(lines)


def compute_prompt_sha(text: str) -> str:
    """정규화된 프롬프트 본문의 sha256(hex)."""
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


# Gate2 판단 프롬프트(결정론 식별 대상). FakeLLM 은 실제로 이 본문을 LLM 에
# 보내지 않지만, prompt_sha 가 번들 식별에 들어가므로 정확히 보존한다.
DEFAULT_JUDGE_PROMPT = """\
당신은 가정 자동화 안전 심사관이다. 후보 행동과 맥락을 받아 위험도를 판정한다.
위험도 단계: ambient < breath < dusk < ember.
규칙(가드레일)이 정한 하한(floor)은 절대 낮출 수 없다. 오직 같거나 더 높게만 판정하라.
JSON 으로만 답하라: {"tier": "<ambient|breath|dusk|ember>", "confidence": <0..1>, "rationale": "<한 줄>"}.
"""


@dataclass(frozen=True)
class PromptTemplate:
    """이름 붙은 프롬프트 템플릿."""

    name: str
    body: str

    @property
    def prompt_sha(self) -> str:
        return compute_prompt_sha(self.body)


@dataclass(frozen=True)
class BundleManifest:
    """프롬프트 + 규칙셋 버전 + 정책을 묶는 재현 매니페스트.

    bundle_id 는 (prompt_sha, ruleset_version, policy) 에서 결정론적으로 파생된다.
    """

    prompt_sha: str
    ruleset_version: str
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def bundle_id(self) -> str:
        canonical = {
            "prompt_sha": self.prompt_sha,
            "ruleset_version": self.ruleset_version,
            "policy": self.policy,
        }
        blob = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return f"bundle:{digest[:16]}"


class PromptRegistry:
    """프롬프트 템플릿 보관소 + 번들 매니페스트 발급기."""

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self.register("judge", DEFAULT_JUDGE_PROMPT)

    def register(self, name: str, body: str) -> PromptTemplate:
        tpl = PromptTemplate(name=name, body=body)
        self._templates[name] = tpl
        return tpl

    def get(self, name: str = "judge") -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"unknown prompt template: {name!r}")
        return self._templates[name]

    def prompt_sha(self, name: str = "judge") -> str:
        return self.get(name).prompt_sha

    def make_bundle(
        self,
        ruleset_version: str,
        *,
        prompt_name: str = "judge",
        policy: dict[str, Any] | None = None,
    ) -> BundleManifest:
        """현재 프롬프트 + 규칙셋 버전 + 정책으로 번들 매니페스트를 만든다."""
        return BundleManifest(
            prompt_sha=self.prompt_sha(prompt_name),
            ruleset_version=ruleset_version,
            policy=dict(policy or {}),
        )
