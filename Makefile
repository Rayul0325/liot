.PHONY: install test demo smoke lint typecheck cov secrets check hooks clean exp-verify exp-lint

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

# 커버리지. 하한은 pyproject.toml [tool.coverage.report] fail_under 한 곳에만 있다.
cov:
	$(PY) -m pytest -q --cov --cov-report=term-missing:skip-covered

secrets:
	./scripts/secret_scan.sh

# 전체 게이트 — CI 와 같은 순서·같은 검사(커버리지 하한·시크릿 스캔 포함).
# 여기서 빠진 게 있으면 '로컬 통과 = CI 통과' 가 거짓이 된다.
# CI 에만 있는 '불변식 게이트' 단계는 여기 따로 두지 않는다 — 같은 단언
# (assert_executor_confined·assert_execution_gated)을 tests/test_invariants.py 가
# pytest 안에서 돌린다. CI 쪽 단계는 이름이 보이는 별도 체크로 남긴 것이다.
check: lint typecheck cov secrets

# 푸시 전 게이트 설치. 적색을 원격까지 들고 가지 않기 위한 로컬 1차 관문.
hooks:
	git config core.hooksPath .githooks
	@echo "pre-push 훅 활성화됨 (건너뛰려면 git push --no-verify)"

demo:
	$(PY) -m liot.demo

# 실제 ollama(이 기계 = 홈 서버의 로컬 HTTP) Gate2 스모크. ollama 미가동이면 degraded 시연.
smoke:
	$(PY) scripts/ollama_smoke.py

# LIOT_LLM_BACKEND=ollama 로 전체 앱을 실제 모델에 붙여 데모.
demo-live:
	LIOT_LLM_BACKEND=ollama $(PY) -m liot.demo

# 연구 실험(experiments/) — make check 밖. 원자료에서 요약을 다시 계산해 커밋된 값과 비교한다.
exp-verify:
	$(PY) -m experiments verify

exp-lint:
	$(PY) -m ruff check experiments

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist
