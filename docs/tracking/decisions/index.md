# 결정 기록

| 번호 | 결정 | 상태 |
|---|---|---|
| [0001](0001-two-risk-axes.md) | 위험도를 행동 확인 강도와 상황 파급도 두 값으로 나눈다 | 채택 |
| [0002](0002-severity-lookup-table.md) | 파급도는 CAP 1.2 를 고친 조회표로 정하고 곱셈 점수를 쓰지 않는다 | 채택 |
| [0003](0003-local-decision-model-raise-only.md) | 표가 바닥이고, 로컬 결정 모델(llama.cpp)은 올리기만 한다. 클라우드 Jev 기각 | 채택(기본 모델 미정) |
| [0004](0004-one-tap-keeps-ember.md) | 밸브 잠그기·도어락 잠그기는 티어 불씨를 유지하고 확인만 탭 한 번 | 채택 |
| [0005](0005-guardian-forward-consent-and-floor.md) | 보호자 전달은 사전 동의 + 파급도 하한 '위험' 일 때만. 모델 상향은 가족을 부르지 않는다 | 채택 |
| [0006](0006-table-first-two-phase-dispatch.md) | 상황 알림은 표 값으로 먼저 보내고 모델 판정은 그 뒤 백그라운드에서 | 채택 |
| [0007](0007-self-declared-traits-only.md) | 거주자 특성은 직접 입력·동의로만, 추론 금지, 기기 밖 비유출 | 채택 |
| [0008](0008-push-priority-follows-severity.md) | 상황 알림의 푸시 세기는 파급도를 따른다(문 열림 4, 가스 5) | 채택 |
| [0009](0009-home-level-consent.md) | 동의는 집 단위 — 누구든 부여, 누구든 철회하면 정지 | 채택 |
| [0010](0010-control-room-shares-db-no-login.md) | 관제실은 데몬 DB 공유, 로그인 없음, 입력자 표시로만 구분 | 채택(로그인은 이후 범위) |
| [0011](0011-119-guidance-only.md) | 119 는 안내만, 자동 신고 없음 | 채택 |
| [0012](0012-ha-unavailable-is-unmonitorable.md) | HA 읽기 불가는 위험이 아니라 감시 불가 — 거주자에게만 한 번 | 채택 |
| [0013](0013-charter-off-keeps-guardian-forward.md) | 헌장 전면 OFF 는 거주자 알림만 끈다. 동의한 보호자 전달·119 안내는 막지 않는다 | 채택 |
| [0015](0015-situation-reject-stops-ladder-only.md) | 상황 알림의 '거절'은 그 상황의 사다리만 멈춘다. 30일 거절 이력에 넣지 않는다 | 채택 |
| [0016](0016-comfort-held-during-situation.md) | 열린 안전 상황 동안 편의 제안은 자동 실행하지 않고, 하한 위험 상황 동안은 폰 발송도 보류한다 | 채택 |
| [0018](0018-digest-expiry-learning-deferred.md) | 모아보기 만료를 비선호 학습에서 빼는 고침은 단독 적용하지 않는다(E13: 폰 도움 −0.070) | 채택 |
| [0019](0019-duplicate-over-missing-after-kill.md) | 강제 종료 직후 중복 알림·중복 보호자 전달은 받아들이고 누락은 받아들이지 않는다 | 채택 |
