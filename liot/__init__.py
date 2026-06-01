"""LIOT — 능동형 스마트홈 AI.

4-Layer 위험도 파이프라인:
  L1 Sensing → L2 Context → L3 Judgment(Gate1 가드레일 + Gate2 LLM) → L4 Execution Safety.

위험도 4단계(심각도 순서): ambient < breath < dusk < ember.

Phase1은 "제안-만" 모델이다. 액추에이터를 실제로 구동하는 코드는 존재하지 않는다.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
