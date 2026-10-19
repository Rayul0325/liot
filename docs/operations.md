# 운영

## 준비물

- macOS 홈 서버(현재 Mac Studio). Python 3.11 이상(개발·CI 는 3.13), `uv`, `git`.
- 실연동 시: Home Assistant 장기 액세스 토큰, ntfy 서버(기본 `https://ntfy.sh`), 결정 모델을 쓸 때 `llama-server`(Homebrew `llama.cpp`)와 GGUF 파일.
- 모델 파일은 `~/models/judge/` 에 둔다. 저장소에 넣지 않는다.

## 처음 설정

저장소 루트에서 순서대로 실행한다.

```bash
make install                      # uv venv --python 3.13 → uv pip install -e ".[dev]"
make hooks                        # git core.hooksPath=.githooks (pre-push 에 make check 와 같은 검사)
make check                        # ruff → mypy → pytest+커버리지 → 비밀 스캔. 0 이어야 한다
```

- `make install` 이 먼저여야 한다. `make check` 와 훅은 `.venv/bin/python` 을 쓴다.
- 설치는 editable 이다. `.venv` 는 이 체크아웃을 가리키므로, git 워크트리에서 검사할 때는 `PYTHONPATH=$PWD make check VENV=/Users/rayul/liot/.venv` 로 돌린다.
- HA 실연동에는 `websockets` 가 필요하다. dev 설치에 들어 있고, 운영만 따로 설치할 때는 `uv pip install -e ".[ha]"`.

## 개발·검증 명령

| 명령 | 하는 일 |
|---|---|
| `make check` | 커밋 전 관문 전체(CI 와 같은 순서) |
| `make lint` / `make typecheck` / `make cov` / `make secrets` | 하나씩 |
| `make test` | pytest 만(커버리지 없음) |
| `make demo` | CLI 데모(조명·온도·가스·능동 틱) |
| `make smoke` | 이 기기 ollama 로 Gate2 실연결 스모크(gemma 태그 자동 감지, 없으면 degraded 시연) |
| `make demo-live` | 데모를 ollama 백엔드로 |

## 데몬 실행

```bash
export LIOT_DB_PATH=/Users/rayul/liot/liot.db          # 절대 경로. 관제실과 같은 값
export LIOT_NOTIFY_SECRET="$(openssl rand -hex 32)"    # 한 번 만들어 보관한다. 바꾸면 나간 링크가 전부 죽는다
export LIOT_HA_URL=ws://<host>:8123/api/websocket
export LIOT_HA_TOKEN=***
export LIOT_NTFY_URL=https://ntfy.sh
export LIOT_NTFY_TOPIC=<추측하기 어려운 거주자 토픽>
export LIOT_NTFY_FEEDBACK_URL=https://<밖에서 닿는 피드백 주소>
export LIOT_LLM_BACKEND=decision                        # 결정 모델(아래 모델 서버를 먼저 띄운다)
.venv/bin/liot-serve
```

- 시작 로그에서 확인한다: `[HA] N entities mapped.`, `[LIOT] backend=… ntfy=on exec=off(제안-만) tick=300.0s db=…`, `[feedback] http://127.0.0.1:8099/feedback 수신 대기`.
- `LIOT_NOTIFY_SECRET` 이 비었으면 `[feedback] 수신구를 열지 않습니다` 가 찍히고 거주자 버튼·보호자 확인이 동작하지 않는다.
- `LIOT_NTFY_URL` 이 없으면 정제까지만 하고 실제로 보내지 않는다(sanitize-only).
- HA 를 쓰지 않으면 센서 스트림 없이 능동 틱만 돈다.
- 데몬은 HA 연결이 끊겨도 다시 붙지 않는다. 웹소켓이 닫히면 예외로 프로세스가 끝나고, 형식 오류로 스트림이 끝나면 틱만 남은 채 센서를 더는 받지 않는다. 지금은 사람이 다시 띄워야 한다.
- HA 에 센서를 추가·교체했으면 다시 시작한다. 카테고리 맵은 시작 때 한 번만 만든다.
- 종료(Ctrl+C)하면 남은 2단계 판정을 마감+1초 안에 마무리하고 DB 를 닫는다. 열린 사다리는 DB 에 남고 다음 시작 때 다시 무장된다.

## 결정 모델 서버(llama.cpp)

