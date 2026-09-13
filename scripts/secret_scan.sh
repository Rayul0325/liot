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
scan "LIOT 시크릿 env 에 평문 값이 커밋됐습니다" \
    '(LIOT_NOTIFY_SECRET|LIOT_HA_TOKEN|LIOT_NTFY_TOKEN)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{8,}'

# 흔한 제공자 키 형태.
scan "제공자 API 키로 보이는 문자열이 커밋됐습니다" \
    '(sk-ant-[A-Za-z0-9_-]{8}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16})'

# 개인키 블록.
scan "개인키 블록이 커밋됐습니다" \
    '-----BEGIN [A-Z ]*PRIVATE KEY-----'

if [ "$fail" -ne 0 ]; then
    echo "시크릿 스캔 실패 — 위 항목을 환경변수로 옮기고 .env(gitignore 대상)에 두세요."
    exit 1
fi

echo "OK: 평문 시크릿 없음"
