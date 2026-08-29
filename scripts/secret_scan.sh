#!/bin/sh
# 커밋된 파일에 평문 시크릿이 있는지 본다. CI 와 pre-push 훅이 같이 쓰는 단일 원천.
#
# 이전 판은 `sk-ant-` 하나만 봤는데, 이 프로젝트는 Anthropic 키를 쓰지 않는다 —
# 정작 위험한 건 이 저장소가 실제로 다루는 것들이다(2026-08-29 코드리뷰 지적):
#   LIOT_NOTIFY_SECRET(딥링크 HMAC) · LIOT_HA_TOKEN(홈어시스턴트 장기 토큰) ·
#   LIOT_NTFY_TOKEN(보호된 토픽 인증).
# 소스에 있는 dev 기본값(liot-dev-signing-key)은 '비밀이 아님'이 설계라 통과시킨다.

set -e
cd "$(dirname "$0")/.."

fail=0

scan() {  # scan <설명> <정규식>
    if git grep -nIE "$2" -- . ':!scripts/secret_scan.sh' >/dev/null 2>&1; then
        echo "::error::$1"
        git grep -nIE "$2" -- . ':!scripts/secret_scan.sh' | head -5
        fail=1
    fi
}

# 이 저장소의 시크릿 env 에 값이 박힌 경우(플레이스홀더 ***·<...>·빈 값은 제외).
scan "LIOT 시크릿 env 에 평문 값이 커밋됐습니다" \
    '(LIOT_NOTIFY_SECRET|LIOT_HA_TOKEN|LIOT_NTFY_TOKEN)[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{8,}'

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
