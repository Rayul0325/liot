#!/bin/sh
# 커밋된 파일에 평문 시크릿이 있는지 본다. CI 와 pre-push 훅이 같이 쓰는 단일 원천.
#
# 이전 판은 `sk-ant-` 하나만 봤는데, 이 프로젝트는 Anthropic 키를 쓰지 않는다 —
# 정작 위험한 건 이 저장소가 실제로 다루는 것들이다(2026-08-29 코드리뷰 지적):
#   LIOT_NOTIFY_SECRET(딥링크 HMAC) · LIOT_HA_TOKEN(홈어시스턴트 장기 토큰) ·
#   LIOT_NTFY_TOKEN(보호된 토픽 인증).
# 소스에 있는 dev 기본값(liot-dev-signing-key)은 '비밀이 아님'이 설계라 통과시킨다.
#
# git grep 종료 코드: 0=찾음 · 1=없음 · 그 밖=실행 실패. 실패를 '없음'으로 읽으면
# 스캔이 조용히 통과한다 — 2026-09-01 리뷰가 실제로 둘 다 재현했다:
#   - '-----BEGIN ...' 패턴이 옵션으로 읽혀(rc=129) 개인키가 커밋돼도 OK.
#   - git 체크아웃 밖에서 돌리면(rc=128) 아무것도 안 보고 OK.
# 그래서 패턴은 -e 로 넘기고, 1 이 아닌 실패는 스캔 실패로 친다.

set -e
cd "$(dirname "$0")/.."

fail=0

scan() {  # scan <설명> <정규식>
    rc=0
    out=$(git grep -nIE -e "$2" -- . ':!scripts/secret_scan.sh' 2>&1) || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "::error::$1"
        printf '%s\n' "$out" | head -5
        fail=1
    elif [ "$rc" -ne 1 ]; then
        echo "::error::시크릿 스캔을 실행하지 못했습니다(git grep 종료 코드 $rc): $1"
        printf '%s\n' "$out" | head -3
        fail=1
    fi
}

# 이 저장소의 시크릿 env 에 값이 박힌 경우 — 셸/.env 의 `=` 와 YAML 의 `:` 둘 다.
# (플레이스홀더 ***·<...>·빈 값은 문자 집합에 안 걸려 제외된다.)
#
# `:` 형태는 문서의 영문 산문 줄(`LIOT_NTFY_TOKEN: required-value`)에도 걸린다 —
# 알고 두는 오탐이다(2026-09-13 리뷰 R-7). 좁히는 방법은 둘 다 더 나쁘다:
#   - 값이 '플레이스홀더처럼 생겼는지'로 거르기: git grep 의 POSIX ERE 에는
#     lookahead 가 없고, 'required-value' 와 실제 패스프레이즈는 구분되지 않는다.
#   - `:` 형태를 yaml/env 파일로 한정하기: .md 에 붙여 넣은 진짜 토큰을 놓친다.
# 오탐은 게이트가 빨개져 사람이 보고 고치면 끝이고, 미탐은 시크릿이 그대로 나간다.
# 그래서 넓은 쪽을 유지한다. 문서에 예시를 적을 땐 ***·<...> 를 쓰면 걸리지 않는다.
#
# 하루 시뮬레이터의 실제 모델 키 4개(2026-10-20)도 같은 규칙으로 본다. Jev 키는 공개된 모양이
# 없어 이 이름 규칙이 유일한 그물이다.
scan "LIOT 시크릿 env 에 평문 값이 커밋됐습니다" \
    '(LIOT_NOTIFY_SECRET|LIOT_HA_TOKEN|LIOT_NTFY_TOKEN|JEV_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{8,}'

# 흔한 제공자 키 형태. sk-proj-·sk-svcacct- 는 하이픈이 들어 sk-[A-Za-z0-9]{32,} 에 안 걸려
# 따로 둔다. AIza 는 Google(Gemini) API 키 머리말이다.
scan "제공자 API 키로 보이는 문자열이 커밋됐습니다" \
    '(sk-ant-[A-Za-z0-9_-]{8}|sk-(proj|svcacct)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})'

# 개인키 블록.
scan "개인키 블록이 커밋됐습니다" \
    '-----BEGIN [A-Z ]*PRIVATE KEY-----'

if [ "$fail" -ne 0 ]; then
    echo "시크릿 스캔 실패 — 위 항목을 환경변수로 옮기고 .env(gitignore 대상)에 두세요."
    exit 1
fi

echo "OK: 평문 시크릿 없음"
