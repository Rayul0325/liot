# bench/risk — Gate2 결정 모델 벤치(한국어)

`make check` 밖이다(pytest 는 `tests/` 만 모으고, ruff·mypy 도 `liot`·`tests`·`scripts` 만 본다).

| 파일 | 내용 |
|---|---|
| `cases.yaml` | 입력 61건 — 편의 행동 단건·쌍, 안전 상황 8종 × 평온/위험, 주입 16건 |
| `labels.yaml` | 골드 라벨(tier·파급도). **status: draft** — 사용자 검토 전 |
| `models.yaml` | 후보 GGUF 와 모델 카드 대조(형식 적합) |
| `run_bench.py` | 검증·측정·보고서 |
| `raw/<모델>.jsonl` | 케이스×반복마다 한 줄(측정 즉시 기록) · `raw/<모델>.sessions.jsonl` 서버 세션(로드 시간·RSS) |
| `results.md` | 보고서(`report` 로 다시 만든다) · `results_notes.md` 는 그 끝에 붙는 손글씨 메모 |

```bash
V=/Users/rayul/liot/.venv/bin/python
PYTHONPATH=. $V bench/risk/run_bench.py check            # 케이스·라벨 검증 + 하한 표
PYTHONPATH=. $V bench/risk/run_bench.py run --model jevk5-4b-q8
PYTHONPATH=. $V bench/risk/run_bench.py report
```

- 모델 파일은 `~/models/judge/` 에 둔다(저장소에 넣지 않는다). 서버 로그는 `~/models/judge/logs/`.
- `run` 은 18770 번부터 빈 포트를 고르고(`lsof`), 중단돼도 다시 돌리면 남은 케이스만 잰다.
- 판정은 운영과 같은 경로다: Guardrail → 파급도 표(거주자 특성 미사용) → `DecisionBackend`
  → `LLMJudge`. 러너는 모델에 실리는 evidence 키가 넷(context_summary·time_bucket·candidate·
  gate1_floor)뿐인지, 맥락 문장에 거주자 특성 낱말이 없는지 확인한다.
- 라벨을 고친 뒤에는 `check` → `report` 만 다시 돌리면 된다(모델 출력은 raw 에 있다).
- 이 벤치는 추천만 한다. liot 의 기본 모델 설정은 바꾸지 않는다.

## 추가 모드 (기본 동작은 그대로)

- `run --model KEY --context operational`: 맥락 문장 자리에 운영 집계 요약(`ContextBuilder` 가 만드는
  `[day] 1 signals; changed=1; safety=gas; trigger=sensor_reading` 꼴)을 넣는다. 결과는 `raw_operational/`
  에 따로 쌓이고 원본 `raw/` 는 건드리지 않는다. 쌍(calm/danger)은 입력이 같아진다.
- `run --model KEY --latency-only [--context ...] [--reps N]`: 지연 재측정. `raw_latency/<맥락>/` 에 쓰고
  요청마다 `load1_before`·`load1_after` 를 남긴다(조용한 기계에서 돌린다).
- `--raw-dir PATH`(재현 확인용) · `--only 접두사`(스모크용).
- `run --model KEY --context structured`: 운영 집계 요약 뒤에 구조화 관측값 둘(`occupied`, `abnormal_for`)을 덧붙인다. 값은 `observations.yaml`(케이스 문장의 명시된 사실만 옮긴 표). 결과는 `raw_structured/`.
