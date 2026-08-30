.PHONY: install test demo smoke lint typecheck cov secrets check hooks clean

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

# 커버리지. 하한(84%)은 2026-08-30 실측 85.7% 기준의 회귀 방지선(여유 1.7%p).
cov:
	$(PY) -m pytest -q --cov --cov-report=term-missing:skip-covered --cov-fail-under=84

secrets:
	./scripts/secret_scan.sh

# 전체 게이트 — CI 와 같은 순서·같은 검사(커버리지 하한·시크릿 스캔 포함).
# 여기서 빠진 게 있으면 '로컬 통과 = CI 통과' 가 거짓이 된다.
check: lint typecheck cov secrets

# 푸시 전 게이트 설치. 적색을 원격까지 들고 가지 않기 위한 로컬 1차 관문.
hooks:
	git config core.hooksPath .githooks
	@echo "pre-push 훅 활성화됨 (건너뛰려면 git push --no-verify)"

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