```bash
llama-server -m ~/models/judge/jevk5-9b-v0.3.3-Q8_0.gguf \
  --host 127.0.0.1 --port 18765 -c 4096 -np 1 -ngl 999 --no-webui
export LIOT_LLM_BACKEND=decision
export LIOT_DECISION_URL=http://127.0.0.1:18765     # 기본값과 같다
export LIOT_DECISION_MODEL=jevk5-9b                 # 판단 행에 남을 이름. 서버에 올린 파일과 맞춘다
export LIOT_DECISION_TIMEOUT=1.5                    # 요청 하나당 초
```

- 서버가 없거나 늦으면 판정은 degraded 로 떨어지고 하한 그대로 간다. 첫 알림은 영향을 받지 않는다.
- 기본 모델은 정해지지 않았다. 위 파일은 개발·시험용 후보다.
- 모델 서버가 받는 입력에는 거주자 특성이 없다.

## 관제실(브라우저)

```bash
LIOT_DB_PATH=/Users/rayul/liot/liot.db .venv/bin/python -m liot.demo_server
# http://127.0.0.1:8765/            파이프라인 실험실
# http://127.0.0.1:8765/safety.html 우리 집·거주자·보호자·동의·전달 기록
```

- 안전 설정 화면은 이 기기 브라우저에서만 열린다(`127.0.0.1` 또는 `localhost` 로 접속).
- 거주자·특성·보호자·동의는 데몬과 같은 DB 파일에 바로 쓴다. 데몬은 다음 상황·다음 전달 시점에 읽으므로 재시작이 필요 없다.
- 보호자를 등록하면 비밀 토픽이 **한 번만** 보인다. 그 자리에서 보호자 폰의 ntfy 앱에 `<LIOT_NTFY_URL>/<토픽>` 구독을 넣는다. 다시 볼 방법은 없다(잃어버리면 비활성화 후 새로 등록한다).
- 실험실(`/api/simulate`)은 요청마다 메모리 앱을 새로 만든다. 등록한 센서·기기·규칙은 데몬에 반영되지 않는다. 실험실의 상황 판정에는 파급도가 나오지 않는다.
- 실험실 Gate2 는 기본으로 이 기기 ollama(`gemma3:4b`)를 부른다. 화면 설정에서 LLM 을 끄면 결정론 가짜 판정기를 쓴다.

## 벤치(결정 모델 후보 측정)

```bash
V=/Users/rayul/liot/.venv/bin/python
PYTHONPATH=. $V bench/risk/run_bench.py check                 # 케이스·라벨 검증 + 하한 표
PYTHONPATH=. $V bench/risk/run_bench.py run --model jevk5-4b-q8
PYTHONPATH=. $V bench/risk/run_bench.py report                # results.md 재생성
```

- `run` 은 18770 번부터 빈 포트에 `llama-server` 를 직접 띄운다. 운영 서버(18765)를 내릴 필요는 없다.
- 결과는 케이스마다 `bench/risk/raw/<모델>.jsonl` 에 바로 쓴다. 중단돼도 다시 돌리면 남은 케이스만 잰다. 다시 재려면 그 파일을 옮긴다.
- 지연 측정은 기계가 조용할 때 한다(`uptime` 의 load average 가 낮을 때). 2026-10-19 측정은 load average ≈ 10 이었다.

## 환경변수

### 데몬

