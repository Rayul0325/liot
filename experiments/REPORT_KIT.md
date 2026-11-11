# 보고서 자료집 — 능동형 스마트홈 AI 에이전트 하네스 구조 설계

> 이 문서는 `experiments/kit/build.py` 가 만든다. **직접 고치지 않는다.** 숫자는 `experiments/kit/claims.yaml` 에서, 판정은 `experiments/kit/hypotheses.yaml` 에서, 글은 `experiments/kit/kit_template.md` 에서 온다.
> 숫자는 모두 `experiments/e*/results/summary.json` 의 값과 `check_numbers.py` 로 대조한다. (e6·e9 는 아직 끝나지 않아 '대기'로 표시된다.)

## 0. 이 자료집을 쓰는 법

**소유자의 원칙: 연구는 편향되지 않아야 한다.** 그래서 이 자료집은 보고서 절 순서대로, 모든 주장에 (1) 정확한 숫자와 그 숫자가 나온 `summary.json` 경로, (2) 그림 경로, (3) 가설 번호와 **최종** 판정, (4) 반드시 함께 쓸 한계 문장을 붙인다. 다음 세 가지는 따로 한 절씩 크게 다뤘다 — 각주가 아니다.

- **LIOT 가 지는 곳** (Ⅴ-1절): LIOT 를 대안과 견줬을 때 LIOT 가 더 나쁜 결과.
- **사후 변경과 그 이유** (Ⅴ-3절): 결과를 본 뒤에 판정·기준·표기를 바꾼 것 전부.
- **구조상 결과와 진짜 발견의 구분** (Ⅲ-4절, Ⅴ-2절): 정답이 우리 표·규칙에서 나와 '그렇게 만들었으니 그렇게 나온' 결과는 비교 결론에 쓰지 않는다.

### 판정 말 일곱 가지

| 판정 | 뜻 | 보고서에서 쓰는 방식 |
|---|---|---|
| 지지 | 사전에 등록한 예측이 맞았고, 반증 조건에 걸리지 않았다 | 결론의 근거로 쓸 수 있다(단 가정 범위 안에서) |
| 반증 | 등록한 반증 조건에 걸렸다(예측이 틀렸다) | '우리 예측이 틀렸다'고 그대로 쓴다 |
| 판정 불가 | 효과의 95% 구간이 0 을 포함하거나, 시뮬레이터 점검을 통과하지 못했다 | 결론에 쓰지 않는다. 왜 못 정했는지를 쓴다 |
| 구조상 결과 | 정답이 우리 표·규칙·정의에서 나온다. 실험이 새로 알려 준 것이 아니다 | 비교 결론에 쓰지 않는다. '확인했다'로만 쓴다 |
| 기술 결과 | 기준값 없는 탐색이라 지지/반증을 말하지 않고 숫자만 보고한다 | 숫자와 한계만 쓴다 |
| 읽기에 따라 갈림 | 등록 문구에 빠진 말(몇 칸에서·어느 해석)이 있어 두 읽기를 모두 보고했다 | 두 읽기를 나란히 쓰고 하나를 고르지 않는다 |
| 대기 | 실험이 아직 끝나지 않았다 | 숫자를 쓰지 않는다. '진행 중'이라고만 쓴다 |

### 그림 세 장 (모두 `experiments/kit/figures/`)

| 그림 | 파일 | 보여 주는 것 |
|---|---|---|
| (a) 가설 판정 지도 | `experiments/kit/figures/a_verdict_map.png` | 가설·사이클 한 줄씩, 연구 질문 Q1–Q6 로 묶고 최종 판정 색으로 칠함 |
| (b) LIOT 대 대안 한눈에 | `experiments/kit/figures/b_liot_vs_alternatives.png` | 질문마다 가장 결정에 중요한 비교 하나와 그 효과·퍼짐 |
| (c) 연구 사이클 | `experiments/kit/figures/c_research_timeline.png` | 가설 → 결과 → 판정 → 다음 가설의 흐름(연구 일지 기준) |

## Ⅰ. 서론의 연구 문제

LIOT 는 한 집에서 로컬로 돌아가는 능동형 스마트홈 판단 시스템이다. 사람이 승인해야만 실행하고, 안전은 표와 규칙으로 결정하며, 결정 모델은 위험을 **올리기만** 할 수 있다. 이 연구는 LIOT 의 설계 선택 각각을 **대안과 같은 조건에서 견주고**, LIOT 가 지는 곳과 구조상 정해진 결과를 가려서 보고하는 것을 목표로 했다.

| 연구 질문 | 쉬운 말 | 실험 | 1차 가설(결론의 뼈대) |
|---|---|---|---|
| Q1 안전 | 위험 경보가 늦어지거나 약해지지 않는가? 보호자는 언제 부르는가? | E1 지연 · E2 불변식·보호자 기준 | P1 = H1.5 보호자 호출 기준의 맞교환 |
| Q2 부담 대 피해 | 확인을 얼마나 세게 받아야 하나? 4단계가 모두 같은 확인보다 나은가? | E3 정책 시뮬레이터 | P2 = H2.1 · P3 = H2.4 · P4 = H2.6 |
| Q3 위험 판정 방식 | 조회표가 곱셈 점수·단일 등급·모델 단독보다 나은가? | E4 | (탐색) |
| Q4 적응 | 학습하고 선호 변화를 따라가는가? | E6 (진행 중) | P5 = H4.2 |
| Q5 결정 모델 | 운영에서 모델이 위험을 올릴 수 있는가? 클라우드가 더 강한가? | E5 · E9(진행 중) | (탐색) |
| Q6 사다리·호출·테스트 | 알림 사다리가 명세를 지키는가? 보호자를 동시에 부를까 차례로 부를까? 안전 모듈의 테스트는 오류를 알아채나? | E7 · E8 | (적합성 확인·탐색·품질 확인) |

> **Q6 묶음에 대한 주의**: 사전 등록 `HYPOTHESES.md` 는 H6.1·H6.2·H6.3(사다리·호출·테스트)을 'Q1' 제목 아래에 적어 두었다. 번호(H6.x)와 실험(E7·E8)을 따라 이 자료집은 Q6 으로 묶었다. 보고서에서 등록 원문을 인용할 때는 이 차이를 밝힌다(부록 C).

**Ⅱ(이론적 배경)** 는 이 자료집이 맡지 않는다. 다만 `HYPOTHESES.md` 가 인용한 문헌은 Turan(2026, arXiv:2606.08919)이며, 그 글은 '실현된 안전'이 확인 비율에 대해 역U자라고 했다. 이 연구는 같은 현상을 '피해'로 보아 U자로 불렀다(H2.3, 기술 결과).

## Ⅲ. 방법

### Ⅲ-1. 실험 일곱 개와 규모

| 실험 | 무엇을 | 어떻게 | 주요 가정(전부 '가정'으로 표시) |
|---|---|---|---|
| E1 지연 | 첫 경보가 얼마나 빨리 나가나 | 진짜 서비스 코드, 가짜 송신기, 실시간 측정 | 모델 지연·올림 30%·마감 2초 |
| E2 불변식·보호자 | 하드 게이트가 깨지나, 보호자 호출 기준 | 무작위·경로 강제 퍼징, 결정 수준 시뮬레이션 | 모델 정직도·공격·오경보·미등록 취약 비율 |
| E3 정책 | 확인 방식별 부담·피해·도움 | 30일 시뮬레이터, 400세트 × 씨앗 20 | 거주자 반응 전부(피로·승인·정확도·지연) |
| E4 판정 방식 | 표·곱셈·단일·모델 단독의 성질 | 24가지 입력에 같은 공리 적용, 곱셈은 전수 | 독립 정답 없음 |
| E5 모델 | 운영 맥락에서 올림이 되나 | 벤치 61문항, 로컬 5·클라우드 2 판정자 | 초안 라벨은 정답이 아님 |
| E7 사다리·호출 | 명세 적합, 동시 대 순차 | 가짜 시계 16시나리오, 몬테카를로 108설정 | 보호자 응답 지수분포 |
| E8 뮤테이션 | 안전 모듈의 테스트가 코드 오류를 알아채나 | 코드를 한 곳씩 틀리게 바꾼 변이 742개 전수 | 동치·안전 관련 분류는 한 사람의 판단 |

#### M-01

- **주장**: E1 은 진짜 서비스로 사건 300건 × 칸 20개(구조 5종 × 모델 조건 4종)를 쟀다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 사건 수 | **300 건** | `e1_latency/results/summary.json` → `main > n_incidents` |
  | 칸 수 | **20 칸** | `e1_latency/results/summary.json` → `main > cells` (len) |

- **반드시 함께 쓸 한계 문장**: 모델 지연(0.2·1.5·4초)·올림 확률 30%·마감 2초는 가정이고, 송신기는 가짜이며, 시간은 부하가 높은 한 대의 기계에서 번갈아 잰 상대 비교다. 폰까지 닿는 실제 시간은 재지 않았다.

#### M-02

- **주장**: E2 는 모델 행동 6종 × 600회의 무작위 퍼징과, 칸마다 사건 20,000개 × 씨앗 10개의 보호자 호출 시뮬레이션을 했다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 퍼징 에피소드 | **3,600 회** | `e2_invariants/results/summary.json` → `fuzz > total_episodes` |
  | 사건 수(씨앗 하나) | **20,000 건** | `e2_invariants/results/summary.json` → `h15 > assumptions > events_per_seed` |
  | 씨앗 수 | **10 개** | `e2_invariants/results/summary.json` → `h15 > assumptions > seeds` |

- **반드시 함께 쓸 한계 문장**: '위반 0'은 무작위 퍼징이 만든 경로 안에서의 0이다. 경로를 강제로 채운 퍼징에서는 게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류가 잡혔다. 재시작·동시성의 모든 조합은 덮지 않았다.

#### M-03

- **주장**: E3 는 매개변수 400세트(라틴 초입방) × 씨앗 20개 × 30일을 같은 사건열로 모든 정책에 돌렸다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 세트 수 | **400 세트** | `e3_policy/results/summary.json` → `blocks > main > sets` |
  | 씨앗 수 | **20 개** | `e3_policy/results/summary.json` → `blocks > main > seeds` |
  | 기간 | **30 일** | `e3_policy/results/summary.json` → `blocks > main > days` |

- **반드시 함께 쓸 한계 문장**: 거주자가 알림을 읽고 판단하는 모델(피로·무심코 승인·정확도·응답 지연)은 전부 가정이고 사람 데이터로 맞춘 값이 아니다. 결론은 '가정한 범위 안에서'만 읽는다.

#### M-04

- **주장**: E4 는 24가지 입력에 조회표·곱셈 점수·단일 등급·모델 단독을 대고, 곱셈 점수는 척도·절단점 조합을 전수로 훑었다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 입력 가짓수 | **24 가지** | `e4_scoring/results/summary.json` → `table > n_inputs` |
  | 우세 쌍 | **156 쌍** | `e4_scoring/results/summary.json` → `table > n_dominance_pairs` |
  | 곱셈 척도 수(격자 M3) | **900 종** | `e4_scoring/results/summary.json` → `mult_grid > M3 > n_codings` |
  | 곱셈 설정 수(격자 M3) | **795,588 개** | `e4_scoring/results/summary.json` → `mult_grid > M3 > n_settings` |

- **반드시 함께 쓸 한계 문장**: 위험 등급에는 독립된 정답이 없다. 우리 조회표를 기준점으로 쓴 값('표 기준')은 순환적이고, 곱셈 점수의 '몇 %'는 우리가 고른 척도·절단점 범위에 따라 달라진다.

#### M-05

- **주장**: E5 는 벤치 61문항(주입 제외) × 반복 2회를 판정자 7명(로컬 5·클라우드 2)에게 물었다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 주입 제외 문항 수 | **61 문항** | `e5_models/results/summary.json` → `h51_sentence_vs_operational > jevk5-9b-q8 > rep0 > n_items` |
  | 판정자 수(luna·jev-omni 제외) | **7 명** | `e5_models/results/summary.json` → `c31_subtype > rep0 > judges` (len) |

- **반드시 함께 쓸 한계 문장**: 문항이 작다(주입 제외 61문항·주입 16건·쌍 15개). 판정자들은 같은 문항을 공유해 서로 독립이 아니고, luna 는 크레딧 소진으로 빠졌으며, 로컬 모델은 글자 확률·클라우드는 서로 다른 인터페이스라 같은 측정이 아니다.

#### M-06

- **주장**: E7 은 알림 사다리를 가짜 시계 위에서 16개 시나리오로 확인하고, 보호자 호출 방식을 108개 설정 × 10,000회로 시뮬레이션했다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 사다리 시나리오 | **16 개** | `e7_ladder/results/summary.json` → `conformance > by_driver > event > scenarios` |
  | 대조한 사건 | **162 건** | `e7_ladder/results/summary.json` → `conformance > by_driver > event > events_matched` |
  | 시뮬레이션 설정 수 | **108 개** | `e7_ladder/results/summary.json` → `mc > cells` (len) |
  | 설정마다 시행 | **10,000 회** | `e7_ladder/results/summary.json` → `mc > trials_per_setting` |

- **반드시 함께 쓸 한계 문장**: 가짜 시계와 가짜 송신기이고 시나리오 16개는 명세의 대표 경로일 뿐 모든 조합이 아니다. 송신 지연이 사다리 틱을 밀어내는 효과는 E1 에서 따로 잰다.

#### M-07

- **주장**: E8 은 안전 핵심 모듈 10개의 코드를 AST 로 일부러 틀리게 바꾼 변이 742개(핵심 연산자 450개)를 만들어 테스트가 알아채는지 보았다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 변이 수(전수) | **742 개** | `e8_mutation/results/summary.json` → `overall > all > n` |
  | 핵심 연산자 변이 | **450 개** | `e8_mutation/results/summary.json` → `overall > core > n` |
  | 대상 모듈 | **10 개** | `e8_mutation/results/summary.json` → `modules` (len) |

- **반드시 함께 쓸 한계 문장**: 측정 대상은 U1/U3 변경 전 코드다(변경 뒤에는 다시 돌려야 한다). 변이기는 AST 로 직접 만든 것이고 한 번에 한 곳만 바꾸며 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람이 코드를 읽고 한 판단이고, 변이가 같은 줄에 뭉쳐 있어 구간은 실제보다 좁다. 줄 커버리지만 쟀다(분기 아님).


### Ⅲ-2. 사전 등록과 판정 규칙

- 가설은 **실행 전에** 커밋했다: 가설 v2 는 2026-11-12 01:31:40(KST, 커밋 4403a95) 등록. v1 초안은 커밋하지 않았고 지표 적대 검토에서 FAIL(순환 지표, 걸릴 수 없거나 반드시 걸리는 반증 조건, E3 매개변수 미등록)을 받아 v2 로 고쳤다.
- 판정은 지지·반증·판정 불가 세 가지다. 효과의 95% 구간이 0 을 포함하면 '판정 불가'다. 수천 회 규모라 p 값 대신 **효과 크기와 95% 구간**을 쓴다.
- 부트스트랩 단위는 매개변수 점 안의 씨앗이다. 점들 사이의 변동은 구간이 아니라 분포(사분위, 지역 지도)로 보였고, 씨앗만으로 만든 구간은 '씨앗 잡음'이라고 이름 붙였다.
- 같은 씨앗이면 모든 정책이 같은 사건열을 받는다(공통 난수). 정책 비교는 짝지은 차이로 한다.
- 탐색 가설은 기준이 없으므로 '지지' 대신 기술 결과로만 쓴다. 결과를 본 뒤 만든 기준은 모두 '사후 기준'이라고 표시했다(Ⅴ-3절).

### Ⅲ-3. 공정 비교 장치

- **대안에게 가장 유리한 설정을 준다.** 예: 모델 뒤 발송 대안은 모델이 예외를 내면 즉시 보낸다. 짧은 마감 대안은 0.25·0.5·1.0초를 모두 재서 가장 좋은 값을 대표로 쓴다. 이진 승인은 '사후 최적 목록'(이진에게 가장 유리하게 고른 목록)으로 결론을 낸다.
- 처음에 대안이 불리하게 정의된 곳은 사이클 2 에서 고쳐 다시 쟀다(하한을 아는 모델 뒤 발송 C2.7, 같은 능동 상한을 모두에게 씌운 비교 C2.2, 방관 효과를 동시 호출 수로 계산한 C2.10).
- 시뮬레이터가 시뮬레이터 자체를 점검한다(H2.2, 해석식 대조). 그리고 그 점검을 LIOT 실제가 통과하지 못했을 때 등록 규칙대로 관련 결론을 '판정 불가'로 내렸다.

### Ⅲ-4. 구조상 결과와 진짜 발견을 가르는 규칙

> **질문 하나로 가른다: "이 숫자의 정답이 우리 표·규칙·정의에서 나오는가?"** 그렇다면 그것은 우리가 '그렇게 만든' 결과이므로 비교 결론에 쓰지 않는다.

| 구조상 결과로 둔 것 | 이유 |
|---|---|
| H1.1 지연 차이 | 대안이 '기다린다'고 정의돼 있으니 기다린 만큼 늦다 |
| C2.7 위험 하한에서 같음 | 하한을 아는 대안이 위험 하한에서 운영 경로를 그대로 타도록 코드로 만들었다 |
| H1.4 하드 게이트 위반 0 | 우리 규칙을 우리 코드가 지키는지 보는 검사다 |
| H1.5 미등록 취약 0% 칸·취약성 못 보는 모드 | 표가 틀릴 수 없거나 모델에 정보가 없다 |
| H2.1·H2.4·H2.6 의 liot_real 부분 | 해로운 제안의 96%가 LIOT 단계의 위쪽에 놓이게 우리가 생성기를 썼다 |
| H5.1 운영 맥락의 올림 감소 | 운영 요약에는 상황 내용이 없어 평온·위험 문장이 같은 입력이 된다 |

### Ⅲ-5. 연구 사이클

