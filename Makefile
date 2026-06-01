.PHONY: install test demo smoke lint clean

VENV ?= .venv
PY := $(VENV)/bin/python

install:
	uv venv --python 3.13
	uv pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q liot tests scripts

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