| 변수 | 기본값 | 뜻·유효 범위 |
|---|---|---|
| `LIOT_HOME_ID` | `singleton` | 테넌트 id. 데몬과 관제실이 같아야 한다 |
| `LIOT_DB_PATH` | `liot.db`(작업 디렉터리 기준) | SQLite 경로. `:memory:` 가능. 절대 경로를 권한다 |
| `LIOT_HA_URL`, `LIOT_HA_TOKEN` | 없음 | 둘 다 있어야 HA 스트림을 연다 |
| `LIOT_LLM_BACKEND` | 없음 = 가짜 판정기 | `decision` = llama.cpp 결정 모델, `ollama`·`gemma`·`home`·`local` = ollama, 그 밖의 값 = 항상 비가용(degraded) |
| `LIOT_DECISION_URL` | `http://127.0.0.1:18765` | 결정 모델 서버 |
| `LIOT_DECISION_MODEL` | `jevk5-9b` | 판단 행에 남을 모델 이름(서버를 검증하지 않는다) |
| `LIOT_DECISION_TIMEOUT` | `1.5` | 요청 하나당 초. 양의 유한수가 아니면 기동 실패. 2단계 전체 마감 = 이 값 × 4 |
| `LIOT_OLLAMA_MODEL` | `gemma4:e4b` | ollama 모델 태그(qwen3.5 는 쓰지 않는다) |
| `LIOT_OLLAMA_TRANSPORT` | `http` | `ssh` 면 다른 호스트의 ollama 를 ssh 로 부른다 |
| `LIOT_OLLAMA_HTTP_URL` | `http://127.0.0.1:11434` | ollama 주소 |
| `LIOT_OLLAMA_SSH_HOST` | 없음 | transport=ssh 일 때 필수 |
| `LIOT_OLLAMA_TIMEOUT` | `30` | 초 |
| `LIOT_NTFY_URL` | 없음 | 있으면 실제 송신 |
| `LIOT_NTFY_TOPIC` | `liot` | 거주자 알림 토픽. 기본값 그대로 공용 서버에 쓰지 않는다 |
| `LIOT_NTFY_FEEDBACK_URL` | 없음 | 알림 버튼이 가리킬 피드백 주소. 없으면 버튼 없음 |
| `LIOT_NTFY_TOKEN` | 없음 | 보호된 토픽 인증. 거주자·보호자 토픽 모두에 같은 토큰을 쓴다 |
| `LIOT_NTFY_TIMEOUT` | `10` | 송신 하나당 초. 이 시간 동안 이벤트 루프가 멈춘다 |
| `LIOT_NOTIFY_SECRET` | 없음 = 공개 개발 키 | 링크 서명 키. 없으면 피드백 수신구를 열지 않는다 |
| `LIOT_FEEDBACK_ALLOW_DEV_KEY` | 꺼짐 | 개발 키로도 수신구를 연다(로컬 확인 전용) |
| `LIOT_FEEDBACK_HOST`, `LIOT_FEEDBACK_PORT` | `127.0.0.1`, `8099` | 수신구. 포트 0 이면 열지 않는다 |
| `LIOT_TICK_SECONDS` | `300` | 능동 틱 주기. 사다리 루프(5초)와 별개 |
| `LIOT_HOME_TZ` | `Asia/Seoul` | 집 시간대(IANA 이름). 시간대 버킷(야간 정숙 포함)과 능동 예산의 '하루'를 이 지역 시각으로 센다. 모르는 이름이면 기동 실패. 데몬만 읽는다(관제실 실험실은 UTC) |
| `LIOT_EXECUTION_ENABLED` | 꺼짐 | 실행 옵트인. HA 가 연결돼 있을 때만 실행기를 만든다 |
| `LIOT_EVENT_LLM` | 꺼짐 | 가짜 LLM 트리거 감지기를 함께 켠다 |
| `LIOT_EXTERNAL_ENABLED` | 꺼짐 | 외부 데이터 공급자 옵트인. 데몬은 공급자를 연결하지 않아 지금은 효과가 없다 |

### 관제실·스크립트

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `LIOT_LAB_HOST`, `LIOT_LAB_PORT` | `127.0.0.1`, `8765` | 관제실 서버 바인드. 바깥에 열어도 안전 설정 API 는 이 기기에서만 받는다 |
| `LIOT_HA_MAX_EVENTS` | `10` | `scripts/ha_bridge_live.py` 가 처리할 이벤트 수(0 = 무한) |

### 사용자 설정(DB 의 `preferences` 테이블, 키 앞에 `default:`)

| 키 | 기본 | 뜻 |
|---|---|---|
| `charter_enabled` | 참 | 거짓이면 거주자 알림 전면 OFF(보호자 전달은 막지 않는다) |
| `muted_categories` | `[]` | 음소거 카테고리 목록 |
| `muted_entities` | `[]` | 음소거 엔티티 목록 |

이 세 값을 바꾸는 화면이나 API 는 아직 없다.

## 데이터

- DB 는 연결할 때 마이그레이션을 끝까지 적용한다(데몬·관제실 모두). 별도 초기화 명령이 없다. 현재 버전은 3 이다.
- 마이그레이션은 앞으로만 간다. 되돌리려면 파일 백업을 쓴다: 데몬과 관제실을 멈춘 뒤 `liot.db`, `liot.db-wal`, `liot.db-shm` 을 함께 복사한다.
- 거주자 삭제는 관제실에서만 한다(특성 값을 지우고 WAL 을 체크포인트한다). DB 파일을 직접 고쳐 지우면 지운 내용이 페이지에 남을 수 있다.

## 배포

배포 파이프라인은 없다. 이 기기에서 데몬을 직접 띄운다. push 는 소유자 승인 뒤에만 하고, push 하면 GitHub Actions(main/master push·PR)가 `make check` 와 같은 검사에 불변식 게이트를 더해 돈다. push 이벤트로 CI 가 발화하지 않으면 저장소 Actions 설정을 껐다 켜는 것이 지난번 해결책이었다.