연구는 세 사이클로 진행됐다. 사이클 1 은 등록한 가설의 시험, 사이클 2 는 사이클 1 의 반증·약한 결과에 대한 원인 가설·대안 공정화·검토 지적 반영, 사이클 3 은 C2.5 반증으로 결론이 바뀐 뒤의 추가 시험(C3.1)이다. 그림 (c) 가 시간순 흐름이다(`experiments/kit/figures/c_research_timeline.png`, 출처 `experiments/RESEARCH_LOG.md`).

## Ⅳ. 결과

### 가설별 최종 판정 한눈에 (그림 a)

그림 `experiments/kit/figures/a_verdict_map.png`. 아래 표의 '최근 문구'는 각 실험 RESULTS.md 의 판정 표(또는 사후 변경 기록 중 더 나중 것)의 표현이다.

**Q1 안전 (E1·E2)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H1.1 | 표 우선 대 모델 뒤 발송 | **구조상 결과** | 구조상 결과('지지' 아님) | E1-01 |
| H1.1c | 2단계 불일치율 | **기술 결과** | 기술 통계(판정 없음) | E1-03 |
| H1.2 전 | 편의 판정이 가스 경보를 막음(U1 전) | **기술 결과** | 재현(판정 아님) — 이미 알려진 결함의 재현이라 반증 조건이 걸릴 수 없었다 | E1-02 |
| H1.2 후 | 편의 판정이 가스 경보를 막음(U1 후) | **지지** | 지지(세 T 모두 충족) | E1-02 |
| H1.3 | 느린 송신이 다음 경보를 늦춤 | **기술 결과** | 특성 기록 | E1-04 |
| H1.4 | 하드 게이트 불변식 | **구조상 결과** | 무작위 퍼징 범위 안에서 0 — 알려진 게이트 1 충돌 1종 별도 (구조상 결과, 비교 결론 아님) | E2-01, E2-02 |
| H1.5 (P1) | 보호자 호출 기준의 맞교환 | **기술 결과** | 단정 없음 — c* 지도, 모드별 | E2-04, E2-06 |
| C2.7 | 하한을 아는 모델 뒤 발송 | **구조상 결과** | 구조상 결과(공정화 확인) — '지지'가 아니다 | E1-05 |
| C2.8 | 관찰 필드가 드러내는 위험 | **읽기에 따라 갈림** | 읽기 (i) 반증 · 읽기 (ii) 반증 아님 — 결론은 읽기에 달렸다 | E2-05 |
| C2.9 | 경로 할당량 퍼징 | **지지** | 지지 — 게이트 1 충돌 두 종류(알려진 억제 + 새 첫 송신 실패 지연) | E2-03 |

**Q2 부담 대 피해 (E3)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H2.1 (P2) | 피로가 있을 때 4단계 대 모두 확인 | **판정 불가** | 판정 불가 · 구조상 결과 (liot_real 이 H2.2 점검을 통과하지 못함) | E3-01, E3-02, E3-03 |
| H2.2 | 시뮬레이터 점검(피로 0) | **판정 불가** | liot_full 약한 통과 / liot_real 불통과 → liot_real 관련 E3 결론 판정 불가 | E3-04 |
| H2.3 | 확인 비율의 U자 | **기술 결과** | 기술 결과(판정 없음) | E3-14 |
| H2.4 (P3) | 오분류 늘면 이점이 사라지는 경계 | **판정 불가** | 판정 불가 · 구조상 결과(liot_real 포함) | E3-05 |
| H2.5 | 이진 승인 대 4단계 | **기술 결과** | 기술 결과 | E3-01 |
| H2.6 막은 피해 | 능동 상한이 막은 피해(P4) | **판정 불가** | '막은 피해' = 판정 불가 · 구조상 결과 | E3-06, E3-13 |
| H2.6 잃은 도움 | 능동 상한이 잃은 도움(P4) | **기술 결과** | '잃은 도움' = 기술 값(구성 의존) | E3-06 |
| H2.7 | 자동 실행 + 되돌리기 | **기술 결과** | 기술 결과 | E3-14 |
| C2.1 | 피해 이점은 견본 몰림 탓 | **반증** | 반증(해로움을 낮은 단계로 옮겨도 이점이 남음 — 단 잔잔 쪽 해로움이 약 20% 되면 사라짐) | E3-07 |
| C2.2 | 같은 침묵 비율이면 같은 맞교환 | **반증** | 반증(이진·모두 자동 기준) — 모두 명시 확인+상한 기준은 대체로 성립; 우위는 구조상 결과 | E3-08, E3-02 |
| C2.3 | 잔잔 자동 실행이 열세의 원인 | **지지** | 지지(알림만으로 바꾸면 피해 열세 사라지고 도움 감소) | E3-09 |
| C2.4 | 점수가 얼마나 정확해야 θ 정책이 이기나 | **기술 결과** | 기술 결과(기준값 없음) | E3-12 |
| C2.11a | 해로움이 잔잔에 놓이면 | **지지** | 지지(예측대로) — 단 잔잔 50% 이상 구간의 손실은 정의에 가까운 구조상 결과이고 정보는 교차점(잔잔 약 16~20%) | E3-10 |
| C2.11b m=10% | 탭 한 번이 더 쉽게 승인(m=10%) | **지지** | 지지(m=10%) — 단 조건이 기준선에서 이미 충족, 발견은 2.0배의 한계 효과 +1.5%p | E3-11 |
| C2.11b m=0 | 탭 한 번이 더 쉽게 승인(m=0) | **판정 불가** | m=0 은 판정 불가(점추정은 과반, 유의 기준은 아님) | E3-11 |

**Q3 위험 판정 방식 (E4)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H3.1 | 방식별 성질(표·곱셈·단일·모델) | **기술 결과** | 판정 대상 아님 — 관찰만 | E4-01, E4-02, E4-03 |
| H3.3 | 모델 단독의 방향성 | **기술 결과** | 관찰: 방향이 낮추는 쪽 | E4-04 |

**Q4 적응 (E6)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H4.1 | 학습의 효과 | **대기** | e6 진행 중 | E6-00 |
| H4.2 (P5) | 선호가 바뀌면 따라가나 | **대기** | e6 진행 중 | E6-00 |
| H4.3 | 확인 방식과 수락 | **대기** | e6 진행 중 | E6-00 |

