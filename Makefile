.PHONY: install test demo smoke lint typecheck cov check clean

VENV ?= .venv
PY := $(VENV)/bin/python

install:
	uv venv --python 3.13
	uv pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check liot tests scripts

typecheck:
	$(PY) -m mypy

# 커버리지. 하한(83%)은 2026-08-29 실측 85% 기준의 회귀 방지선.
cov:
	$(PY) -m pytest -q --cov --cov-report=term-missing:skip-covered --cov-fail-under=83

# 전체 게이트 — CI 와 같은 순서.
check: lint typecheck test

demo:
	$(PY) -m liot.demo

# 실제 ollama(gemma via ssh home) Gate2 스모크. home 오프라인이면 degraded 시연.
smoke:
	$(PY) scripts/ollama_smoke.py

# LIOT_LLM_BACKEND=ollama 로 전체 앱을 실제 모델에 붙여 데모.
demo-live:
	LIOT_LLM_BACKEND=ollama $(PY) -m liot.demo

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist
