# experiments — 연구 실험 모음

LIOT 최종 연구 보고서의 근거가 되는 실험들이 여기 있다. 실험마다 폴더가 하나이고, 공통 도구는 `experiments/common/` 에 있다.

## 폴더 구조

```
experiments/
├── __main__.py            ← python -m experiments verify|list
├── common/                ← io.py · plotstyle.py · service_kit.py · invariants.py
└── eN_이름/
    ├── run.py             ← 원자료를 만든다 (--analyze 면 요약·그림만 만든다)
    ├── analyze.py         ← summarize(raw_dir) -> dict  (raw/ 만 읽는다, 결정적)
    ├── raw/               ← 실행 한 번당 JSONL 한 줄 (커밋한다)
    ├── results/summary.json
    ├── figures/
    └── RESULTS.md         ← 결과 해설
```

`e0_smoke/` 가 견본이다. 새 실험은 이 폴더를 복사해서 시작한다.

## 실행 규칙

- 모든 실험은 **자기 체크아웃에서** `PYTHONPATH=$PWD` 를 붙여 실행한다. 그래야 `import liot` 이 그 체크아웃의 코드를 읽는다.
  확인: `PYTHONPATH=$PWD .venv/bin/python -c "import liot; print(liot.__file__)"`
- 예: `PYTHONPATH=$PWD .venv/bin/python -m experiments.e0_smoke.run`, 이어서 `... run --analyze`
- 원자료는 한 건 쓸 때마다 `fsync` 한다(`append_jsonl`). 중간에 끊겨도 `done_keys` 로 이어서 돌린다.
- 시간을 재는 실험은 조용한 기계에서 돌리고, 각 기록에 `run_meta()`(커밋·부하·시각)를 싣는다.

## 검증

```
PYTHONPATH=$PWD .venv/bin/python -m experiments verify [--only eN]
PYTHONPATH=$PWD .venv/bin/python -m experiments list
make exp-verify     # 같은 검증
make exp-lint       # ruff check experiments
```

`verify` 는 각 실험의 `raw/` 에서 요약을 다시 계산해 커밋된 `results/summary.json` 과 비교한다(키 정렬, 소수 6자리 반올림). 한 줄씩 출력한다.

| 출력 | 뜻 |
|---|---|
| `OK eN` | 다시 계산한 값이 같다 |
| `MISMATCH eN <키 경로>` | 처음으로 달라진 항목. 종료 코드가 0 이 아니다 |
| `NO-RAW eN` | 요약은 있는데 `raw/` 가 비어 있다 |
| `SKIP eN (no summary yet)` | 아직 요약이 없다 |
| `0 experiments` | 실험이 하나도 없다(종료 코드 0) |

실험으로 인정되는 조건: `experiments/e*_*/analyze.py` 가 있고 `summarize(raw_dir: Path) -> dict` 를 낸다.

## 클라우드 원자료

클라우드 모델이 돌려준 원자료(`raw/cloud*`)는 **git 에 올리지 않는다**(`.gitignore` 가 막는다). 비용·약관·재현성 때문이다.
따라서 `summarize()` 는 커밋된 원자료만으로도 같은 요약이 나오게 짜거나, 클라우드 부분을 요약에서 분리한다. 클라우드 결과를 보고서에 쓰려면 숫자만 `RESULTS.md` 에 옮기고 원자료 위치를 적는다.

## 공통 도구 요약

| 파일 | 내용 |
|---|---|
| `common/io.py` | `append_jsonl` · `read_jsonl` · `done_keys` · `write_summary` · `run_meta` |
| `common/plotstyle.py` | `setup()` · `save(fig, exp_dir, name)` · `POLICY_COLORS` (정책마다 고정 색) |
| `common/service_kit.py` | `build_service` → 진짜 `LiotService` 조립, 가짜 백엔드·송신기·실행기 |
| `common/invariants.py` | 실행마다 부르는 불변식 검사기(위반 목록, 빈 목록 = 통과) |

## 용어

- **원자료(raw)**: 실행 결과를 가공 없이 한 줄씩 적은 파일.
- **요약(summary)**: 원자료에서 계산한 숫자 모음. 보고서의 숫자는 여기서만 가져온다.
- **불변식**: 어떤 실험 조건에서도 깨지면 안 되는 안전 규칙(바닥은 오르기만 한다 등).