**Q5 결정 모델 (E5·E9)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H5.1 | 운영 맥락에서 올림이 줄어드나 | **구조상 결과** | 구조상 결과 (지지/반증 아님) | E5-01 |
| H5.2 | 클라우드가 주입에 강한가 | **판정 불가** | 판정 불가(구간 겹침 | E5-02 |
| H5.3 | 판정자끼리 갈리나 | **기술 결과** | 기술 결과(탐색 | E5-03 |
| ADD-5 | 문장을 보는 모델이 표보다 덜 놓치나 | **기술 결과** | 관찰 — 문장 맥락에서만 표보다 덜 놓침 | E5-04 |
| C2.5 | 관측값 두 개로 올림이 돌아오나 | **반증** | 반증 (돌아온 몫 0.014 < 0.5) | E5-05 |
| C3.1 | 상황 종류 코드로 올림이 돌아오나 | **반증** | 반증 (돌아온 몫 0.113 < 0.5; 규칙 조회가 모델과 같거나 나음) | E5-06 |
| H5.4 | 실제 종단 실행에서 Jev 와 luna | **대기** | e9 진행 중 | E9-00 |

**Q6 사다리·호출 (E7·E8)**

| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |
|---|---|---|---|---|
| H6.1 | 사다리 명세 적합 | **반증** | 반증(등록 문구대로 — 5초 틱 해상도) | E7-01 |
| H6.2 | 동시 대 순차 호출 | **기술 결과** | 기술 결과(판정 없음) | E7-02 |
| C2.10 | 방관 효과를 동시 호출 수로 계산 | **읽기에 따라 갈림** | 해석 N 은 반증 조건에 걸리고 해석 동시 호출 수는 지지 — 결론은 해석에 달렸다 | E7-03 |
| H6.3 | 안전 모듈의 뮤테이션 점수 | **지지** | 지지(반증 실패) — 커버리지 95% 이상인데 10개 모듈 중 7개가 90% 미만(개수는 가정에 따라 7→4→2); 안전 관련 빈틈 15곳. 등록 때는 품질 확인이라 연구 결론에서 제외 | E8-01, E8-02, E8-03 |


### LIOT 대 대안 한눈에 (그림 b)

그림 `experiments/kit/figures/b_liot_vs_alternatives.png`. 질문마다 가장 결정에 중요한 비교 하나만 골랐다: E1 하한을 아는 대안의 지연 대 푸시, E2 운영점·AUC 0.8 의 c\*, E3 같은 상한 비교, E4 동점 손실, E5 올림 회복(규칙 조회 포함), E7 순차 대 동시(두 읽기), E8 모듈별 뮤테이션 점수. 파랑은 LIOT 쪽·유리, 붉은색은 LIOT 에 불리, 회색은 대안·참고다.

### Ⅳ-Q1. 안전 — 경보와 보호자 호출 (E1·E2)

#### E1-01 · H1.1

- **최종 판정**: 구조상 결과
- **주장**: LIOT(표 우선 발송)의 첫 경보는 모델 조건과 무관하게 수 밀리초다. 모델을 기다리는 대안은 기다린 시간만큼 늦다. 이 차이는 '대안이 기다리도록 정의됐다'는 데서 나오므로 비교 결론에 쓰지 않는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | LIOT 첫 경보 지연 중앙값(모델 정상) | **2.7 ms** | `e1_latency/results/summary.json` → `main > cells > liot/normal > median` |
  | LIOT 첫 경보 지연 p95(모델 정상) | **4.7 ms** | `e1_latency/results/summary.json` → `main > cells > liot/normal > p95` |
  | 모델 뒤 발송 − LIOT 중앙값(모델 정상 0.2초) | **+205.4 ms** | `e1_latency/results/summary.json` → `main > delta_vs_liot > wait_for_model/normal > median > diff` |
  | 모델 뒤 발송 − LIOT 중앙값(모델 느림 1.5초) | **+1506.6 ms** | `e1_latency/results/summary.json` → `main > delta_vs_liot > wait_for_model/slow > median > diff` |
  | 모델 뒤 발송 − LIOT 중앙값(마감 초과 4초) | **+2002.0 ms** | `e1_latency/results/summary.json` → `main > delta_vs_liot > wait_for_model/timeout > median > diff` |
  | 모델 뒤 발송 − LIOT 중앙값(모델 예외) | **+0.5 ms** | `e1_latency/results/summary.json` → `main > delta_vs_liot > wait_for_model/raising > median > diff` |

- **그림**: `experiments/e1_latency/figures/latency_by_arm_cond.png`
- **반드시 함께 쓸 한계 문장**: 비교 대안은 운영 서비스를 상속한 얇은 래퍼이고, 지연의 차이는 '대안이 모델을 기다리도록 정의됐다'는 데서 나온다. 모델 응답의 내용은 재생하지 않았고 올림 여부는 가정이다.

#### E1-02 · H1.2

- **최종 판정**: 기술 결과(U1 전 재현) → 지지(U1 후)
- **주장**: 편의 판정이 이벤트 루프를 막던 때(U1 전)는 가스 첫 경보가 대조군보다 편의 판정 시간만큼 늦었다(이미 알려진 결함의 재현). 편의 판정을 워커 스레드로 보낸 뒤(U1 후)에는 같은 시간대 대조군과의 p95 차이가 사전 기준 0.1초 안에 들어온다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | U1 전 p95 차이(편의 판정 0.5초) | **0.480 초** | `e1_latency/results/summary.json` → `h12 > before > 0.5 > pooled > p95_minus_control_p95` |
  | U1 전 p95 차이(1.5초) | **1.429 초** | `e1_latency/results/summary.json` → `h12 > before > 1.5 > pooled > p95_minus_control_p95` |
  | U1 전 p95 차이(3.0초) | **2.853 초** | `e1_latency/results/summary.json` → `h12 > before > 3.0 > pooled > p95_minus_control_p95` |
  | U1 후 p95 차이(0.5초) | **+6.2 ms** | `e1_latency/results/summary.json` → `h12 > after > 0.5 > pooled > p95_minus_control_p95` |
  | U1 후 p95 차이(1.5초) | **+6.5 ms** | `e1_latency/results/summary.json` → `h12 > after > 1.5 > pooled > p95_minus_control_p95` |
  | U1 후 p95 차이(3.0초) | **+5.1 ms** | `e1_latency/results/summary.json` → `h12 > after > 3.0 > pooled > p95_minus_control_p95` |
  | U1 후 사전 기준(≤ 0.1초) 충족(1.5초) | **예** | `e1_latency/results/summary.json` → `h12 > after > 1.5 > pooled > after_criterion_met_p95_diff_le_0.1s` |

- **그림**: `experiments/e1_latency/figures/h12_comfort_blocks_gas.png`
- **반드시 함께 쓸 한계 문장**: 모델 지연(0.2·1.5·4초)·올림 확률 30%·마감 2초는 가정이고, 송신기는 가짜이며, 시간은 부하가 높은 한 대의 기계에서 번갈아 잰 상대 비교다. 폰까지 닿는 실제 시간은 재지 않았다.

#### E1-03 · H1.1c

- **최종 판정**: 기술 결과
- **주장**: 경고 하한 사건에서 LIOT 는 첫 알림을 '경고'로 보냈다가 모델이 올리면 두 번째 푸시를 보낸다. 위험 하한 사건에서는 이 일이 없다. 합성 17.3%는 올림 30% 가정이 만든 숫자이고, 벤치 원시 답 재생은 16.7%다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 합성 2단계 불일치율(LIOT) | **17.3%** | `e1_latency/results/summary.json` → `main > stage2_change > liot/normal > all > rate` |
  | 그중 경고 하한 사건 | **25.9%** | `e1_latency/results/summary.json` → `main > stage2_change > liot/normal > floor_warning > rate` |
  | 그중 위험 하한 사건 | **0.0%** | `e1_latency/results/summary.json` → `main > stage2_change > liot/normal > floor_critical > rate` |
  | 벤치 재생 불일치율(모델 6종 합) | **16.7%** | `e1_latency/results/summary.json` → `replay > pooled > mismatch > rate` |
  | 벤치 재생 95% 구간 하한 | **11.5%** | `e1_latency/results/summary.json` → `replay > pooled > mismatch > lo` |
  | 벤치 재생 95% 구간 상한 | **21.9%** | `e1_latency/results/summary.json` → `replay > pooled > mismatch > hi` |

- **그림**: `experiments/e1_latency/figures/h11c_replay_mismatch.png`
- **반드시 함께 쓸 한계 문장**: 벤치 재생은 상황 사례 32건뿐이고 맥락 문장이 운영과 다르다. 모델은 정답이 아니므로 '불일치'는 '오류'가 아니다.

#### E1-04 · H1.3

- **최종 판정**: 기술 결과(특성 기록)
- **주장**: 알림 송신(ntfy)이 느리면 그 사이에 들어온 다음 안전 경보가 송신 횟수 × 송신 지연만큼 늦는다. 보호자 3명·송신 10초이면 두 번째 안전 신호의 첫 알림이 약 30초 늦는다. 판정은 없는 특성 기록이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 첫 송신 중 도착(지연 10초) 중앙값 | **9.907 초** | `e1_latency/results/summary.json` → `h13 > first_send/10.0/0 > median` |
  | 사다리 중 | **19.918 초** | `e1_latency/results/summary.json` → `h13 > ladder/10.0/1 > median` |
  | 사다리 중 | **29.925 초** | `e1_latency/results/summary.json` → `h13 > ladder/10.0/3 > median` |
  | 송신 지연 0초일 때(보호자 3명) | **3.0 ms** | `e1_latency/results/summary.json` → `h13 > ladder/0.0/3 > median` |

- **그림**: `experiments/e1_latency/figures/h13_send_delay.png`
- **반드시 함께 쓸 한계 문장**: 모델 지연(0.2·1.5·4초)·올림 확률 30%·마감 2초는 가정이고, 송신기는 가짜이며, 시간은 부하가 높은 한 대의 기계에서 번갈아 잰 상대 비교다. 폰까지 닿는 실제 시간은 재지 않았다.

#### E1-05 · C2.7

- **최종 판정**: 구조상 결과(공정화 확인) + 기술 결과(경고 하한)
- **주장**: 하한을 아는 대안(하한이 위험이면 기다리지 않음)은 위험 하한 사건에서 LIOT 와 지연·푸시가 같다 — 코드 구성상 반드시 같아지는 공정화 확인이지 시험이 아니다. 경고 하한 사건에서만 대안이 약 1초 늦게 보내는 대신 푸시를 한 번으로 줄인다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 위험 하한 — 하한을 아는 대안 − LIOT 중앙값 | **-0.1 ms** | `e1_latency/results/summary.json` → `c27 > by_floor > CRITICAL > delta_vs_liot > floor_aware > median > diff` |
  | 위험 하한 — 항상 기다리는 대안 − LIOT 중앙값 | **+1011.9 ms** | `e1_latency/results/summary.json` → `c27 > by_floor > CRITICAL > delta_vs_liot > wait_for_model > median > diff` |
  | 경고 하한 — 하한을 아는 대안 − LIOT 중앙값 | **+955.0 ms** | `e1_latency/results/summary.json` → `c27 > by_floor > WARNING > delta_vs_liot > floor_aware > median > diff` |
  | 경고 하한 — 대안의 사건당 푸시 차이 | **-0.267 회** | `e1_latency/results/summary.json` → `c27 > by_floor > WARNING > delta_vs_liot > floor_aware > pushes_mean_diff` |
  | 경고 하한 — LIOT 의 두 번째 푸시 비율 | **26.7%** | `e1_latency/results/summary.json` → `c27 > by_floor > WARNING > cells > liot > mismatch_share > rate` |
  | 벤치 모델 판정 시간 중앙값 | **0.957 초** | `e1_latency/results/summary.json` → `c27 > model_seconds > median` |

- **그림**: `experiments/e1_latency/figures/latency_by_arm_cond.png`
- **반드시 함께 쓸 한계 문장**: 비교 대안은 운영 서비스를 상속한 얇은 래퍼이고, 지연의 차이는 '대안이 모델을 기다리도록 정의됐다'는 데서 나온다. 모델 응답의 내용은 재생하지 않았고 올림 여부는 가정이다.

#### E2-01 · H1.4

- **최종 판정**: 구조상 결과
- **주장**: 무작위 퍼징 3,600회(모델 행동 6종) 안에서 하드 게이트 위반은 0건이다. 이것은 '우리 규칙을 우리 코드가 지킨다'는 점검이고 다른 구조와의 비교가 아니다. 알려진 게이트 1 충돌 두 종류는 아래 E2-03 과 지는 곳 절에 따로 적는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 퍼징 에피소드 | **3,600 회** | `e2_invariants/results/summary.json` → `fuzz > total_episodes` |
  | 하드 게이트 위반(퍼징 안) | **0 건** | `e2_invariants/results/summary.json` → `fuzz > violations_total` |
  | 판단 행(모두 최종 ≥ 하한) | **25,634 행** | `e2_invariants/results/summary.json` → `fuzz > by_behavior > * > stress > judgment_rows` (sum) |
  | 새 상황 | **11,628 건** | `e2_invariants/results/summary.json` → `fuzz > by_behavior > * > stress > new_situations` (sum) |
  | 푸시 | **39,959 건** | `e2_invariants/results/summary.json` → `fuzz > by_behavior > * > stress > pushes` (sum) |
  | 여는 방향 승인·실행 시도(전부 거부) | **1,473 건** | `e2_invariants/results/summary.json` → `fuzz > by_behavior > * > stress > exec_opening_attempts` (sum) |
  | 하한 아래라 버려진 모델 답(구조상) | **3,716 건** | `e2_invariants/results/summary.json` → `fuzz > by_behavior > * > stress > answers_below_floor_discarded` (sum) |

- **그림**: `experiments/e2_invariants/figures/h15_false_and_miss_curves.png`
- **반드시 함께 쓸 한계 문장**: '위반 0'은 무작위 퍼징이 만든 경로 안에서의 0이다. 경로를 강제로 채운 퍼징에서는 게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류가 잡혔다. 재시작·동시성의 모든 조합은 덮지 않았다.

#### E2-02 · H1.4(별도 기록)

- **최종 판정**: 기술 결과
- **주장**: 위험 라벨이 문 열림에 붙으면 '이 집에 취약 거주자가 있다'는 사실 1비트가 폰 알림에 드러난다(추론 누출). 송신 낱말 검사로는 잡히지 않는다. 하드 게이트 위반으로 세지는 않았지만 설계가 알고 있어야 하는 누출이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 취약 거주자 미등록 집 | **경고** | `e2_invariants/results/summary.json` → `inference_leak_door_label > plain > resident_labels > 0` |
  | 취약 거주자 등록 집 | **위험** | `e2_invariants/results/summary.json` → `inference_leak_door_label > registered_vulnerable > resident_labels > 0` |
  | 취약 거주자 등록 집 | **위험** | `e2_invariants/results/summary.json` → `inference_leak_door_label > registered_vulnerable > guardian_labels > 0` |

- **반드시 함께 쓸 한계 문장**: '위반 0'은 무작위 퍼징이 만든 경로 안에서의 0이다. 경로를 강제로 채운 퍼징에서는 게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류가 잡혔다. 재시작·동시성의 모든 조합은 덮지 않았다.

#### E2-03 · C2.9

- **최종 판정**: 지지(+ 새 결함 발견)
- **주장**: 경로를 강제로 채운 퍼징에서 게이트 1 충돌 두 종류가 잡혔다. (1) 알려진 억제 — 경고 하한 카테고리를 거절한 뒤 다시 열리면 첫 알림이 없이 600초 뒤 재알림으로 나간다. (2) 새 결함 — 첫 송신이 실패하면 복구가 즉시여도 첫 푸시가 사다리 시각(60초·600초)까지 기다린다. 이 둘은 LIOT 의 안전 알림이 '늦거나 침묵하지 않는다'는 게이트 1 문구와 부딪히며 소유자 판단이 필요하다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 거절 후 재발생 경로에서 알려진 억제 | **38 건** | `e2_invariants/results/summary.json` → `quota > by_path > reject_reopen > miss_classes > known_suppression` |
  | 억제된 건의 첫 푸시 시각(중앙) | **600 초** | `e2_invariants/results/summary.json` → `quota > by_path > reject_reopen > delayed_first_push_s > median` |
  | 첫 송신 실패 경로 100회 중 지연 | **100 건** | `e2_invariants/results/summary.json` → `quota > by_path > first_send_fail > miss_classes > send_failure_delay` |
  | 첫 송신 실패 후 첫 푸시 지연(최소) | **60 초** | `e2_invariants/results/summary.json` → `quota > by_path > first_send_fail > delayed_first_push_s > min` |
  | 첫 송신 실패 후 첫 푸시 지연(중앙) | **600 초** | `e2_invariants/results/summary.json` → `quota > by_path > first_send_fail > delayed_first_push_s > median` |
  | 알려진 억제 밖의 '미상' 위반 | **0 건** | `e2_invariants/results/summary.json` → `quota > unknown_misses` |
  | 탐침 14칸 중 두 번째 첫 알림이 나간 칸 | **9 칸** | `e2_invariants/results/summary.json` → `probe_dismissal > * > second_first_alert_pushed` (count_true) |
  | 탐침 14칸 중 나가지 않은 칸 | **5 칸** | `e2_invariants/results/summary.json` → `probe_dismissal > * > second_first_alert_pushed` (count_false) |

- **반드시 함께 쓸 한계 문장**: '위반 0'은 무작위 퍼징이 만든 경로 안에서의 0이다. 경로를 강제로 채운 퍼징에서는 게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류가 잡혔다. 재시작·동시성의 모든 조합은 덮지 않았다.

#### E2-04 · H1.5 (= P1)

- **최종 판정**: 기술 결과(단정 없음)
- **주장**: 보호자 호출을 표 하한(LIOT)으로 정하느냐 모델 상향을 포함한 최종값으로 정하느냐는 어느 쪽이 늘 낫다고 쓸 수 없다. 손익분기 비용비 c* 지도에서 맞교환 칸이 대부분이고, 정직한 모델·공격 없음·미등록 취약 가구 10%에서는 최종값 방식이 오호출은 같고 놓침만 6.93%에서 0%로 줄이는 순이득이다 — 이것은 LIOT 가 지는 곳이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 모델이 진실을 보는 모드 — LIOT 우세 칸(구조상) | **92 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_sees_truth > liot_dominates` |
  | 같은 모드 — 최종값 우세 칸 | **8 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_sees_truth > final_dominates` |
  | 같은 모드 — 맞교환 칸 | **184 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_sees_truth > tradeoff` |
  | 취약성을 못 보는 모드(blind) — LIOT 우세 칸(구조상) | **92 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_blind > liot_dominates` |
  | 같은 모드 — 동률 칸(최종값 우세 칸은 없음) | **12 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_blind > tie` |
  | 같은 모드 — 맞교환 칸 | **184 칸** | `e2_invariants/results/summary.json` → `h15 > class_counts_model_blind > tradeoff` |
  | 핵심 칸(오경보 2%·미등록 10%·정직도 0.9·공격 0) LIOT 오호출 | **0.61%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > liot > false_rate` |
  | 같은 칸 LIOT 놓침 | **6.93%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > liot > miss_rate` |
  | 같은 칸 최종값 오호출 | **2.49%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > final > false_rate` |
  | 같은 칸 최종값 놓침 | **0.69%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > final > miss_rate` |
  | 같은 칸 c* | **0.30** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > c_star > point` |
  | 정직도 1.0·공격 0 — 최종값 놓침 | **0.00%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h1.0|a0.0|b0|base > final > miss_rate` |
  | 정직도 1.0·공격 30% — c* | **2.41** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h1.0|a0.3|b0|base > c_star > point` |
  | 미등록 30%·정직도 0.9 — c* | **0.08** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.3|h0.9|a0.0|b0|base > c_star > point` |
  | 모델이 진실을 보는 모드 c* 중앙값(맞교환 칸) | **1.02** | `e2_invariants/results/summary.json` 의 `h15 > cells` 칸들에서 계산(파생값) — 파생값: summary.json 칸들에서 계산 |
  | 취약성 못 보는 모드 c* 중앙값(구조상) | **5.02** | `e2_invariants/results/summary.json` 의 `h15 > cells` 칸들에서 계산(파생값) — 파생값: summary.json 칸들에서 계산 |

- **그림**: `experiments/e2_invariants/figures/h15_cstar_maps.png`
- **반드시 함께 쓸 한계 문장**: 모델 정직도·공격 비율·센서 오경보율·미등록 취약 가구 비율은 모두 가정이고 범위를 쓸어 보았을 뿐이다. 최종값 방식과 추론 변형은 서비스에 구현하지 않고 결정 수준에서 시뮬레이션했다.

#### E2-05 · C2.8

- **최종 판정**: 읽기에 따라 갈림(읽기 i 반증 · 읽기 ii 반증 아님)
- **주장**: 상황 심각도가 특성 없는 관찰 필드로 드러날 때 '모델 상향을 보호자 호출에 쓰는 이득은 작다'는 결론은 AUC 만으로 나오지 않는다. 등록 문구에 '몇 칸에서'가 없어 두 읽기를 모두 보고한다 — 한 칸이라도 c* 가 2 를 넘으면 반증(읽기 i)이고 과반 칸에서 넘어야 반증(읽기 ii)이면 반증이 아니다. 현재 운영(AUC 0.5)에서는 최종값 방식이 오호출을 1~2%p 늘리고 놓침은 0.75%p 이하만 줄인다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | AUC 0.8 이상 비구조 칸 중 c*>2 비율 — 진실 모드 | **26.0%** | `e2_invariants/results/summary.json` → `h15b > by_mode > honest > auc_ge_0.8_nonstructural > cstar_over_2_share` |
  | 같은 비율 — 취약성 못 보는 모드(게이트 3 을 지키는 모드) | **42.7%** | `e2_invariants/results/summary.json` 의 `h15b > cells` 칸들에서 계산(파생값) — 파생값 — summary.json 의 blind 요약 칸이 비어 있어 칸들에서 다시 계산(RESULTS.md 와 일치) |
  | AUC 0.8 이상 비구조 칸 수(모드마다) | **96 칸** | `e2_invariants/results/summary.json` → `h15b > by_mode > honest > auc_ge_0.8_nonstructural > cells` |
  | 현재 운영 지점 — 최종값 − LIOT 오호출 최대 | **+1.9%p** | `e2_invariants/results/summary.json` → `h15b > e5_operational > final_minus_liot_false_rate_max` |
  | 현재 운영 지점 — 최종값 − LIOT 놓침 최소(가장 많이 줄인 값) | **-0.75%p** | `e2_invariants/results/summary.json` → `h15b > e5_operational > final_minus_liot_miss_rate_min` |
  | 현재 운영 지점 18칸 중 맞교환 칸 | **16 칸** | `e2_invariants/results/summary.json` → `h15b > e5_operational > class_counts_attack0 > tradeoff` |
  | 현재 운영 지점 18칸 중 LIOT 우세 칸 | **2 칸** | `e2_invariants/results/summary.json` → `h15b > e5_operational > class_counts_attack0 > liot_dominates` |

- **그림**: `experiments/e2_invariants/figures/c28_cstar_vs_auc.png`
- **반드시 함께 쓸 한계 문장**: 탐지기 문턱(오탐 상향률 5%)과 '관찰 필드로 드러나는 위험' 비율은 가정이고, AUC 는 정규분포 가정의 요약이다. 반증 조건에 '몇 칸에서'가 없어 두 읽기를 모두 적었으므로, 결론은 읽기에 달렸다.

#### E2-06 · H1.5 (탐색 — 거주자 특성 추론 허용 변형)

- **최종 판정**: 기술 결과(탐색 — 설계로 채택할 근거가 아님)
- **주장**: 거주자 특성을 추론해도 되는 변형은 놓침을 줄이는 것처럼 보이는 구간이 있다(미등록 취약 10%, 오경보 2%에서 참양성 95%·거짓양성 0%이면 놓침 6.93%→0.34%). 그러나 추론이 부정확해지면(참양성 80%·거짓양성 5%) 오호출이 0.61%에서 3.35%로 오른다. 이 변형은 하드 게이트 3('특성은 추론하지 않는다')을 깨는 것이므로 개선처럼 보여도 설계 채택의 근거가 아니다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | LIOT 놓침(미등록 10%·오경보 2%) | **6.93%** | `e2_invariants/results/summary.json` → `h15 > cells > fa0.02|u0.1|h0.9|a0.0|b0|base > liot > miss_rate` |
  | 추론(참양성 0.95·거짓양성 0%) 놓침 | **0.34%** | `e2_invariants/results/summary.json` → `h15 > infer > fa0.02|u0.1 > infer_0.95_0.0 > miss_rate` |
  | 추론(0.8·5%) 오호출 | **3.35%** | `e2_invariants/results/summary.json` → `h15 > infer > fa0.02|u0.1 > infer_0.8_0.05 > false_rate` |
  | 추론(0.8·5%) 놓침 | **1.41%** | `e2_invariants/results/summary.json` → `h15 > infer > fa0.02|u0.1 > infer_0.8_0.05 > miss_rate` |
  | 추론(0.5·20%) 오호출 | **11.76%** | `e2_invariants/results/summary.json` → `h15 > infer > fa0.02|u0.1 > infer_0.5_0.2 > false_rate` |

- **그림**: `experiments/e2_invariants/figures/h15_trait_inference_exploratory.png`
- **반드시 함께 쓸 한계 문장**: 모델 정직도·공격 비율·센서 오경보율·미등록 취약 가구 비율은 모두 가정이고 범위를 쓸어 보았을 뿐이다. 최종값 방식과 추론 변형은 서비스에 구현하지 않고 결정 수준에서 시뮬레이션했다.


### Ⅳ-Q2. 부담 대 피해 — 확인을 얼마나 세게 (E3)

#### E3-01 · H2.1 (= P2) 참고 수치

- **최종 판정**: 판정 불가(구조상 결과, liot_real 부분)
- **주장**: 정책 평균(세트 400개, 오분류 m=10%, 지수 피로). LIOT 실제(능동 상한)는 하루 확인 3.4건으로 가장 적고 해로운 실행률도 가장 낮지만, 그 낮음은 땅거미 이상을 통째로 침묵시키는 데서 오며(구조상 결과) 도움 완료율도 모두 명시 확인과 같은 수준에 그친다. LIOT 가상(4단계 전부)은 해로운 실행률이 모두 명시 확인보다 높다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 하루 확인 수 — LIOT 실제 | **3.4 건** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_real > burden_per_day > mean` |
  | 하루 확인 수 — LIOT 가상 | **9.6 건** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_full > burden_per_day > mean` |
  | 하루 확인 수 — 모두 명시 확인 | **12.0 건** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > flat_explicit > burden_per_day > mean` |
  | 해로운 실행률 — LIOT 실제 | **7.1%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_real > harm_rate > mean` |
  | 해로운 실행률 — LIOT 가상 | **29.2%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_full > harm_rate > mean` |
  | 해로운 실행률 — 모두 명시 확인 | **26.2%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > flat_explicit > harm_rate > mean` |
  | 해로운 실행률 — 이진(외부 목록) | **90.3%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > binary_ext > harm_rate > mean` |
  | 도움 완료율 — LIOT 실제 | **44.8%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_real > helpful_completion > mean` |
  | 도움 완료율 — LIOT 가상 | **59.8%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > liot_full > helpful_completion > mean` |
  | 도움 완료율 — 모두 명시 확인 | **45.4%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > flat_explicit > helpful_completion > mean` |

- **그림**: `experiments/e3_policy/figures/pareto_burden_vs_harm.png`
- **반드시 함께 쓸 한계 문장**: 거주자가 알림을 읽고 판단하는 모델(피로·무심코 승인·정확도·응답 지연)은 전부 가정이고 사람 데이터로 맞춘 값이 아니다. 결론은 '가정한 범위 안에서'만 읽는다.

#### E3-02 · H2.1 (= P2), N1

- **최종 판정**: 판정 불가(구조상 결과)
- **주장**: 같은 능동 상한(땅거미 이상 침묵)을 모두 명시 확인에도 똑같이 씌우면 상한의 몫이 빠지고 '단계별 확인만의 값'이 남는다. 이때 LIOT 실제는 오분류 10%에서 해로운 실행률이 +3.2%p 높고(95.8% 세트에서 더 나쁨), 도움 완료는 +12.5%p 높고, 확인은 하루 2.4건 적다. 즉 LIOT 실제의 낮은 피해는 단계 매김의 이점이 아니라 상한(침묵)에서 온다. LIOT 의 낮은 피해를 '단계 매김의 정보'로 읽을 근거는 없다. (최종 재검증에서 정확한 동률을 별도 범주로 세도록 고쳤다 — m=0 에서는 LIOT 실제가 더 나쁜 세트 16.3%, 동률 35.8%, 더 좋은 세트 48.0% 로 갈라 적는다. 이전 판의 21%는 쓰지 않는다.)
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 해로운 실행률 차이(LIOT 실제 − 모두 명시 확인+같은 상한) | **+3.2%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > harm_diff > mean` |
  | 그 차이의 세트 간 사분위 하한(Q25) | **+2.1%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > harm_diff > p25` |
  | 그 차이의 세트 간 사분위 상한(Q75) | **+4.4%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > harm_diff > p75` |
  | LIOT 실제가 더 나쁜 세트 비율(점추정) | **95.8%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > share_sets_liot_real_harm_higher_point` |
  | 도움 완료율 차이(LIOT 실제 − 대조) | **+12.5%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > help_diff > mean` |
  | 하루 확인 수 차이 | **-2.40 건** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > liot_real_vs_flat_same_liot_ceiling > burden_diff > mean` |
  | 같은 비교 | **-0.2%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.0 > liot_real_vs_flat_same_liot_ceiling > harm_diff > mean` |
  | 같은 비교 | **16.3%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.0 > liot_real_vs_flat_same_liot_ceiling > share_sets_liot_real_harm_higher_point` |
  | 같은 비교 | **35.8%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.0 > liot_real_vs_flat_same_liot_ceiling > share_sets_liot_real_harm_tie` |
  | 같은 비교 | **48.0%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.0 > liot_real_vs_flat_same_liot_ceiling > share_sets_liot_real_harm_lower_point` |

- **그림**: `experiments/e3_policy/figures/c22_ceiling_matched_silence.png`
- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-03 · H2.1 (= P2) 참고(liot_full)

- **최종 판정**: 판정 불가(liot_real 부분) · liot_full 은 참고
- **주장**: 상한 없이 4단계 전부를 쓰는 LIOT 가상은 오분류 10%에서 세트의 약 69%에서 해로운 실행률이 모두 명시 확인보다 유의하게 높고(씨앗 잡음 기준), 낮은 세트는 약 5%뿐이다. 도움 완료율은 +14%p 높다. 오분류가 늘수록 열세가 깊어진다(m=40%에서 +13.2%p).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | LIOT 가상이 유의하게 더 해로운 세트(m=10%) | **68.5%** | `e3_policy/results/summary.json` → `h2_1 > exp > m0.1 > liot_full > harm > areas > higher > share` |
  | 유의하게 덜 해로운 세트(m=10%) | **4.8%** | `e3_policy/results/summary.json` → `h2_1 > exp > m0.1 > liot_full > harm > areas > lower > share` |
  | 이진(사후 최적 목록)의 해로운 실행률(m=10%) | **34.8%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > binary_opt > harm_rate > mean` |

- **그림**: `experiments/e3_policy/figures/winfraction_vs_m.png`
- **반드시 함께 쓸 한계 문장**: 구간은 같은 매개변수 점 안에서 씨앗만 다시 뽑은 '씨앗 잡음'이다. 가정(매개변수)의 불확실성은 어느 구간에도 들어 있지 않고, 세트 사이의 변동은 사분위로만 보였다.

#### E3-04 · H2.2

- **최종 판정**: 판정 불가(liot_full 약한 통과 · liot_real 불통과 → liot_real 관련 E3 결론 판정 불가)
- **주장**: 시뮬레이터 점검(피로 0·무심코 승인 0). LIOT 실제는 등록된 점검을 통과하지 못한다 — 모두 명시 확인보다 낮은 세트가 97.8%인데, 침묵은 오류가 없고 확인은 확률 1−a 로 틀리기 때문이며 해석식 대조는 통과해 시뮬레이터 결함은 아니다. 등록 규칙대로 liot_real 이 관련된 E3 결론은 '판정 불가·구조상 결과'로 내린다. LIOT 가상의 통과는 약하다(세트의 67%에서 차이가 정확히 0).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | LIOT 실제 — 모두 명시 확인이 더 높은 세트 | **97.8%** | `e3_policy/results/summary.json` → `h2_2 > verdict_inputs > liot_real > share_sets_flat_strictly_higher` |
  | LIOT 가상 — 차이가 정확히 0 인 세트(m=0) | **67%** | `e3_policy/results/summary.json` → `h2_2 > m0.0 > liot_full > share_sets_difference_exactly_zero` |
  | LIOT 가상 — 모두 명시 확인이 유의하게 낮은 세트 | **14%** | `e3_policy/results/summary.json` → `h2_2 > verdict_inputs > liot_full > share_sets_flat_strictly_lower` |

- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-05 · H2.4 (= P3)

- **최종 판정**: 판정 불가(구조상 결과, liot_real 포함)
- **주장**: 오분류가 늘면 4단계의 이점이 사라지는 경계. liot_real 이 포함된 등록 지표는 판정 불가·구조상 결과다. 참고로 liot_full 만 본 이점 영역은 m=0 에서도 9%로 작고 m=40%에서 0.3%까지 줄어, 경계 m* 는 격자의 첫 칸(10%) 이하다(경계 정의는 우리가 사후에 정함).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | liot_full 만의 이점 영역 면적(m=0) | **9.0%** | `e3_policy/results/summary.json` → `h2_4 > exp > area_m0` |
  | 같은 면적(m=40%) | **0.3%** | `e3_policy/results/summary.json` → `h2_4 > exp > area_m40` |

- **그림**: `experiments/e3_policy/figures/winfraction_vs_m.png`
- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-06 · H2.6 (= P4)

- **최종 판정**: '막은 피해' 판정 불가(구조상 결과) · '잃은 도움' 기술 결과
- **주장**: 실제 능동 상한이 치르는 비용. 상한 때문에 사라지는 도움 제안은 전체의 약 45.8%이고, 그 대가로 막은 해로운 제안은 86.2%지만 후자는 생성기 정렬에서 오는 구조상 결과라 결론에 쓰지 않는다. 사라지는 도움의 약 88%가 가전·출입이며, 이것이 LIOT 설계의 실제 비용이다. LIOT 가상과 비교하면 도움 완료율이 약 15%p 낮다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 상한에 걸려 사라지는 도움 제안(풀링) | **45.8%** | `e3_policy/results/summary.json` → `h2_6 > exp > pooled_silenced_helpful_share` |
  | 같은 상한이 막은 해로운 제안(구조상) | **86.2%** | `e3_policy/results/summary.json` → `h2_6 > exp > pooled_silenced_harm_share` |
  | 막힌 도움 중 가전 몫 | **48.3%** | `e3_policy/results/summary.json` → `h2_6_device_sweep > contribution_to_total_silenced > 3` |
  | 막힌 도움 중 출입 몫 | **39.4%** | `e3_policy/results/summary.json` → `h2_6_device_sweep > contribution_to_total_silenced > 4` |
  | 도움 완료율 차이(LIOT 실제 − LIOT 가상) | **-15.0%p** | `e3_policy/results/summary.json` → `h2_6 > exp > real_minus_full_help_completion > mean` |

- **그림**: `experiments/e3_policy/figures/silenced_helpful_by_device.png`
- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-07 · C2.1

- **최종 판정**: 반증
- **주장**: 예측은 '해로운 제안이 숨결 이하로 몰릴수록 liot_real 의 피해 이점이 줄어 50% 에서 과반 아래로 떨어진다'였다. 해로운 제안의 50%를 숨결 이하 견본으로 옮겨도 모두 명시 확인보다 피해가 유의하게 낮은 세트가 과반(예측은 50% 아래로 떨어진다)이어서 예측이 틀렸다. 단, 이 다이얼은 해로움을 잔잔(자동 실행)에 놓지 않은 LIOT 에 유리한 설정이다 — 아래 E3-10 이 그 한계를 푼다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 숨결 이하 50% — 이점 면적(m=0) | **88.25%** | `e3_policy/results/summary.json` → `c2_1 > rows > block=c21_p050 > m0.0 > liot_real > area_harm_lower_seed_noise_sig > share` |
  | 숨결 이하 50% — 이점 면적(m=10%) | **71.3%** | `e3_policy/results/summary.json` → `c2_1 > rows > block=c21_p050 > m0.1 > liot_real > area_harm_lower_seed_noise_sig > share` |
  | 숨결 이하 100% — 이점 면적(m=10%) | **39.8%** | `e3_policy/results/summary.json` → `c2_1 > rows > block=c21_p100 > m0.1 > liot_real > area_harm_lower_seed_noise_sig > share` |

- **그림**: `experiments/e3_policy/figures/c21_harm_template_shift.png`
- **반드시 함께 쓸 한계 문장**: 생성기의 해로움 규칙과 견본, 그리고 해로운 제안이 어느 단계에 놓이는지를 정하는 노브는 우리가 정했다. 잔잔 쪽 해로움은 한 견본(조명 끄기)에 몰려 있어 다른 잔잔 견본은 시험하지 않았다.

#### E3-08 · C2.2

- **최종 판정**: 반증(이진·모두 자동 기준) — 모두 명시 확인+상한 기준에서는 예측이 대체로 성립(구조상 해석)
- **주장**: 같은 침묵 비율이면 상한 종류와 무관하게 같은 맞교환이라는 예측은 모두 명시 확인+상한에서만 대체로 맞고(±2%p 안 73%), 이진(외부)·모두 자동에 상한을 씌운 비교에서는 LIOT 단계 상한이 같은 침묵 비율의 생성기 상한보다 피해가 약 3%p 낮아 반증 조건이 88% 세트에서 충족된다. 단 이 우위는 생성기와 LIOT 단계의 정렬에서 나오는 구조상 결과라 'LIOT 단계가 외부 기준에서도 정보가 있다'로 쓰지 않는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 모두 명시 확인+상한 — ±2%p 안(둘 다) 세트 | **73%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > flat_explicit > liot_minus_generator_ceiling_at_matched_silence > share_within_2pp_both > share` |
  | 이진(외부)+상한 — 반증 조건 해당 세트(m=10%) | **88%** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > binary_ext > liot_minus_generator_ceiling_at_matched_silence > share_falsifier_harm_lower_2pp_and_help_not_lower > share` |
  | 이진(외부)+상한 — 피해 차이 중앙값 | **-3.3%p** | `e3_policy/results/summary.json` → `c2_2 > exp > m0.1 > binary_ext > liot_minus_generator_ceiling_at_matched_silence > harm_diff > median` |

- **그림**: `experiments/e3_policy/figures/c22_ceiling_matched_silence.png`
- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-09 · C2.3

- **최종 판정**: 지지
- **주장**: LIOT 가상이 오분류 10% 이상에서 이점을 잃는 원인은 '잔잔으로 잘못 내려간 해로운 제안이 자동 실행되기 때문'이다. 잔잔을 알림만(자동 실행 안 함)으로 바꾸면 모두 명시 확인보다 피해가 높은 세트가 과반에서 1%로 줄어 피해 열세가 사라진다. 대가로 도움 완료율이 자동 실행일 때의 +13%p 에서 −11%p 로 내려간다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 잔잔 자동 실행 — 유의하게 더 해로운 세트(m=10%) | **62.8%** | `e3_policy/results/summary.json` → `c2_3 > exp > 1 > liot_full > area_harm_higher_seed_noise_sig > share` |
  | 잔잔 알림만 — 유의하게 더 해로운 세트(m=10%) | **1.3%** | `e3_policy/results/summary.json` → `c2_3 > exp > 1 > liot_full_notify_only > area_harm_higher_seed_noise_sig > share` |
  | 도움 완료 차이 — 알림만(m=10%) | **-10.8%p** | `e3_policy/results/summary.json` → `c2_3 > exp > 1 > liot_full_notify_only > help_diff_pooled > mean` |
  | 도움 완료 차이 — 자동 실행(m=10%) | **+13.1%p** | `e3_policy/results/summary.json` → `c2_3 > exp > 1 > liot_full > help_diff_pooled > mean` |

- **그림**: `experiments/e3_policy/figures/c23_ambient_notify_only.png`
- **반드시 함께 쓸 한계 문장**: 거주자가 알림을 읽고 판단하는 모델(피로·무심코 승인·정확도·응답 지연)은 전부 가정이고 사람 데이터로 맞춘 값이 아니다. 결론은 '가정한 범위 안에서'만 읽는다.

#### E3-10 · C2.11 (a)

- **최종 판정**: 지지
- **주장**: 해로운 제안이 잔잔(자동 실행)에 놓이는 비율을 높이면 liot_real 의 피해 이점이 사라진다. 잔잔 50%에서 이점 면적이 5~9%로 떨어진다(예측대로). 다만 50% 이상 구간의 큰 손실은 정의에 가까운 구조상 결과다(잔잔은 확인 없이 자동 실행되므로 거기 놓인 해로운 제안은 정의상 실행되고, 모두 명시 확인은 그것을 확인한다) — 그 격차를 발견으로 쓰지 않는다. 정보가 되는 출력은 교차점이다 — 이점 면적이 50% 아래로 내려가는 잔잔 비율은 약 16%(m=10%)~20%(m=0)다. 따라서 E3-07 의 '이점이 남는다'는 잔잔 쪽 해로움이 그 교차점 아래일 때만 성립하며, 현실의 잔잔 비율은 모른다. 또한 잔잔 해로움(저녁 조명 끄기)은 구성상 낮은 심각도인데 건수로는 '오븐 켜고 외출'과 똑같이 1건으로 센다(심각도 가중 없음).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 잔잔 50% — 이점 면적(m=0) | **8.5%** | `e3_policy/results/summary.json` → `c2_11a > rows > block=c211a_p050 > m0.0 > liot_real > area_harm_lower_seed_noise_sig > share` |
  | 잔잔 50% — 이점 면적(m=10%) | **5.0%** | `e3_policy/results/summary.json` → `c2_11a > rows > block=c211a_p050 > m0.1 > liot_real > area_harm_lower_seed_noise_sig > share` |
  | 잔잔 50% — 모두 명시 확인보다 유의하게 더 해로운 세트(m=10%) | **85.0%** | `e3_policy/results/summary.json` → `c2_11a > rows > block=c211a_p050 > m0.1 > liot_real > area_harm_higher_seed_noise_sig > share` |
  | 잔잔 50% — 피해 차이 평균(m=0) | **+21.6%p** | `e3_policy/results/summary.json` → `c2_11a > rows > block=c211a_p050 > m0.0 > liot_real > harm_diff_pooled > mean` |

- **그림**: `experiments/e3_policy/figures/c211_ambient_harm_and_light_stamp.png`
- **반드시 함께 쓸 한계 문장**: 생성기의 해로움 규칙과 견본, 그리고 해로운 제안이 어느 단계에 놓이는지를 정하는 노브는 우리가 정했다. 잔잔 쪽 해로움은 한 견본(조명 끄기)에 몰려 있어 다른 잔잔 견본은 시험하지 않았다.

#### E3-11 · C2.11 (b)

- **최종 판정**: 지지(m=10%) · 판정 불가(m=0)
- **주장**: 탭 한 번 확인이 명시 확인보다 무심코 승인되기 쉬우면(2.0배) liot_full 의 피해가 모두 명시 확인보다 높은 세트가 m=10%에서 과반이지만, 이 조건은 기준선(1.0배)에서 이미 충족돼 있었다 — 예측이 맞았다기보다 배수와 무관하게 성립하는 값이다. 발견은 2.0배의 한계 효과(피해 차이 평균 +3.1%p → +4.6%p, 약 +1.5%p, 유의하게 높은 세트 68.5% → 79.7%)다. m=0 에서는 점추정으로 과반이지만 유의 기준으로는 과반이 아니어서 판정 불가로 둔다. LIOT 실제는 이 설정에서도 피해가 낮은 세트가 85%지만 그것은 침묵 때문이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 2.0배 — liot_full 이 유의하게 더 해로운 세트(m=10%) | **79.75%** | `e3_policy/results/summary.json` → `c2_11b > rows > block=c211b_x200 > m0.1 > liot_full > area_harm_higher_seed_noise_sig > share` |
  | 2.0배 — 같은 값(m=0) | **30.25%** | `e3_policy/results/summary.json` → `c2_11b > rows > block=c211b_x200 > m0.0 > liot_full > area_harm_higher_seed_noise_sig > share` |
  | 1.0배(기준선) — liot_full 피해 차이 평균(m=10%) | **+3.1%p** | `e3_policy/results/summary.json` → `c2_11b > rows > block=c211b_x100 > m0.1 > liot_full > harm_diff_pooled > mean` |
  | 2.0배 — liot_full 피해 차이 평균(m=10%) | **+4.6%p** | `e3_policy/results/summary.json` → `c2_11b > rows > block=c211b_x200 > m0.1 > liot_full > harm_diff_pooled > mean` |
  | 2.0배 — liot_real 이 유의하게 덜 해로운 세트(m=10%) | **85.3%** | `e3_policy/results/summary.json` → `c2_11b > rows > block=c211b_x200 > m0.1 > liot_real > area_harm_lower_seed_noise_sig > share` |

- **그림**: `experiments/e3_policy/figures/c211_ambient_harm_and_light_stamp.png`
- **반드시 함께 쓸 한계 문장**: 생성기의 해로움 규칙과 견본, 그리고 해로운 제안이 어느 단계에 놓이는지를 정하는 노브는 우리가 정했다. 잔잔 쪽 해로움은 한 견본(조명 끄기)에 몰려 있어 다른 잔잔 견본은 시험하지 않았다.

#### E3-12 · C2.4

- **최종 판정**: 기술 결과
- **주장**: 위험 점수가 얼마나 정확해야 θ 정책(점수 상위 일부에만 확인)이 4단계를 이기는가. 오분류 10%에서 θ 정책이 liot_full 을 과반 세트에서 지배하려면 점수 AUC 가 약 0.81 이상이어야 한다. LIOT 단계 자체의 AUC 는 0.68(오분류 0)·0.63(10%)로 이 임계보다 훨씬 낮다 — 오분류 10%의 LIOT 단계는 AUC 0.8 정도의 점수에게 진다. 단 AUC 가 같아도 확인 강도 배치가 달라 우열이 갈릴 수 있다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | LIOT 단계의 AUC(오분류 0) | **0.68** | `e3_policy/results/summary.json` → `c2_4 > liot_tier_auc > tier_m0.0_median` |
  | LIOT 단계의 AUC(오분류 10%) | **0.63** | `e3_policy/results/summary.json` → `c2_4 > liot_tier_auc > tier_m0.1_median` |
  | 생성기 기본 심각도의 AUC(참고) | **0.79** | `e3_policy/results/summary.json` → `c2_4 > liot_tier_auc > base_sev_median` |
  | 잡음 없는 점수(AUC 0.82)가 liot_full 을 지배하는 세트(m=10%) | **57.5%** | `e3_policy/results/summary.json` → `c2_4 > rows > kappa=0.0,m=0.1 > share_sets_dominating_liot_full > share` |
  | 같은 값(m=0) | **14.8%** | `e3_policy/results/summary.json` → `c2_4 > rows > kappa=0.0,m=0.0 > share_sets_dominating_liot_full > share` |

- **그림**: `experiments/e3_policy/figures/c24_theta_auc.png`
- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-13 · H2.2 · C2.1 (생성기 정렬)

- **최종 판정**: 구조상 결과(측정이 아니라 설계)
- **주장**: 시뮬레이터가 만든 해로운 제안의 약 95.8%가 LIOT 자신의 단계로 땅거미·불씨에 놓이고 잔잔은 0%다. 이 정렬은 `generator.py` 의 해로움 규칙과 견본을 우리가 쓴 결과이지 세상에서 측정한 것이 아니다. E3 의 'LIOT 실제'가 막은 피해를 크게 만드는 원인의 일부이므로, 이 정렬에 기댄 수치는 결론에 쓰지 않는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 해로운 제안 중 땅거미·불씨에 놓이는 몫(풀링) | **95.8%** | `e3_policy/results/summary.json` → `harm_alignment > harmful_share_dusk_or_ember_pooled` |
  | 잔잔에 놓이는 몫 | **0.0%** | `e3_policy/results/summary.json` → `harm_alignment > harmful_share_by_tier_pooled > AMBIENT` |
  | 숨결에 놓이는 몫 | **4.2%** | `e3_policy/results/summary.json` → `harm_alignment > harmful_share_by_tier_pooled > BREATH` |

- **반드시 함께 쓸 한계 문장**: 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.

#### E3-14 · H2.3 · H2.5 · H2.7 (탐색)

- **최종 판정**: 기술 결과
- **주장**: 탐색 가설 세 가지는 기준이 없어 기술 결과로만 쓴다. (H2.3) 확인 비율 θ 에 대한 피해의 U자는 점수가 거의 완벽할 때(AUC 0.82)만 나온다. (H2.5) 외부 기준 목록(문 잠금 해제·차고 열기만 확인)의 이진 승인은 해로운 제안을 약 90% 통과시킨다. (H2.7) 자동 실행 + 15분 되돌리기 창은 해로운 실행률이 모두 명시 확인보다 훨씬 높다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | U자가 나오는 세트(f>0 | **41%** | `e3_policy/results/summary.json` → `h2_3 > exp > s0.0 > u_shape_sig > share` |
  | 이진(외부 목록) 해로운 실행률 | **90.3%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > binary_ext > harm_rate > mean` |
  | 자동+되돌리기 15분 해로운 실행률 | **69.7%** | `e3_policy/results/summary.json` → `policy_table > exp > m0.1 > auto_undo_w15 > harm_rate > mean` |

- **그림**: `experiments/e3_policy/figures/harm_vs_theta.png`
- **반드시 함께 쓸 한계 문장**: 거주자가 알림을 읽고 판단하는 모델(피로·무심코 승인·정확도·응답 지연)은 전부 가정이고 사람 데이터로 맞춘 값이 아니다. 결론은 '가정한 범위 안에서'만 읽는다.


### Ⅳ-Q3. 위험 판정 방식 (E4)

#### E4-01 · H3.1

- **최종 판정**: 기술 결과(판정 대상 아님)
- **주장**: 단조(한 축이 나빠지면 등급이 내려가지 않음) 공리는 조회표와 곱셈 점수 사이를 가르지 못한다 — 둘 다 구조상 위반 0 이다. 동점 손실(분명히 더 나쁜 입력이 같은 등급이 된 쌍)은 우리 조회표가 32쌍(20.5%)으로, 곱셈 점수의 최선 설정(14쌍)보다 더 뭉갠다 — 이것은 LIOT 가 지는 곳이다. 곱셈 점수 중앙값(43)과 표(32)의 비교는 '손으로 맞춘 것 대 임의 절단점'이라 같은 조건이 아니다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 조회표 동점 손실 | **32 쌍** | `e4_scoring/results/summary.json` → `table > tie_loss` |
  | 조회표 동점 손실 비율 | **20.5%** | `e4_scoring/results/summary.json` → `table > tie_loss_share` |
  | 곱셈(격자 M3) 동점 손실 최소 | **14 쌍** | `e4_scoring/results/summary.json` → `mult_grid > M3 > tie_loss > min` |
  | 곱셈(격자 M3) 동점 손실 중앙값 | **43 쌍** | `e4_scoring/results/summary.json` → `mult_grid > M3 > tie_loss > median` |
  | 곱셈(격자 M3) 동점 손실 최대 | **111 쌍** | `e4_scoring/results/summary.json` → `mult_grid > M3 > tie_loss > max` |
  | 곱셈(M2p1) 동점 손실 최소 | **16 쌍** | `e4_scoring/results/summary.json` → `mult_grid > M2p1 > tie_loss > min` |
  | 단일 등급 동점 손실(우세 쌍 20개 중) | **20 쌍** | `e4_scoring/results/summary.json` → `single_tier > tie_loss` |
  | 곱셈(M3) 단조 위반 설정 | **0 개** | `e4_scoring/results/summary.json` → `mult_grid > M3 > mono_violation_settings` |
  | 조회표 단조 위반 | **0 건** | `e4_scoring/results/summary.json` → `table > mono_violations` |
  | 표보다 동점이 많은 곱셈 설정 비율(M3 격자) | **82.7%** | `e4_scoring/results/summary.json` → `mult_grid > M3 > settings_with_more_tie_loss_than_table_share` |

- **반드시 함께 쓸 한계 문장**: 위험 등급에는 독립된 정답이 없다. 우리 조회표를 기준점으로 쓴 값('표 기준')은 순환적이고, 곱셈 점수의 '몇 %'는 우리가 고른 척도·절단점 범위에 따라 달라진다.

#### E4-02 · H3.1

- **최종 판정**: 기술 결과(판정 대상 아님)
- **주장**: 곱셈 점수에는 바닥이 없다. '취약 거주자 없음'의 생명·몇 분(가스·연기)이 위험 아래로 내려가는 곱셈 설정이 상당수이고(표 기준 표시), 서로 우열이 없는 120쌍은 척도에 따라 순서가 늘 뒤집힌다(정의상 결과). 조회표는 이 순서를 칸마다 명시해 두므로 척도 선택에 흔들리지 않는다 — 단 이것은 표가 '옳다'는 뜻이 아니라 '결정이 어디에 적혀 있는지'의 차이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 가스·연기가 위험 아래로 내려가는 곱셈(M3 격자) 설정 | **273,438 개** | `e4_scoring/results/summary.json` → `mult_grid > M3 > settings_with_a_critical_category_below_critical` |
  | 전체 곱셈(M3 격자) 설정 | **795,588 개** | `e4_scoring/results/summary.json` → `mult_grid > M3 > n_settings` |
  | 두 요인만 곱하는 M2p1 에서 같은 설정 | **0 개** | `e4_scoring/results/summary.json` → `mult_grid > M2p1 > settings_with_a_critical_category_below_critical` — 최고점 칸이 늘 최상위라 구조상 0 |
  | 우열이 없는 쌍 중 척도에 따라 뒤집히는 쌍(M3 격자) | **120 쌍** | `e4_scoring/results/summary.json` → `order_flips > grid_M3 > coding_dependent_pairs` |
  | 우열이 없는 쌍 전체 | **120 쌍** | `e4_scoring/results/summary.json` → `order_flips > n_incomparable_pairs` |
  | 한 쌍에서 소수 쪽 순서를 내는 척도의 평균 비율 | **25.6%** | `e4_scoring/results/summary.json` → `order_flips > grid_M3 > mean_minority_share` |

- **반드시 함께 쓸 한계 문장**: 위험 등급에는 독립된 정답이 없다. 우리 조회표를 기준점으로 쓴 값('표 기준')은 순환적이고, 곱셈 점수의 '몇 %'는 우리가 고른 척도·절단점 범위에 따라 달라진다.

#### E4-03 · H3.1

- **최종 판정**: 기술 결과(판정 대상 아님)
- **주장**: 곱셈 점수가 조회표의 24칸을 모두 똑같이 재현하는 설정은 극히 좁은 범위에서만 있다(0.002%). 이 값은 표를 기준점으로 쓴 순환적 값이라 '곱셈이 나쁘다'는 증거가 아니다. 모든 24칸이 아닌 단순 기본값 곱셈은 표와 24칸 중 2칸만 같다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 표를 24칸 전부 재현하는 곱셈(M3 격자) 설정 | **16 개** | `e4_scoring/results/summary.json` → `mult_grid > M3 > agree_all24_settings` |
  | 같은 설정(M2p1 격자) | **21 개** | `e4_scoring/results/summary.json` → `mult_grid > M2p1 > agree_all24_settings` |
  | 단순 기본값 곱셈이 표와 같은 칸 | **2/24 칸** | `e4_scoring/results/summary.json` → `mult_naive_default > agree_with_table_n` |

- **반드시 함께 쓸 한계 문장**: 위험 등급에는 독립된 정답이 없다. 우리 조회표를 기준점으로 쓴 값('표 기준')은 순환적이고, 곱셈 점수의 '몇 %'는 우리가 고른 척도·절단점 범위에 따라 달라진다.

#### E4-04 · H3.3

- **최종 판정**: 기술 결과(관찰 — 방향이 낮추는 쪽)
- **주장**: 모델 단독 판정은 평온 문장에서는 하한 아래로, 위험 문장에서는 하한 위로 답하고 우세한 카테고리를 열세한 카테고리보다 낮게 매기는 단조 위반이 표 기준 쌍에서 20쌍 중 1~8쌍 나온다(표·곱셈은 0). 주입된 '낮춰라' 사례(파급도 질문 4건)에서는 하한 아래로 내려가는 답이 3~4건이다. 최종 파급도는 하한과 모델 답 중 큰 값이라 이 하강이 최종 알림 세기를 낮추지는 못한다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 단조 위반(평온 문장) — apus | **2/20 쌍** | `e4_scoring/results/summary.json` → `model_only > models > apus-openjev-4b-q4 > category_axioms_calm > mono_violations` |
  | 단조 위반(평온 문장) — jevk5-9b-q8 | **8/20 쌍** | `e4_scoring/results/summary.json` → `model_only > models > jevk5-9b-q8 > category_axioms_calm > mono_violations` |
  | 단조 위반(위험 문장) — jevk5-4b-q8 | **1/20 쌍** | `e4_scoring/results/summary.json` → `model_only > models > jevk5-4b-q8 > category_axioms_danger > mono_violations` |
  | 주입 낮춰라 파급도 질문에서 하한 아래 — apus | **4/4 건** | `e4_scoring/results/summary.json` → `model_only > models > apus-openjev-4b-q4 > severity_injection_lower > below_floor` |
  | 같은 값 — jevk5-4b-q4 | **3/4 건** | `e4_scoring/results/summary.json` → `model_only > models > jevk5-4b-q4 > severity_injection_lower > below_floor` |

- **그림**: `experiments/e4_scoring/figures/model_only_lowering.png`
- **반드시 함께 쓸 한계 문장**: 모델 단독 표본은 작다(상황 8종·주입 8건). 모델 5개는 같은 입력 61건을 공유해 서로 독립이 아니다. 평온/위험 문장 짝은 사람이 아닌 에이전트가 설계했다.


### Ⅳ-Q4. 적응 (E6 — 진행 중)

#### E6-00 · H4.1 · H4.2 (= P5) · H4.3

- **최종 판정**: 대기
- **주장**: 개인화(학습·선호 변화·30일 거절 게이트) 실험은 아직 끝나지 않았다. 숫자 자리는 비워 둔다.
- **상태**: 대기 — `e6_personalization/results/summary.json` 이 아직 없다(숫자 없음).
- **반드시 함께 쓸 한계 문장**: 이 실험은 아직 끝나지 않아 숫자가 없다. 보고서에는 '진행 중'이라고만 쓰고 추정 숫자를 넣지 않는다.


### Ⅳ-Q5. 결정 모델 (E5·E9)

#### E5-01 · H5.1

- **최종 판정**: 구조상 결과
- **주장**: 운영처럼 집계 요약만 보면 하한 위로 올리는 답이 판정자 7명 합 75건에서 4건으로 거의 사라진다. 이것은 실험이 발견한 것이 아니라 운영 집계 요약에 상황 내용이 없어 평온 문장과 위험 문장이 같은 입력이 되는 데서 정해진 구조상 결과다 — '지지'가 아니다. 같은 맥락에서 하한 '아래'로 답한 수는 판정자에 따라 늘기도 줄기도 했다(합 105 → 140).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 올림 합(상황 문장 맥락) | **75 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > raised_sentence` |
  | 올림 합(운영 집계 요약) | **4 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > raised_operational` |
  | 하한 아래 합(상황 문장) | **105 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > below_floor_sentence` |
  | 하한 아래 합(운영 집계 요약) | **140 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > below_floor_operational` |
  | 올림 비율 차이 — 가장 작은 판정자(jevk5-9b-q8) | **+0.115** | `e5_models/results/summary.json` → `h51_sentence_vs_operational > jevk5-9b-q8 > rep0 > diff_sentence_minus_operational` |
  | 올림 비율 차이 — 가장 큰 판정자(Jev API) | **+0.213** | `e5_models/results/summary.json` → `h51_sentence_vs_operational > jev-api > rep0 > diff_sentence_minus_operational` |
  | 하한 아래 늘어난 예 — jevk5-9b-q8 문장 | **9 건** | `e5_models/results/summary.json` → `h51_sentence_vs_operational > jevk5-9b-q8 > rep0 > below_floor_sentence` |
  | 같은 판정자 | **27 건** | `e5_models/results/summary.json` → `h51_sentence_vs_operational > jevk5-9b-q8 > rep0 > below_floor_operational` |

- **그림**: `experiments/e5_models/figures/h51_raise_sentence_vs_operational.png`
- **반드시 함께 쓸 한계 문장**: 운영 집계 요약에는 상황 내용이 없어 평온 문장과 위험 문장이 같은 입력이 된다. 그래서 올림이 0 에 가까운 것은 실험이 발견한 것이 아니라 입력에서 정해진 구조상 결과다.

#### E5-02 · H5.2

- **최종 판정**: 판정 불가
- **주장**: 클라우드 판정자가 주입 '낮춰라' 사례에 더 강하다고 말할 수 없다. 클라우드 최선(Haiku 2/8)과 로컬 최선(5/8)의 점추정 차이는 3사례지만 95% 구간이 크게 겹친다. '최선'을 결과를 보고 고른 것이라 이 비교 자체가 사후 선택이고, luna 는 크레딧 소진으로 호출할 수 없었다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | Haiku 가 '낮춰라' 8건 중 하한 아래로 내려간 사례 | **2/8 건** | `e5_models/results/summary.json` → `h52_lower_injection > haiku > sentence > rep=0 > cases_lowered_any_question` |
  | Haiku 95% 구간 하한 | **0.03** | `e5_models/results/summary.json` → `h52_lower_injection > haiku > sentence > rep=0 > cases_lowered_ci95 > 0` |
  | Haiku 95% 구간 상한 | **0.65** | `e5_models/results/summary.json` → `h52_lower_injection > haiku > sentence > rep=0 > cases_lowered_ci95 > 1` |
  | Jev API | **3/8 건** | `e5_models/results/summary.json` → `h52_lower_injection > jev-api > sentence > rep=0 > cases_lowered_any_question` |
  | 로컬 jevk5-4b-q8 | **5/8 건** | `e5_models/results/summary.json` → `h52_lower_injection > jevk5-4b-q8 > sentence > rep=0 > cases_lowered_any_question` |
  | 로컬 jev-9b-q6(가장 많음) | **7/8 건** | `e5_models/results/summary.json` → `h52_lower_injection > jev-9b-q6 > sentence > rep=0 > cases_lowered_any_question` |
  | luna 호출 실패 횟수 | **6 회** | `e5_models/results/summary.json` → `unavailable_judges > luna > rows` |

- **그림**: `experiments/e5_models/figures/h52_lowered_by_injection.png`
- **반드시 함께 쓸 한계 문장**: 문항이 작다(주입 제외 61문항·주입 16건·쌍 15개). 판정자들은 같은 문항을 공유해 서로 독립이 아니고, luna 는 크레딧 소진으로 빠졌으며, 로컬 모델은 글자 확률·클라우드는 서로 다른 인터페이스라 같은 측정이 아니다.

#### E5-03 · H5.3

- **최종 판정**: 기술 결과(탐색, 기준 없음)
- **주장**: 판정자끼리 크게 갈린다. 상황 문장 맥락에서 5명의 Gwet AC1 은 확인 강도 0.53·파급도 0.42이고, 적어도 한 명이 하한을 벗어난 문항만 보면 일치가 더 낮다(AC1 0.34). 'κ ≥ 0.6' 같은 선은 결과를 본 뒤의 사후 기준이라 판정에 쓰지 않는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 5명 AC1 — 확인 강도 | **0.532** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > all > tier > gwet_ac1` |
  | 5명 AC1 — 파급도 | **0.423** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > all > severity > gwet_ac1` |
  | 5명 Fleiss κ — 확인 강도 | **0.366** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > all > tier > fleiss_kappa` |
  | 하한을 벗어난 판정자가 있는 문항에서의 AC1(확인 강도) | **0.344** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > all > tier > leave_floor_gwet_ac1` |
  | 그런 문항 수 | **45 문항** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > all > tier > items_where_any_rater_leaves_floor` |
  | 클라우드 2명이 하한을 벗어난 문항에서의 AC1(확인 강도) | **0.036** | `e5_models/results/summary.json` → `h53_agreement > sentence_rep0 > cloud_only > tier > leave_floor_gwet_ac1` |

- **그림**: `experiments/e5_models/figures/h53_agreement_ac1.png`
- **반드시 함께 쓸 한계 문장**: 문항이 작다(주입 제외 61문항·주입 16건·쌍 15개). 판정자들은 같은 문항을 공유해 서로 독립이 아니고, luna 는 크레딧 소진으로 빠졌으며, 로컬 모델은 글자 확률·클라우드는 서로 다른 인터페이스라 같은 측정이 아니다.

#### E5-04 · ADD-5

- **최종 판정**: 기술 결과(관찰)
- **주장**: 상황 문장을 보는 모델 단독은 표(위험 문장 15쌍 모두 놓침, 놓침 1.00)보다 덜 놓친다. 그러나 그것은 자유 문장을 볼 때뿐이다 — 자유 문장은 지금 운영에 보내지 않고, 같은 통로가 주입에도 열려 있다(이득과 위험이 한 통로에 있다). 운영 집계 요약 맥락에서는 놓침이 표와 구별되지 않는다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 놓침 — Jev API(문장 맥락 | **0.20** | `e5_models/results/summary.json` → `add5_miss_vs_table > jev-api > sentence > answer_level > miss_rate` |
  | 놓침 — jevk5-9b-q8(문장 맥락 | **0.60** | `e5_models/results/summary.json` → `add5_miss_vs_table > jevk5-9b-q8 > sentence > answer_level > miss_rate` |
  | 놓침 — Jev API(운영 맥락) | **1.00** | `e5_models/results/summary.json` → `add5_miss_vs_table > jev-api > operational > answer_level > miss_rate` |
  | 놓침 — apus(운영 맥락) | **1.00** | `e5_models/results/summary.json` → `add5_miss_vs_table > apus-openjev-4b-q4 > operational > answer_level > miss_rate` |
  | 쌍 수 | **15 쌍** | `e5_models/results/summary.json` → `add5_miss_vs_table > jev-api > sentence > pairs` |

- **반드시 함께 쓸 한계 문장**: 초안 라벨(에이전트가 표 규칙으로 만든 것)도 모델 답도 정답이 아니다. 라벨에 기대는 값은 '초안 라벨 의존'으로만 읽는다.

#### E5-05 · C2.5

- **최종 판정**: 반증
- **주장**: 운영 요약에 거주자 특성이 아닌 관측값 두 개(재실·이상 경과 시간)를 더해도 올림 차이의 1.4%만 돌아온다(예측은 절반 이상). 하한 아래 답은 오히려 줄었다. 그래서 '관찰 필드를 더하면 모델 상향이 살아난다'는 기대는 틀렸다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 올림 합(운영 요약 + 관측값 2개) | **5 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > raised_target` |
  | 올림 차이 중 돌아온 몫 | **1.4%** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > returned_fraction` |
  | 돌아온 몫 95% 구간 하한(사례 단위 부트스트랩) | **-6.2%** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > returned_fraction_ci95_case_bootstrap > 0` |
  | 돌아온 몫 95% 구간 상한 | **+9.5%** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > returned_fraction_ci95_case_bootstrap > 1` |
  | 하한 아래 합(요약 → 요약+관측값) | **102 건** | `e5_models/results/summary.json` → `c25_structured > rep0 > pooled > below_floor_target` |
  | 예측 지지 여부 | **아니오** | `e5_models/results/summary.json` → `c25_structured > rep0 > prediction_supported` |

- **그림**: `experiments/e5_models/figures/c25_structured_observations.png`
- **반드시 함께 쓸 한계 문장**: 문항이 작다(주입 제외 61문항·주입 16건·쌍 15개). 판정자들은 같은 문항을 공유해 서로 독립이 아니고, luna 는 크레딧 소진으로 빠졌으며, 로컬 모델은 글자 확률·클라우드는 서로 다른 인터페이스라 같은 측정이 아니다.

#### E5-06 · C3.1

- **최종 판정**: 반증
- **주장**: 라벨을 보지 않은 코더가 정한 '상황 하위 종류' 코드를 운영 요약에 더해도 올림 차이의 11.3%만 돌아온다(예측은 절반 이상). 같은 코드를 조회표에 그대로 넣은 규칙 기반 판정이 모델과 같거나 낫다 — 정보는 모델이 아니라 분류 코드에 있다. 따라서 '운영 경로의 모델 상향은 사실상 쓸 수 없다'로 결론짓는다(Jev API 한 판정자는 예외지만 사후 관찰이다).
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 올림 합(운영 요약 + 하위 종류 코드) | **12 건** | `e5_models/results/summary.json` → `c31_subtype > rep0 > pooled > raised_target` |
  | 올림 차이 중 돌아온 몫 | **11.3%** | `e5_models/results/summary.json` → `c31_subtype > rep0 > pooled > returned_fraction` |
  | 돌아온 몫 95% 구간 하한 | **1.9%** | `e5_models/results/summary.json` → `c31_subtype > rep0 > pooled > returned_fraction_ci95_case_bootstrap > 0` |
  | 돌아온 몫 95% 구간 상한 | **20.0%** | `e5_models/results/summary.json` → `c31_subtype > rep0 > pooled > returned_fraction_ci95_case_bootstrap > 1` |
  | 하한 아래 합(요약 → 요약+코드) | **117 건** | `e5_models/results/summary.json` → `c31_subtype > rep0 > pooled > below_floor_target` |
  | 규칙 조회의 올림 문항 | **9 문항** | `e5_models/results/summary.json` → `rule_lookup_vs_models > rule > above_floor` |
  | 규칙 조회의 놓침(쌍 15개) | **0.40** | `e5_models/results/summary.json` → `rule_lookup_vs_models > rule > add5_answer_level > miss_rate` |
  | 같은 코드를 받은 모델들의 놓침 중앙값 | **0.93** | `e5_models/results/summary.json` → `rule_lookup_vs_models > models_median_miss` |
  | 규칙이 모델과 같거나 나은가 | **예** | `e5_models/results/summary.json` → `rule_lookup_vs_models > rule_matches_or_beats_models` |
  | Jev API 올림(코드 맥락 | **7/13 건** | `e5_models/results/summary.json` → `c31_subtype > rep0 > per_judge > jev-api > raised_target` |
  | 코더 간 일치 κ(occupied) | **0.95** | `e5_models/results/summary.json` → `coder_agreement > occupied > cohen_kappa` |
  | 코더 간 일치 κ(abnormal_for) | **0.61** | `e5_models/results/summary.json` → `coder_agreement > abnormal_for > cohen_kappa` |

- **그림**: `experiments/e5_models/figures/h51_raise_sentence_vs_operational.png`
- **반드시 함께 쓸 한계 문장**: 맹검 코더가 만든 41종 코드는 사례의 위험 구분을 사실상 다시 적은 것일 수 있고, 규칙 매핑은 맹검이 아니다. 이 코드를 운영에서 자동으로 얻을 수 있는지는 시험하지 않았다.

#### E9-00 · H5.4

- **최종 판정**: 대기
- **주장**: 클라우드 모델의 실제 종단 실행(Jev 와 luna 비교)은 아직 끝나지 않았다. 숫자 자리는 비워 둔다.
- **상태**: 대기 — `e9_cloud/results/summary.json` 이 아직 없다(숫자 없음).
- **반드시 함께 쓸 한계 문장**: 이 실험은 아직 끝나지 않아 숫자가 없다. 보고서에는 '진행 중'이라고만 쓰고 추정 숫자를 넣지 않는다.


### Ⅳ-Q6. 사다리·호출·테스트 (E7·E8)

> 등록 원문은 이 가설들을 Q1 제목 아래에 적었다. 번호(H6.x)를 따라 Q6 으로 묶었다.

#### E7-01 · H6.1

- **최종 판정**: 반증(등록 문구대로)
- **주장**: 사다리가 계산한 시각은 명세와 정확히 같다(오차 0초). 그러나 실제 데몬은 사다리 틱이 5초 간격이고 상황이 열린 순간과 정렬되지 않아 모든 사다리 발화가 0~5초 늦는다. 등록 문구('오차 1초 이상이 1건이라도')대로 반증이며, '결함 아님'이라고 다시 읽는 것은 사후 해석이다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 이상적 시계(event)에서 오차 1초 이상 | **0 건** | `e7_ladder/results/summary.json` → `conformance > by_driver > event > events_over_1s` |
  | 5초 틱 위상 0 에서 오차 1초 이상 | **0 건** | `e7_ladder/results/summary.json` → `conformance > by_driver > tick5 > events_over_1s` |
  | 틱 위상 1.7초 — 오차 1초 이상 사건 | **146 건** | `e7_ladder/results/summary.json` → `conformance > by_driver > tick5_p1.7 > events_over_1s` |
  | 틱 위상 1.7초 — 최대 오차 | **1.7 초** | `e7_ladder/results/summary.json` → `conformance > by_driver > tick5_p1.7 > max_abs_error_s` |
  | 틱 위상 3.3초 — 최대 오차 | **3.3 초** | `e7_ladder/results/summary.json` → `conformance > by_driver > tick5_p3.3 > max_abs_error_s` |

- **그림**: `experiments/e7_ladder/figures/h61_conformance.png`
- **반드시 함께 쓸 한계 문장**: 가짜 시계와 가짜 송신기이고 시나리오 16개는 명세의 대표 경로일 뿐 모든 조합이 아니다. 송신 지연이 사다리 틱을 밀어내는 효과는 E1 에서 따로 잰다.

#### E7-02 · H6.2

- **최종 판정**: 기술 결과
- **주장**: 보호자를 한꺼번에 부르는 현재 방식(동시)과 한 명씩 90초 간격으로 부르는 대안(순차) 중, 응답 모델이 같은 해석(두 방식 모두 보호자 수 N 으로 방관 효과)에서는 N≥2 모든 설정에서 순차가 첫 확인이 늦다. 순차가 늦은 것은 보호자마다 90초 대기를 정의상 더하기 때문이라 구조상 결과에 가깝다. 대신 사건당 호출받는 사람은 순차가 적다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 순차 − 동시 | **+33 초** | `e7_ladder/results/summary.json` → `mc > cells > mu1.0|q0.0|b0.0|N3 > delta_seq_minus_sim_s > point` |
  | 같은 값(평균 응답 3분·N=3) | **+59 초** | `e7_ladder/results/summary.json` → `mc > cells > mu3.0|q0.0|b0.0|N3 > delta_seq_minus_sim_s > point` |
  | 같은 값(평균 응답 10분·N=3) | **+78 초** | `e7_ladder/results/summary.json` → `mc > cells > mu10.0|q0.0|b0.0|N3 > delta_seq_minus_sim_s > point` |
  | 같은 값(평균 응답 10분·N=5) | **+131 초** | `e7_ladder/results/summary.json` → `mc > cells > mu10.0|q0.0|b0.0|N5 > delta_seq_minus_sim_s > point` |
  | 사건당 호출 인원(N=3 | **3.0 명** | `e7_ladder/results/summary.json` → `mc > cells > mu3.0|q0.0|b0.0|N3 > sim > pages` |
  | 같은 조건 순차 | **1.83 명** | `e7_ladder/results/summary.json` → `mc > cells > mu3.0|q0.0|b0.0|N3 > seq > pages` |

- **그림**: `experiments/e7_ladder/figures/h62_sim_vs_seq.png`
- **반드시 함께 쓸 한계 문장**: 보호자 응답은 지수분포 가정의 시뮬레이션이고 실제 가족의 응답 분포는 측정하지 않았다. 방관 효과 지수와 무응답 확률도 가정이며, 순차 호출은 구현하지 않고 시뮬레이션만 했다.

#### E7-03 · C2.10

- **최종 판정**: 읽기에 따라 갈림
- **주장**: 방관 효과(보호자가 많을수록 한 사람이 나서지 않음)를 '보호자 수 N'으로 계산하면 b=0.6 에서도 300초 안 확인 확률은 동시 호출이 높다(순차가 빠른 설정 0개, 동시가 높은 설정 24/27). '동시에 호출된 사람 수'로 계산하면 b=0.6 에서 순차가 27/27 설정에서 높다. 결론은 어느 해석을 쓰느냐에 달렸고 실제 가족의 반응은 재지 않았다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 해석 N | **24/27 설정** | `e7_ladder/results/summary.json` → `mc > c2_10 > N_scaled_both > b0.6 > sim_higher_ci_separated` |
  | 해석 N | **0/27 설정** | `e7_ladder/results/summary.json` → `mc > c2_10 > N_scaled_both > b0.6 > seq_higher_ci_separated` |
  | 해석 N | **-8.0%p** | `e7_ladder/results/summary.json` → `mc > c2_10 > N_scaled_both > b0.6 > median_diff_p300_seq_minus_sim` |
  | 해석 동시 호출 수 | **27/27 설정** | `e7_ladder/results/summary.json` → `mc > c2_10 > concurrent_scaled_seq > b0.6 > seq_higher_ci_separated` |
  | 해석 동시 호출 수 | **+15.6%p** | `e7_ladder/results/summary.json` → `mc > c2_10 > concurrent_scaled_seq > b0.6 > median_diff_p300_seq_minus_sim` |
  | 해석 동시 호출 수 | **18/27 설정** | `e7_ladder/results/summary.json` → `mc > c2_10 > concurrent_scaled_seq > b0.3 > seq_higher_ci_separated` |
  | 해석 동시 호출 수 | **4/27 설정** | `e7_ladder/results/summary.json` → `mc > c2_10 > concurrent_scaled_seq > b0.3 > sim_higher_ci_separated` |

- **그림**: `experiments/e7_ladder/figures/h62_sim_vs_seq.png`
- **반드시 함께 쓸 한계 문장**: 보호자 응답은 지수분포 가정의 시뮬레이션이고 실제 가족의 응답 분포는 측정하지 않았다. 방관 효과 지수와 무응답 확률도 가정이며, 순차 호출은 구현하지 않고 시뮬레이션만 했다.

#### E8-01 · H6.3

- **최종 판정**: 지지(반증 실패)
- **주장**: 줄 커버리지가 모두 95% 이상인 안전 핵심 모듈 10개 중 7개가 뮤테이션 점수 90% 미만이다. 사전 등록 반증 조건('대상 모듈 모두 90% 이상')에 걸리지 않았으므로 H6.3 은 지지다. 핵심 연산자 450개의 점수는 80.0%이다. 커버리지(테스트가 그 줄을 지나갔는가)가 높아도 뮤테이션 점수(그 줄이 틀리면 테스트가 알아채는가)는 모듈마다 크게 다르다. 가장 낮은 것은 notifier·llm_judge·intervention 이고, Gate1 결정론 게이트(guardrail)와 executor 는 거의 다 잡는다 — 커버리지가 높으면 안심도 아니고 낮다도 아니며 모듈마다 다르다. 이 가설은 등록 때 품질 확인이라 연구 결론에서 제외로 적혀 있었다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 핵심 연산자 뮤테이션 점수(대상 10개 합) | **80.0%** | `e8_mutation/results/summary.json` → `overall > core > score_targeted` |
  | 95% 구간 하한 | **76.2%** | `e8_mutation/results/summary.json` → `overall > core > score_targeted_ci95 > 0` |
  | 95% 구간 상한 | **83.8%** | `e8_mutation/results/summary.json` → `overall > core > score_targeted_ci95 > 1` |
  | 가장 낮은 모듈의 줄 커버리지 | **95.9%** | `e8_mutation/results/summary.json` → `modules > * > coverage_pct` (min) |
  | 점수가 90% 미만인 모듈(커버리지 95% 이상 중) | **7/10 모듈** | `e8_mutation/results/summary.json` → `h63_modules_low_score_despite_coverage` (len) |
  | 반증 조건('모두 90% 이상') 해당 여부 | **아니오** | `e8_mutation/results/summary.json` → `h63_all_modules_ge_90` |
  | 변이 수(핵심 연산자) | **450 개** | `e8_mutation/results/summary.json` → `overall > core > n` |
  | 변이 수(추가 연산자 포함 전체) | **742 개** | `e8_mutation/results/summary.json` → `overall > all > n` |
  | notifier 점수(가장 낮은 쪽) | **56.25%** | `e8_mutation/results/summary.json` → `modules > liot/layers/l4_execution/notifier.py > score_core` |
  | llm_judge 점수 | **57.1%** | `e8_mutation/results/summary.json` → `modules > liot/layers/l3_judgment/llm_judge.py > score_core` |
  | guardrail(Gate1) 점수(반례) | **100.0%** | `e8_mutation/results/summary.json` → `modules > liot/layers/l3_judgment/guardrail.py > score_core` |
  | executor 점수(반례) | **97.5%** | `e8_mutation/results/summary.json` → `modules > liot/layers/l4_execution/executor.py > score_core` |

- **그림**: `experiments/e8_mutation/figures/coverage_vs_mutation.png`
- **반드시 함께 쓸 한계 문장**: 측정 대상은 U1/U3 변경 전 코드다(변경 뒤에는 다시 돌려야 한다). 변이기는 AST 로 직접 만든 것이고 한 번에 한 곳만 바꾸며 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람이 코드를 읽고 한 판단이고, 변이가 같은 줄에 뭉쳐 있어 구간은 실제보다 좁다. 줄 커버리지만 쟀다(분기 아님).

#### E8-02 · H6.3 (가정에 따른 흔들림)

- **최종 판정**: 지지(개수는 가정에 따라 크게 흔들림)
- **주장**: 90% 미만인 모듈의 개수는 가정에 따라 7 → 4 → 2 로 줄어든다(핵심 연산자·직접 관련 테스트 → 동치 변이를 분모에서 뺌 → 추가 연산자까지 포함하고 동치 제외). '하나 이상'이라는 판정은 어느 가정에서도 유지되지만 개수는 하나의 숫자로 쓰지 않는다. 동치 판정은 사람이 코드를 읽고 한 것이며 증명이 아니다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 핵심 연산자·직접 관련 테스트(사전 등록 방식) | **7/10 모듈** | `e8_mutation/results/summary.json` 의 `modules` 칸들에서 계산(파생값) — 파생값 — 모듈별 summary.json 칸에서 계산 |
  | 핵심 연산자·동치 변이를 분모에서 뺌 | **4/10 모듈** | `e8_mutation/results/summary.json` 의 `modules` 칸들에서 계산(파생값) — 파생값 — 모듈별 summary.json 칸에서 계산 |
  | 추가 연산자 포함·동치 제외 | **2/10 모듈** | `e8_mutation/results/summary.json` 의 `modules` 칸들에서 계산(파생값) — 파생값 — 모듈별 summary.json 칸에서 계산 |
  | 동치로 판정한 변이 | **35 개** | `e8_mutation/results/summary.json` → `survivor_class_counts > equivalent` |

- **반드시 함께 쓸 한계 문장**: 측정 대상은 U1/U3 변경 전 코드다(변경 뒤에는 다시 돌려야 한다). 변이기는 AST 로 직접 만든 것이고 한 번에 한 곳만 바꾸며 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람이 코드를 읽고 한 판단이고, 변이가 같은 줄에 뭉쳐 있어 구간은 실제보다 좁다. 줄 커버리지만 쟀다(분기 아님).

#### E8-03 · H6.3 (살아남은 변이의 분류)

- **최종 판정**: 지지 — 안전 관련 빈틈 15곳(후속 테스트 대상)
- **주장**: 전체 테스트로도 안 잡힌 살아남은 변이 95개를 사람이 읽고 나누면 동치 35, 테스트 없는 동작 45, 안전 관련 빈틈 15이다. 안전 관련 빈틈 15는 '그 줄이 지금 사고를 낸다'는 뜻이 아니라 '틀려져도 테스트가 알려 주지 못한다'는 뜻이다(egress_gate 6·severity 6·llm_judge 1·executor 1·notifier 1). 진짜 하드 게이트 위반은 이 실험에서 관찰되지 않았다. 이 15곳에 대한 테스트는 다른 가지에서 쓰는 중이라 이 자료집의 숫자는 테스트 추가 전 상태다.
- **숫자와 출처**:

  | 숫자 | 값 | summary.json 경로 |
  |---|---|---|
  | 전체 테스트로도 살아남은 변이 | **95 개** | `e8_mutation/results/summary.json` → `survivors_after_full_suite` |
  | 안전 관련 빈틈 | **15 곳** | `e8_mutation/results/summary.json` → `survivor_class_counts > safety_gap` |
  | 테스트 없는 동작 | **45 개** | `e8_mutation/results/summary.json` → `survivor_class_counts > untested` |
  | 하네스 점검 카나리 변이가 모두 잡혔나 | **예** | `e8_mutation/results/summary.json` → `validation > canary_all_killed` |
  | 하네스 점검 운영 코드가 변이 전후 동일한가 | **예** | `e8_mutation/results/summary.json` → `validation > integrity_all_equal` |
  | 부하로 120초에 걸린 시험 중 재판정에서 살아남은 것 | **6/9 건** | `e8_mutation/results/summary.json` → `timeouts > recheck_outcomes > survived` |

- **반드시 함께 쓸 한계 문장**: 측정 대상은 U1/U3 변경 전 코드다(변경 뒤에는 다시 돌려야 한다). 변이기는 AST 로 직접 만든 것이고 한 번에 한 곳만 바꾸며 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람이 코드를 읽고 한 판단이고, 변이가 같은 줄에 뭉쳐 있어 구간은 실제보다 좁다. 줄 커버리지만 쟀다(분기 아님).


## Ⅴ. 고찰

### Ⅴ-1. LIOT 가 지는 곳

이 절은 각주가 아니라 결과의 절반이다(18항목). 아래 항목은 모두 실험에서 나온 LIOT 에 불리한 결과이며, 각 줄의 숫자는 위 청구(번호)와 같은 값이다.

1. **같은 상한을 씌우면 LIOT 실제의 낮은 피해가 사라진다 (E3-02).** 모두 명시 확인에도 같은 능동 상한(땅거미 이상 침묵)을 씌우면, LIOT 실제는 오분류 10%에서 해로운 실행률이 +3.2%p 높고(96% 세트에서 더 나쁨, 세트 사분위 +2.1%p~+4.4%p), 오분류가 없을 때는 사실상 동률이다(평균 차이 -0.2%p; 세트로 세면 더 나쁨 16.3%·동률 35.8%·더 좋음 48.0%). 이기는 쪽은 도움 완료(+12.5%p)와 부담(하루 -2.40 건)이다. 즉 낮은 피해는 단계 매김의 이점이 아니라 침묵에서 온다.
2. **4단계 전부(LIOT 가상)는 모두 명시 확인보다 해롭다 (E3-03).** 오분류 10%에서 세트의 68.5%에서 해로운 실행률이 유의하게 더 높고, 낮은 세트는 4.8%뿐이다. 원인은 잔잔으로 잘못 내려간 해로운 제안의 자동 실행이다(E3-09: 알림만으로 바꾸면 62.8% → 1.3%, 대신 도움 완료가 -10.8%p).
3. **잔잔 쪽 해로움이 약 20% 만 되어도 liot_real 의 피해 이점이 사라진다 (E3-10).** 잔잔 50%에서 이점 면적이 8.5%~5.0%로 떨어지고, 모두 명시 확인보다 유의하게 더 해로운 세트가 85.0%다. 단 50% 이상 구간의 큰 격차는 정의에 가까운 구조상 결과(잔잔은 자동 실행)라 발견으로 쓰지 않고, 정보가 되는 것은 이점이 사라지는 교차점(잔잔 약 16~20%)이다. 현실의 잔잔 비율은 모른다.
4. **탭 한 번이 더 쉽게 승인되면 LIOT 가상의 열세가 깊어진다 (E3-11).** 2.0배에서 유의하게 더 해로운 세트가 79.75%(m=10%)이지만 기준선(1.0배)에서도 이미 과반이었다. 발견은 한계 효과다: 피해 차이가 +3.1%p에서 +4.6%p로 커진다.
5. **위험 점수가 AUC 0.8 쯤만 돼도 단순한 θ 정책이 4단계를 이긴다 (E3-12).** LIOT 단계 자체의 AUC 는 0.68(오분류 0)·0.63(10%)이고, 잡음 없는 점수(AUC 0.82)는 오분류 10%에서 세트의 57.5%에서 liot_full 을 지배한다.
6. **능동 상한이 실제로 잃는 것 (E3-06).** 도움 제안의 45.8%가 사라지고(가전·출입이 대부분), 도움 완료율이 LIOT 가상보다 -15.0%p 낮다.
7. **미등록 취약 가구가 있으면 정직한 모델의 최종값 방식이 낫다 (E2-04).** 오경보 2%·미등록 10%·정직도 0.9·공격 0 에서 최종값은 오호출 2.49%(LIOT 0.61%), 놓침 0.69%(LIOT 6.93%). 정직도 1.0 이면 놓침이 0.00%로 줄고 오호출은 같다. 다만 공격이 있거나(c\* 2.41) 모델이 취약성을 못 보면(운영 모델은 게이트 3 때문에 못 본다) 판이 바뀐다.
8. **LIOT 의 첫 알림 구조는 푸시를 더 보낸다 (E1-03·E1-05).** 경고 하한 사건의 26.7%에서 두 번째 푸시가 나간다. 하한을 아는 대안은 약 +955.0 ms 늦게 보내는 대신 푸시를 한 번으로 줄인다.
9. **게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류 (E2-03).** (a) 경고 하한 상황을 거절한 뒤 다시 열면 38 건에서 첫 알림이 없고 600 초 뒤 재알림으로 처음 나간다. (b) 첫 송신이 실패하면 복구가 즉시여도 첫 푸시가 사다리 시각까지 기다린다(최소 60 초, 중앙 600 초). 소유자 판단이 필요하다. 무작위 퍼징의 '위반 0'(E2-01)은 이 경로를 한 번도 만들지 못한 결과였다.
10. **느린 송신은 다음 안전 경보를 늦춘다 (E1-04).** 보호자 3명·송신 10초이면 두 번째 안전 신호의 첫 알림이 약 29.925 초 늦는다.
11. **사다리는 명세보다 0~5초 늦는다 (E7-01).** 등록 문구대로 반증이다(위상이 어긋난 틱에서 146 건의 사건이 1초 넘게 늦음).
12. **방관 효과를 '동시에 호출된 사람 수'로 보면 순차 호출이 낫다 (E7-03).** b=0.6 에서 순차가 높은 설정이 27/27 설정. 반대로 '보호자 수'로 보면 동시가 낫다(24/27 설정). LIOT 의 현재 방식(동시)이 늘 낫다고 쓸 수 없다.
13. **조회표는 곱셈 점수의 최선 설정보다 등급을 더 뭉갠다 (E4-01).** 동점 손실 표 32 쌍 대 곱셈 최선 14 쌍. 단 표를 24칸 전부 재현하는 곱셈 설정은 16 개뿐이고, 더 잘게 나눈다고 더 옳은 것은 아니다.
14. **운영에서는 결정 모델이 위험을 올릴 정보가 사실상 없다 (E5-01·E5-05·E5-06).** 운영 요약에서는 올림이 75 건에서 4 건로 사라지고, 관측값 두 개를 더해도 1.4%만, 맹검 코더의 종류 코드를 더해도 11.3%만 돌아온다. 같은 코드를 조회표에 넣은 규칙이 모델과 같거나 낫다(규칙 놓침 0.40 대 모델 중앙값 0.93). 모델 상향 기능은 현재 운영 입력으로는 쓸모가 거의 없고, 위험 정보는 '분류 코드'에 있다.
15. **보호자 호출에서 모델 상향의 이득이 작다는 결론은 읽기에 달렸다 (E2-05).** 현재 운영 지점에서 최종값은 오호출을 +1.9%p 늘리고 놓침은 -0.75%p 줄인다. 두 읽기의 결론이 다르다.
16. **추론 누출 1비트 (E2-02).** 위험 라벨이 취약 거주자의 존재를 폰에 드러낸다. 하드 게이트 위반은 아니지만 설계가 알아야 한다.
17. **클라우드 대 로컬의 우열은 가르지 못했다 (E5-02).** 구간이 겹치고 luna 는 돌리지 못했다.
18. **LIOT 자신의 안전 테스트에 빈틈이 있다 (E8-01~03).** 줄 커버리지가 모두 95.9% 이상인데도 안전 핵심 모듈 7/10 모듈가 뮤테이션 점수 90% 미만이다(핵심 연산자 점수 80.0%, 구간 76.2%~83.8%). 가정에 따라 90% 미만 모듈은 7/10 모듈 → 4/10 모듈 → 2/10 모듈 로 흔들린다. 틀려져도 테스트가 알려 주지 못하는 안전 관련 빈틈이 15 곳곳(egress_gate·severity 가 각 6)이며, 그 줄이 지금 사고를 낸다는 뜻은 아니다. 반례: guardrail(100.0%)과 executor(97.5%)는 변이를 거의 다 잡는다. 이 빈틈의 테스트는 다른 가지에서 쓰는 중이다.

### Ⅴ-2. 구조상 결과로 따로 둔 것 (비교 결론에 쓰지 않음)

Ⅲ-4절의 표가 목록이다. 보고서에서는 '확인했다'로만 쓴다. 특히 다음 세 문장은 쓰지 않는다: "LIOT 의 하드 게이트 위반은 0 이다"(→ 무작위 퍼징 범위 안에서 0 이고 알려진 게이트 1 충돌이 두 종류 있다), "LIOT 단계 매김이 외부 기준에서도 정보가 있다"(→ 생성기와 단계의 정렬에서 나온 구조상 결과), "모델이 운영에서 올림을 줄였다"(→ 입력에 내용이 없어 줄 수밖에 없었다).

### Ⅴ-3. 사후 변경과 그 이유

아래는 `HYPOTHESES.md` 의 '사후 변경 기록'을 한 곳에 모은 것이다. **모두 결과를 본 뒤의 판단이다.** 이 표에 없는 판정 변경은 없다.

| 시각 | 무엇을 바꿨나 | 왜 |
|---|---|---|
| 11-12 02:20 | 등록 시각 정정('02:00' → 실제 커밋 01:31:40). 사이클 1 결과 커밋(E4 01:45·E5 01:47·E3 01:59)이 모두 등록 뒤임을 확인 | 기록 오류 정정. 단 E4 의 모델 단독 원시 응답과 E5 의 상황 문장 응답은 2026-10-19 벤치 자료의 재분석이라 등록 전 자료 |
| 사이클 1 후(시각 미기재) | v1 기준 인용 정정: v1 은 커밋되지 않았으므로 RESULTS 가 인용한 v1 기준(κ≥0.6 등)과 등록에 없는 기준은 모두 '사후 기준' | 사후 기준이 등록 기준처럼 읽히는 것을 막으려고 |
| 02:20 | **H2.2 판정**: liot_real 이 점검을 통과하지 못해(세트의 97.8%에서 모두 명시 확인보다 낮음) liot_real 관련 E3 결론(H2.1 의 liot_real 부분, P4 의 '막은 피해', H2.4)을 '판정 불가·구조상 결과'로 내림. liot_full 의 통과도 '약한 통과' | 등록 규칙이 점검 실패를 그렇게 정했다. 한때 이를 '사후 해석 메모'로 처리하라고 지시한 것은 오케스트레이터의 잘못이며 이 항목으로 바로잡음 |
| 사이클 1 후(시각 미기재) | H5.1 을 '지지'가 아니라 구조상 결과로 표기 | 운영 맥락에는 상황 내용이 없어 올림이 0 에 가까운 것은 입력에서 정해진 결과 |
| 02:29 | E1·E2·E7 판정 표기: H1.1 은 구조상 결과, H1.2(U1 전)는 '재현'(판정은 U1 후 측정), H6.2·ADD-6 은 기술 결과, **H6.1 은 등록 문구대로 반증**(사다리 5초 해상도). '결함 아님'으로 다시 읽은 것은 사후 해석. 시나리오 S14 기대값을 실행 뒤 390초 → 510초로 고침(bbfd29d) | 타당성 검토 지적 반영. S14 기대값 수정은 서비스 동작이 아니라 명세 해석('전달 5분 뒤')을 고친 것 |
| 02:29 | **E2 헤드라인 정정**: '하드 게이트 위반 0' → '무작위 퍼징 범위 안에서 0, 알려진 게이트 1 충돌 1종(경고 상황 거절 뒤 억제)' | 무작위 퍼징이 그 경로를 한 번도 만들지 못했을 뿐이었다 |
| 03:05 | **C2.8 반증 조건의 수량어**: '몇 칸에서'가 없어 두 읽기(한 칸이라도 / 과반 칸)를 모두 보고, 게이트 3 을 지키는 '못 보는 모드'도 계산에 넣음. 결론을 c\* 가 아니라 운영 지점의 절대 차이로 씀 | 결과를 본 뒤 하나를 고르는 것을 피하려고 |
| 03:05 | C2.7 을 '지지'가 아니라 구조상 결과(공정화 확인)로 표기 | 하한을 아는 대안은 위험 하한에서 운영 경로를 그대로 타므로 두 예측이 코드상 반드시 성립 |
| 사이클 2 | E3: 같은 상한을 모든 정책에 씌우는 비교(N1)와 생성기 상한에도 같은 오분류 m 을 적용하는 보정(N2)을 추가. 적용 전 결과(m=10% 에서 LIOT 단계 상한이 더 나쁘게 나옴)는 '구조상 결과'로 표시하고 새 표로 대체 | 두 상한을 다르게 취급했기 때문 |
| 사이클 2 | C2.1 의 다이얼 편향(해로움이 잔잔에 한 건도 놓이지 않음·탭 한 번 = 명시 확인)을 푸는 C2.11 을 실행 전에 등록(02:58)하고 실행 | 검토 지적 N3: 두 설정이 모두 LIOT 에 유리했다 |
| 사이클 2~3 | C2.5 반증으로 결론이 바뀌어 사이클 3(C3.1) 등록. 맹검 코더를 쓰고, 규칙 조회 대안을 실행 전에 커밋(a4fafb7). 영아 코드는 Gate 3 때문에 치환 코드를 주 팔로 사용 | 코드가 '표 확장'일 수 있다는 대안 해석을 시험하려고 |
| 최종 재검증 | **E3 동률 처리**: 합계만 저장한 차선의 값을 씨앗별로 펼쳐 다시 더하면 마지막 자리가 달라져 정확한 동률이 아주 작은 차이로 바뀌던 것을 고쳤다(합계를 정수로 비교, 1e-9 미만은 0 으로 접어 동률을 별도 범주로 셈). 바뀐 숫자: 같은 상한 비교 m=0 의 'LIOT 실제가 더 나쁜 세트' 21% → 16.3%(동률 35.8%·더 좋음 48.0%), C2.4 의 m=0 지배 비율 13.3% → 14.8%(κ=0), m=10% 값 96% → 95.8% | 구현 오차가 동률을 가렸다. 이전 판의 '차이 없음' 면적 일부는 동률이었다 |
| 최종 재검증 | **C2.11 해석 보정**: (a) 잔잔 50% 이상 구간의 큰 손실은 정의에 가까운 구조상 결과이고 정보는 교차점(약 16~20%)이라고 고침. (b) m=10% 의 '2.0배에서 과반'은 기준선에서 이미 충족이라 예측이 맞았다기보다 배수와 무관한 값이고 발견은 한계 효과(+1.5%p)라고 고침. 판정 말('지지')은 RESULTS.md 그대로 두되 의미를 좁힘. 또 C2.11 의 실행 전 등록 시각을 '02:58'에서 커밋 449c653·03:00:13 으로 정정. 해로움의 심각도는 가중하지 않고 건수로만 센다는 한계를 추가 | 검토 지적: 두 설정이 LIOT 에 유리했고, 큰 손실 구간을 발견처럼 읽을 수 있었다 |
| 03:45 | **E6 H4.2 회복 지표(결과를 본 뒤)**: 등록한 '80% 로 돌아오기까지 일수'가 선호 변화가 도움 장면을 더하기만 할 때 거의 정의상 충족(최솟값 6일)된다는 것을 보고 '바뀐 장면의 재수락' 기준을 추가. 추가한 형태는 사후 기준이고 '약한 형태는 지지'라는 판정은 쓰지 않음. **E6 H4.3**: '피로 0 이어도 차이가 남으면 결함'은 H2.2 의 문구이고 H4.3 의 등록 내용은 분모 규칙뿐이라 정정, 피로·노력 비용 0 대조의 차이 0 은 구조상 결과 | E6 는 아직 검토 중이라 판정은 대기. 사후 변경 두 건만 먼저 기록 |
| 03:50 | **E8 위치**: H6.3 은 등록 때 '품질 확인 — 연구 결론에서 제외'였으나 결과가 도착해 지지(반증 실패)로 판정됨. 측정 대상은 U1/U3 변경 전 코드이고, 안전 관련 빈틈 15곳의 테스트는 다른 가지에서 작성 중 | 등록 반증 조건('대상 모듈 모두 90% 이상')에 걸리지 않음. 개수는 가정에 따라 7→4→2 로 흔들려 하나의 숫자로 쓰지 않는다 |
| 사이클 2 | E4: 모델 단독의 단조성 위반 누락을 검토에서 지적받아 C2.6 에서 '표 기준 표시'와 함께 보고 | 검토 FAIL 반영 |

### Ⅴ-4. 한계 총정리

- **L-SIM** (M-03, E3-01, E3-09, E3-14): 거주자가 알림을 읽고 판단하는 모델(피로·무심코 승인·정확도·응답 지연)은 전부 가정이고 사람 데이터로 맞춘 값이 아니다. 결론은 '가정한 범위 안에서'만 읽는다.
- **L-GEN** (E3-02, E3-04, E3-05, E3-06, E3-08, E3-12, E3-13): 시뮬레이터가 만든 해로운 제안의 약 96%가 LIOT 자신의 단계(땅거미·불씨)에 놓이는 것은 생성기를 우리가 쓴 결과이지 세상에서 잰 값이 아니다. 이 정렬에 기대는 수치는 구조상 결과다.
- **L-GEN-LIGHT** (E3-07, E3-10, E3-11): 생성기의 해로움 규칙과 견본, 그리고 해로운 제안이 어느 단계에 놓이는지를 정하는 노브는 우리가 정했다. 잔잔 쪽 해로움은 한 견본(조명 끄기)에 몰려 있어 다른 잔잔 견본은 시험하지 않았다.
- **L-SEEDNOISE** (E3-03): 구간은 같은 매개변수 점 안에서 씨앗만 다시 뽑은 '씨앗 잡음'이다. 가정(매개변수)의 불확실성은 어느 구간에도 들어 있지 않고, 세트 사이의 변동은 사분위로만 보였다.
- **L-LAT** (M-01, E1-02, E1-04): 모델 지연(0.2·1.5·4초)·올림 확률 30%·마감 2초는 가정이고, 송신기는 가짜이며, 시간은 부하가 높은 한 대의 기계에서 번갈아 잰 상대 비교다. 폰까지 닿는 실제 시간은 재지 않았다.
- **L-LAT-ALT** (E1-01, E1-05): 비교 대안은 운영 서비스를 상속한 얇은 래퍼이고, 지연의 차이는 '대안이 모델을 기다리도록 정의됐다'는 데서 나온다. 모델 응답의 내용은 재생하지 않았고 올림 여부는 가정이다.
- **L-REPLAY** (E1-03): 벤치 재생은 상황 사례 32건뿐이고 맥락 문장이 운영과 다르다. 모델은 정답이 아니므로 '불일치'는 '오류'가 아니다.
- **L-FUZZ** (M-02, E2-01, E2-02, E2-03): '위반 0'은 무작위 퍼징이 만든 경로 안에서의 0이다. 경로를 강제로 채운 퍼징에서는 게이트 1(안전 알림이 늦거나 침묵하지 않는다)과 부딪히는 알려진 문제 두 종류가 잡혔다. 재시작·동시성의 모든 조합은 덮지 않았다.
- **L-CSTAR** (E2-04, E2-06): 모델 정직도·공격 비율·센서 오경보율·미등록 취약 가구 비율은 모두 가정이고 범위를 쓸어 보았을 뿐이다. 최종값 방식과 추론 변형은 서비스에 구현하지 않고 결정 수준에서 시뮬레이션했다.
- **L-C28** (E2-05): 탐지기 문턱(오탐 상향률 5%)과 '관찰 필드로 드러나는 위험' 비율은 가정이고, AUC 는 정규분포 가정의 요약이다. 반증 조건에 '몇 칸에서'가 없어 두 읽기를 모두 적었으므로, 결론은 읽기에 달렸다.
- **L-LADDER** (M-06, E7-01): 가짜 시계와 가짜 송신기이고 시나리오 16개는 명세의 대표 경로일 뿐 모든 조합이 아니다. 송신 지연이 사다리 틱을 밀어내는 효과는 E1 에서 따로 잰다.
- **L-BYSTANDER** (E7-02, E7-03): 보호자 응답은 지수분포 가정의 시뮬레이션이고 실제 가족의 응답 분포는 측정하지 않았다. 방관 효과 지수와 무응답 확률도 가정이며, 순차 호출은 구현하지 않고 시뮬레이션만 했다.
- **L-E4-NOGT** (M-04, E4-01, E4-02, E4-03): 위험 등급에는 독립된 정답이 없다. 우리 조회표를 기준점으로 쓴 값('표 기준')은 순환적이고, 곱셈 점수의 '몇 %'는 우리가 고른 척도·절단점 범위에 따라 달라진다.
- **L-E4-MODEL** (E4-04): 모델 단독 표본은 작다(상황 8종·주입 8건). 모델 5개는 같은 입력 61건을 공유해 서로 독립이 아니다. 평온/위험 문장 짝은 사람이 아닌 에이전트가 설계했다.
- **L-E5-SMALL** (M-05, E5-02, E5-03, E5-05): 문항이 작다(주입 제외 61문항·주입 16건·쌍 15개). 판정자들은 같은 문항을 공유해 서로 독립이 아니고, luna 는 크레딧 소진으로 빠졌으며, 로컬 모델은 글자 확률·클라우드는 서로 다른 인터페이스라 같은 측정이 아니다.
- **L-E5-STRUCT** (E5-01): 운영 집계 요약에는 상황 내용이 없어 평온 문장과 위험 문장이 같은 입력이 된다. 그래서 올림이 0 에 가까운 것은 실험이 발견한 것이 아니라 입력에서 정해진 구조상 결과다.
- **L-E5-DRAFT** (E5-04): 초안 라벨(에이전트가 표 규칙으로 만든 것)도 모델 답도 정답이 아니다. 라벨에 기대는 값은 '초안 라벨 의존'으로만 읽는다.
- **L-E5-CODER** (E5-06): 맹검 코더가 만든 41종 코드는 사례의 위험 구분을 사실상 다시 적은 것일 수 있고, 규칙 매핑은 맹검이 아니다. 이 코드를 운영에서 자동으로 얻을 수 있는지는 시험하지 않았다.
- **L-E8** (M-07, E8-01, E8-02, E8-03): 측정 대상은 U1/U3 변경 전 코드다(변경 뒤에는 다시 돌려야 한다). 변이기는 AST 로 직접 만든 것이고 한 번에 한 곳만 바꾸며 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람이 코드를 읽고 한 판단이고, 변이가 같은 줄에 뭉쳐 있어 구간은 실제보다 좁다. 줄 커버리지만 쟀다(분기 아님).
- **L-PENDING** (E6-00, E9-00): 이 실험은 아직 끝나지 않아 숫자가 없다. 보고서에는 '진행 중'이라고만 쓰고 추정 숫자를 넣지 않는다.

### Ⅴ-5. 아직 소유자가 정해야 하는 것

- 게이트 1 과 부딪히는 두 문제(경고 상황 거절 뒤 억제, 첫 송신 실패 뒤 지연)를 어떻게 다룰지(E2-03). 소유자에게 보고됐고 `docs/tracking/findings.md` 에 기록됐다.
- 위험 라벨이 드러내는 1비트 추론 누출을 허용할지(E2-02).
- 사다리 틱 해상도 5초(설계 상수)를 둘지(E7-01).
- E8 이 찾은 안전 관련 빈틈 15곳의 테스트 추가(진행 중인 다른 가지)와, U1/U3 변경 뒤 뮤테이션 재측정(E8-03). 죽은 상수(`notifier.py` 의 두 상수) 정리 여부.

## Ⅵ. 결론

### 결론에 쓸 수 있는 문장

1. LIOT 는 설계 선택마다 대안과 같은 조건에서 견줬고, **LIOT 가 지는 곳을 18가지로 분리해 보고했다**(Ⅴ-1절).
2. 판정을 사전 등록했고, 결과를 본 뒤의 변경은 시각·이유와 함께 모두 공개했다(Ⅴ-3절). 사후 변경 중에는 우리 지시의 오류를 바로잡은 것도 있다.
3. 현재까지 결과가 나온 가설·사이클 37개 중 6개가 지지됐고 5개가 반증됐다. 반증에는 우리 예측이 틀린 것(C2.1·C2.5·C3.1·H6.1)이 들어 있다. 6개는 판정하지 못했고, 4개는 구조상 결과, 14개는 기술 결과, 2개는 읽기에 따라 갈렸으며, 4개는 진행 중이다.
4. **운영 경로의 안전은 표와 규칙이 결정하고 모델이 하는 일은 거의 없다.** 이것은 설계 의도이지만, 동시에 '모델 상향이 가치를 만든다'는 주장은 이 연구가 지지하지 못한다(C2.5·C3.1 반증, 규칙 조회가 같거나 나음).
5. 능동 상한(땅거미 이상 침묵)은 해로운 실행을 줄이지만 그 몫은 침묵에서 오며 도움 제안의 약 절반을 잃는다. 단계별 확인 자체의 피해 이점은 같은 상한을 씌우면 확인되지 않는다.
6. 게이트 1 에 부딪히는 알려진 문제 두 종류가 남아 있고, 소유자 판단이 필요하다.

### 결론에 쓰면 안 되는 문장

- "LIOT 는 모든 대안보다 낫다" / "LIOT 의 하드 게이트 위반은 0 이다" / "모델이 올림을 줄였다" / "클라우드 모델이 더 안전하다" / "LIOT 단계 매김이 외부 기준에서도 정확하다" — 위 판정과 한계 어느 것으로도 뒷받침되지 않는다.
- 시뮬레이터 결과를 '실제 가정의 결과'처럼 쓰는 모든 문장. 거주자·보호자 반응은 가정이다.
- 대기 중인 실험(E6 적응, E9 클라우드 종단)에 대한 문장. 숫자가 없다. E6·E9 는 검토 중이다.

## 부록 A. 용어집 (비개발자용)

| 말 | 뜻 |
|---|---|
| LIOT | 한 집에서 로컬로 돌아가는 능동형 스마트홈 판단 시스템. 사람이 승인해야 실행한다 |
| 하한(floor) | 표와 취약 거주자 여부로 정해지는, 어떤 모델도 내릴 수 없는 최저 위험 등급 |
| 올림 | 모델이 하한보다 높은 등급으로 답하는 것(모델은 올릴 수만 있다) |
| 4단계 | 확인 강도 네 단계: 잔잔(확인 없이 실행)·숨결(탭 한 번)·땅거미(명시 확인)·불씨(이중 확인) |
| 능동 상한 | LIOT 가 먼저 제안할 때 땅거미 이상의 제안은 하지 않고 침묵하는 규칙 |
| 모두 명시 확인 | 모든 제안에 같은 확인을 요구하는 대안 방식 |
| 이진 승인 | 민감한 행동만 확인하고 나머지는 자동 실행하는 대안 방식 |
| 맞교환(트레이드오프) | 하나가 좋아지면 다른 하나가 나빠지는 관계 |
| c\* (손익분기 비용비) | '놓침 1건이 오호출 몇 건만큼 나쁜가'. 놓침이 c\* 배보다 더 나쁘다고 보면 최종값 방식이 낫다 |
| 오호출 / 놓침 | 필요 없는데 가족을 부른 것 / 필요한데 부르지 못한 것 |
| AUC | 점수가 해로운 것과 아닌 것을 가르는 정도. 0.5 는 동전 던지기, 1.0 은 완벽 |
| 95% 구간 | 같은 실험을 다시 하면 값이 이 안에 들어올 범위. 0 을 포함하면 '차이가 있다고 말할 수 없다' |
| 씨앗(seed) | 난수의 출발값. 같은 씨앗이면 모든 정책이 같은 사건을 겪는다 |
| 씨앗 잡음 | 같은 조건에서 씨앗만 바꿔 생긴 흔들림. 가정이 틀릴 가능성은 포함하지 않는다 |
| 사분위(Q25~Q75) | 값을 줄 세웠을 때 가운데 절반이 걸치는 범위 |
| 퍼징 | 무작위·강제 입력을 쏟아 넣어 규칙이 깨지는지 보는 시험 |
| 하드 게이트 | 어떤 조건에서도 깨지면 안 되는 안전 규칙 5개(경보가 늦지 않음, 하한은 오르기만 함 등) |
| 구조상 결과 | 정답이 우리 표·규칙·정의에서 나오는 결과. 비교 결론에 쓰지 않는다 |
| 사후 변경 | 결과를 본 뒤 기준·판정·표기를 바꾼 것. 시각과 이유를 적어 공개한다 |
| 사전 등록 | 실험을 돌리기 전에 가설·판정 기준을 커밋해 두는 것 |
| 맹검 코더 | 정답 라벨과 결과를 보지 않고 사례를 분류한 별도 에이전트 |
| 시뮬레이터 | 거주자 반응을 가정해 30일을 가상으로 돌리는 프로그램. 결과는 가정 범위 안에서만 읽는다 |
| 벤치 | 모델에게 같은 문항을 물어 답을 모은 시험지(61문항) |

## 부록 B. 진행 중인 실험 (대기)

- **e6_personalization** (H4.1 · H4.2 (= P5) · H4.3): 대기. 개인화(학습·선호 변화·30일 거절 게이트) 실험은 아직 끝나지 않았다. 숫자 자리는 비워 둔다.
- **e9_cloud** (H5.4): 대기. 클라우드 모델의 실제 종단 실행(Jev 와 luna 비교)은 아직 끝나지 않았다. 숫자 자리는 비워 둔다.

e6·e9 가 `research/final-2026-11` 에 병합되면 `PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.kit.build` 를 다시 돌린다. 결과가 도착했는데 `claims.yaml`·`hypotheses.yaml` 이 아직 '대기'이면 빌드가 `NEW-RESULT` 로 실패해 잊지 않게 한다.

## 부록 C. RESULTS.md 와 summary.json 사이에서 발견한 불일치 (고치지 않고 기록만)

`check_numbers.py` 는 RESULTS.md 에 적힌 숫자를 summary.json 과 대조한다. 이 자료집이 인용한 숫자는 아래 항목을 빼고 모두 일치했다.

1. **INC-1** (`e2_invariants`): RESULTS.md 의 C2.8 표는 '취약성 못 보는 모드'에서 AUC 0.8 이상 비구조 칸 192칸 중 c*>2 가 41/96 = 42.7%(공격 0%에서 6/32)라고 적는다. 그러나 summary.json 의 h15b.by_mode.blind.auc_ge_0.8_nonstructural 은 비어 있다(cells 0, 값 null). 숫자는 h15b.cells 에서 다시 계산하면 RESULTS.md 와 같다(kit 의 파생값 E2-05). RESULTS.md 가 틀린 것이 아니라 summary.json 의 요약이 빠진 것이다.
2. **INC-2** (`e2_invariants`): RESULTS.md 는 H1.5 의 c* 를 모드별로 적고 '두 모드를 합친 중앙값은 쓰지 않는다'고 한다(진실 모드 중앙 1.02, 못 보는 모드 5.02). summary.json 의 h15.cstar_range_tradeoff_cells 는 바로 그 합친 값(최소 0.076·중앙 2.016·최대 8.27)만 저장하고 모드별 중앙값은 저장하지 않는다. kit 은 h15.cells 에서 모드별로 다시 계산했다(파생값 E2-04, 기본 알람 구성 칸만).
3. **INC-3** (`e3_policy`): RESULTS.md 가 정확히 .x5 인 백분율을 일관되게 반올림하지 않는다: 88.25%를 '88.2', 79.75%를 '79.7', 30.25%를 '30.2'(E3), 56.25%를 '56.2'(E8) 로 쓰지만 71.25%는 '71.3', 13.25%는 '13.3' 으로 쓴다. 의미는 같고 마지막 자리 반올림만 다르다. kit 은 이 네 값을 소수 둘째 자리까지 적었다.
4. **INC-4** (`RESEARCH_LOG.md ↔ HYPOTHESES.md`): 연구 일지 02:05 행은 이제 'E5 의 luna 판정자 실행 불가(E9 의 H5.4 도 같은 이유로 판정 불가가 됨)'라고 적는다(이전 판은 H5.4 판정 불가를 E5 결과처럼 적었다). 다만 E9 결과 자체는 아직 검토 중이라 kit 은 H5.4 를 '대기'로 둔다. E5 쪽 luna 불가는 H5.2 의 한계이며 H5.2 는 '판정 불가'다.
5. **INC-5** (`HYPOTHESES.md`): H6.1·H6.2·H6.3(사다리·호출·테스트)이 'Q1' 제목 아래에 적혀 있지만 번호는 H6.x 이고 실험은 E7·E8 이다. kit 은 이를 Q6 으로 묶었다.
6. **INC-6** (`e3_policy`): '해로운 제안의 95.8%가 땅거미·불씨' 는 풀링 값(summary.json harm_alignment.harmful_share_dusk_or_ember_pooled 0.9575)이다. 같은 파일의 '세트 평균' 값은 95.9%, 검토의 재계산은 96.4% 였다. 정의·표본 차이이고 오류는 아니다(RESULTS.md 와 HYPOTHESES.md 사후 기록이 모두 이를 밝힌다).

## 부록 D. 다시 만드는 법

```
PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.kit.build          # 그림 3장 + 자료집 + 숫자 검사
PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.kit.check_numbers  # 숫자 검사만(실패하면 종료 코드 1)
```
