"""ntfy 알림 transport — 실제 외부 송신(L4 의 "몸통").

설계 핵심:
  - **egress 이후만 받는다**: transport 는 ``emit_external(NTFY, ...)`` 를
    통과한 *정제된* payload 만 인자로 받는다. raw Proposal 이나 device/room/
    rationale 은 구조적으로 도달 불가(Notifier 가 정제분만 넘긴다). transport
    는 정제 dict 를 ntfy HTTP 형식으로 옮길 뿐, 무엇을 빼야 하는지 모른다.
  - **알림일 뿐, 실행이 아니다**: ntfy 토픽으로 알림 1건을 보낸다. HA 액추에이터를
    호출하지 않는다(불변식 3: 제안-만 모델 유지). 이 모듈에 'Executor' 류 클래스 없음.
  - **무의존**: urllib 만 사용(OllamaBackend 의 HTTP transport 와 동일 패턴).
  - **fail-closed**: 송신 실패는 ``NtfyError`` 로 올라가고, 호출자(Notifier)가
    파이프라인을 죽이지 않고 '미전송'으로 기록한다.

프라이버시: 메시지 본문/제목에 디바이스명·방이름·rationale 가 들어가지 않는다.
NotificationPayload 가 위험도 라벨(label)·글리프·서명 딥링크만 담기 때문이며,
transport 는 그 화이트리스트 필드만 ntfy 로 옮긴다.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from liot.core.severity import SeverityLevel

from .escalation import GUIDANCE_119_TEXT
from .notifier import sign_feedback

__all__ = [
    "NtfyError",
    "NtfyTransport",
    "HTTPNtfyTransport",
    "FakeNtfyTransport",
    "build_ntfy_transport",
]


class NtfyError(Exception):
    """ntfy 송신 실패(도달 불가/비-2xx/타임아웃)."""


# 위험도 정수 → ntfy 우선순위(1=min ~ 5=max). 심각할수록 높게.
_TIER_PRIORITY = {0: "2", 1: "3", 2: "4", 3: "5"}

# 파급도 한국어 라벨 → SeverityLevel. 페이로드는 라벨 문자열만 싣기 때문에
# transport 는 라벨을 거꾸로 풀어 파급도의 푸시 세기를 얻는다.
_SEVERITY_BY_LABEL: dict[str, SeverityLevel] = {lvl.label_ko: lvl for lvl in SeverityLevel}


def _priority(sanitized: dict[str, Any], tier: int) -> str:
    """ntfy 우선순위. 파급도 라벨이 있으면 파급도에서(위험5·경고4·주의3·기록2),
    없거나 모르는 라벨이면 예전처럼 위험도 tier 에서 정한다."""
    label = sanitized.get("severity_label")
    level = _SEVERITY_BY_LABEL.get(label) if isinstance(label, str) else None
    if level is not None:
        return level.ntfy_priority
    return _TIER_PRIORITY.get(tier, "3")


@runtime_checkable
class NtfyTransport(Protocol):
    """정제된 알림 payload 를 외부 채널로 보낸다."""

    def send(self, topic: str, sanitized: dict[str, Any]) -> None: ...


def _parse_deeplink(deeplink: str) -> tuple[str, str, str, str] | None:
    """signed_deeplink(liot://p/<id>?t=<tier>&sig=<hex>[&n=<nonce>]) → 4요소.

    피드백 액션 버튼 URL 을 만들기 위해 (id, t, sig, nonce) 를 추출한다.
    nonce 는 없을 수 있다(빈 문자열). 형식이 어긋나면 None(액션 버튼 없이
    알림만 보낸다)."""
    try:
        parts = urlsplit(deeplink)
        pid = parts.path.rsplit("/", 1)[-1]
        q = parse_qs(parts.query)
        t = q.get("t", [""])[0]
        sig = q.get("sig", [""])[0]
        if pid and t and sig:
            return pid, t, sig, q.get("n", [""])[0]
    except (ValueError, IndexError):  # pragma: no cover - 방어적
        return None
    return None


def _parse_ack_link(link: str) -> tuple[str, str, str] | None:
    """보호자 확인 링크(liot://ack/<sid>?g=<gid>&sig=<hex>) → (sid, gid, sig).

    형식이 어긋나면 None(확인 버튼 없이 알림만 보낸다).
    """
    try:
        parts = urlsplit(link)
        if parts.scheme != "liot" or parts.netloc != "ack":
            return None
        sid = parts.path.rsplit("/", 1)[-1]
        q = parse_qs(parts.query)
        gid = q.get("g", [""])[0]
        sig = q.get("sig", [""])[0]
    except (ValueError, IndexError):  # pragma: no cover - 방어적
        return None
    if sid and gid and sig:
        return sid, gid, sig
    return None


class HTTPNtfyTransport:
    """ntfy 서버로 실제 HTTP POST(urllib). 기본 https://ntfy.sh.

    feedback_base_url 이 주어지면 수락/거절 http 액션 버튼을 ntfy Actions
    헤더로 붙인다(서명 딥링크의 id/t/sig 를 재사용 → 위조 불가).
    """

    def __init__(
        self,
        base_url: str = "https://ntfy.sh",
        *,
        feedback_base_url: str | None = None,
        timeout_s: float = 10.0,
        token: str | None = None,
        signing_secret: bytes | str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.feedback_base_url = (
            feedback_base_url.rstrip("/") if feedback_base_url else None
        )
        self.timeout_s = timeout_s
        self.token = token  # ntfy 인증 토큰(보호된 토픽용). 헤더에만 쓰고 로그 금지.
        # 액션 버튼 서명용 비밀키. 있어야 수락/거절 버튼을 만든다(action-bound 서명).
        # 없으면 버튼 없이 '열기' 딥링크만(상태 변경 불가 → 안전한 기본값).
        self._secret = (
            signing_secret.encode("utf-8")
            if isinstance(signing_secret, str)
            else signing_secret
        )

    def _summary_headers(self, sanitized: dict[str, Any]) -> dict[str, str]:
        """상황 요약 알림(보호자 전달·해소·119 안내) 헤더.

        Click 을 두지 않는다 — 알림을 여는 것은 확인이 아니다. 보호자 확인은
        서명 링크를 옮긴 별도 액션 버튼(``/guardian-ack``)에서만 일어난다.
        """
        headers: dict[str, str] = {
            "Title": "LIOT",
            "Priority": _priority(sanitized, 2),
            "Tags": "rotating_light",
        }
        parsed = _parse_ack_link(str(sanitized.get("signed_deeplink", "")))
        if parsed and self.feedback_base_url:
            sid, gid, sig = parsed
            ack = f"{self.feedback_base_url}/guardian-ack?s={sid}&g={gid}&sig={sig}"
            headers["Actions"] = f"http, Ack, {ack}, method=GET"
        if GUIDANCE_119_TEXT in str(sanitized.get("situation_label", "")):
            # 119 안내: 전화 앱을 여는 버튼만(번호를 누르는 건 사람 — 자동 신고 아님).
            call = "view, Call 119, tel:119"
            headers["Actions"] = (
                f"{headers['Actions']}; {call}" if "Actions" in headers else call
            )
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _headers(self, sanitized: dict[str, Any]) -> dict[str, str]:
        if "situation_label" in sanitized:
            return self._summary_headers(sanitized)
        tier = int(sanitized.get("risk_tier", 1))
        # 헤더는 ASCII 만(한글 라벨/글리프는 본문에). Tags 는 tier 영문명.
        headers: dict[str, str] = {
            "Title": "LIOT",
            "Priority": _priority(sanitized, tier),
            "Tags": {0: "white_circle", 1: "white_check_mark", 2: "question", 3: "shield"}.get(
                tier, "bell"
            ),
        }
        deeplink = str(sanitized.get("signed_deeplink", ""))
        if deeplink:
            headers["Click"] = deeplink
        # 피드백 http 액션(수락/거절). 각 액션마다 action-bound 서명을 새로
        # 계산해 싣는다 → 한 버튼 서명으로 다른 액션을 못 한다(선택 뒤집기 방어).
        # 서명 비밀키가 없으면 버튼을 만들지 않는다(상태 변경 불가, 안전한 기본값).
        # 지역 변수로 묶어 둔다: self._secret 은 bytes | None 이라, 바깥 가드로
        # 런타임엔 안전해도 클로저 안에서는 타입이 좁혀지지 않는다(mypy 지적).
        secret = self._secret
        parsed = _parse_deeplink(deeplink) if (self.feedback_base_url and secret) else None
        if parsed and secret is not None:
            pid, t, _, nonce = parsed
            base = self.feedback_base_url

            def _sig(action: str) -> str:
                # 서명 규약의 정본은 sign_feedback 하나다. 여기서 HMAC 을 다시
                # 구현하면(전에 그랬다) 한쪽만 고쳐도 게이트웨이 검증이 조용히
                # 어긋난다 — 버튼이 전부 403 이 되는 방향으로(2026-09-01 리뷰 R2-27).
                return sign_feedback(secret, pid, t, action, nonce)

            qs = f"p={pid}&t={t}&n={nonce}"
            accept = f"{base}/feedback?{qs}&sig={_sig('accept')}&a=accept"
            dismiss = f"{base}/feedback?{qs}&sig={_sig('dismiss')}&a=dismiss"
            headers["Actions"] = (
                f"http, Accept, {accept}, method=GET; "
                f"http, Dismiss, {dismiss}, method=GET"
            )
            # 상황 알림(파급도 있음)에는 '괜찮아요'(ok) 버튼을 덧붙인다 — 그 상황의
            # 사다리만 멈추고 선호 학습 신호는 남기지 않는다. ntfy 버튼 상한(3) 안이다.
            if sanitized.get("severity_label"):
                ok = f"{base}/feedback?{qs}&sig={_sig('ok')}&a=ok"
                headers["Actions"] += f"; http, OK, {ok}, method=GET"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _body(sanitized: dict[str, Any]) -> bytes:
        if "situation_label" in sanitized:
            # 상황 요약: [집 별칭] 상황 라벨(파급도) · 시각. 기기·방·근거는 페이로드에 없다.
            return (
                f"[{sanitized.get('home_alias', '')}] {sanitized.get('situation_label', '')}"
                f" ({sanitized.get('severity_label', '')}) · {sanitized.get('at', '')}"
            ).encode()
        # 본문은 위험도 라벨/글리프만(디바이스/방/rationale 부재). UTF-8 허용.
        glyph = str(sanitized.get("glyph", ""))
        label = str(sanitized.get("label", ""))
        return f"{glyph} {label} 제안이 도착했어요. 열어서 확인해 주세요.".encode()

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        import urllib.error
        import urllib.request

        # URL/헤더/본문 구성 자체의 오류(예: 비-ASCII topic → UnicodeEncodeError)도
        # NtfyError 로 감싼다. 그래야 LiotService.dispatch 의 fail-closed 핸들러가
        # 잡고, 스트림/틱 루프가 죽지 않는다(미전송으로 기록).
        try:
            url = f"{self.base_url}/{topic}"
            req = urllib.request.Request(
                url, data=self._body(sanitized), headers=self._headers(sanitized), method="POST"
            )
        except (UnicodeError, ValueError, TypeError) as exc:
            raise NtfyError(f"ntfy request build failed: {exc}") from exc
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                code = getattr(resp, "status", 200)
                if not (200 <= int(code) < 300):
                    raise NtfyError(f"ntfy returned status {code}")
        except urllib.error.HTTPError as exc:
            raise NtfyError(f"ntfy http error {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError, UnicodeError) as exc:
            raise NtfyError(f"ntfy transport failed: {exc}") from exc


class FakeNtfyTransport:
    """테스트용 transport. 보낸 (topic, payload) 를 기록한다.

    fail=True 면 send 가 NtfyError 를 던져 fail-closed 경로를 검증할 수 있다.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        if self.fail:
            raise NtfyError("fake transport forced failure")
        # 기록 전 직렬화 왕복으로 JSON-안전성도 함께 확인.
        self.sent.append((topic, json.loads(json.dumps(sanitized))))


def build_ntfy_transport() -> NtfyTransport | None:
    """환경변수로 ntfy transport 구성. 미설정이면 None(송신 안 함, sanitize-only).

    LIOT_NTFY_URL          : ntfy 서버(기본 https://ntfy.sh).
    LIOT_NTFY_TOPIC        : 토픽(Notifier 가 사용; transport 는 인자로 받음).
    LIOT_NTFY_FEEDBACK_URL : 피드백 엔드포인트 base(액션 버튼용). 미설정 시 버튼 없음.
    LIOT_NTFY_TOKEN        : 보호된 토픽 인증 토큰.
    LIOT_NTFY_TIMEOUT      : 초(기본 10).
    """
    import os

    base = os.environ.get("LIOT_NTFY_URL", "").strip()
    if not base:
        return None
    return HTTPNtfyTransport(
        base,
        feedback_base_url=os.environ.get("LIOT_NTFY_FEEDBACK_URL", "").strip() or None,
        timeout_s=float(os.environ.get("LIOT_NTFY_TIMEOUT", "10") or "10"),
        token=os.environ.get("LIOT_NTFY_TOKEN", "").strip() or None,
        # 액션 버튼 서명용 비밀키(딥링크 서명과 동일해야 게이트웨이가 검증).
        signing_secret=os.environ.get("LIOT_NOTIFY_SECRET", "").strip() or None,
    )
