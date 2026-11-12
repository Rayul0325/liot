"""LIOT 상시 데몬 진입점 — ``liot-serve``.

환경변수로 실배포를 구성한다(하드웨어/시크릿은 코드에 없음):

  필수 없음(전부 합리적 기본값). 자주 쓰는 것:
    LIOT_HOME_ID        : 테넌트 id(기본 'singleton').
    LIOT_DB_PATH        : SQLite 경로(기본 'liot.db'; ':memory:' 가능).
    LIOT_HA_URL         : HA WebSocket URL. 있으면 센서 스트림 소비 — 끊기면 같은 프로세스 안에서
                          다시 붙고(1→60초 대기) 붙을 때마다 현재 센서값을 재동기화한다. 시작 때
                          HA 가 닿지 않아도 사다리·틱은 돈다. SIGTERM/SIGINT 로 정리하고 끝난다.
    LIOT_HA_TOKEN       : HA 장기 액세스 토큰.
    LIOT_LLM_BACKEND    : 'ollama' 등(미설정 시 FakeLLM).
    LIOT_NTFY_URL       : ntfy 서버. 있으면 실제 알림 송신(미설정 시 sanitize-only).
    LIOT_NTFY_TOPIC     : 토픽(기본 'liot').
    LIOT_NTFY_FEEDBACK_URL : 알림 액션 버튼이 가리킬 피드백 base URL.
    LIOT_NOTIFY_SECRET  : 딥링크 HMAC 서명 비밀키. 미설정(빈 값·공백 포함)이면
                          소스에 공개된 dev 키가 쓰이고, 그 동안에는 피드백
                          수신구를 아예 열지 않는다(fail-closed).
    LIOT_FEEDBACK_ALLOW_DEV_KEY : dev 키인 채로 수신구를 여는 로컬 확인용 탈출구
                          (기본 꺼짐. 켜면 위조 가능한 서명을 받아들인다).
    LIOT_TICK_SECONDS   : 능동 틱 주기(기본 300).
    LIOT_HOME_TZ        : 집 시간대(IANA 이름, 기본 'Asia/Seoul'). 시간대 버킷(아침·낮·저녁·밤,
                          야간 정숙)과 능동 예산의 '하루'를 이 지역 시각으로 센다. 모르는
                          이름이면 기동 시 ValueError(한국어 한 줄)로 멈춘다.
    LIOT_FEEDBACK_HOST/PORT : 피드백 수신구(기본 127.0.0.1:8099; 0=비활성).

데몬은 절대 액추에이터를 호출하지 않는다(제안-만). HA 로는 읽기만, 외부로는
ntfy 알림만 나간다.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import sys
from typing import Any, Protocol

from liot.app import LiotApp
from liot.core.clock import Clock, resolve_tz
from liot.core.env import env_flag
from liot.db.database import init_db
from liot.layers.l1_sensing.ha_bridge import (
    BridgeFactory,
    call_service_once,
    connect_ha,
    get_state_once,
)
from liot.layers.l3_judgment.llm_judge import build_llm_backend
from liot.layers.l4_execution.executor import HAExecutor, build_execution_policy
from liot.layers.l4_execution.ntfy_transport import build_ntfy_transport
from liot.service import DEV_SIGNING_SECRET, LiotService

DEV_SECRET_TEXT = DEV_SIGNING_SECRET.decode("utf-8")


class FeedbackServing(Protocol):
    """수신구를 열 수 있는 것. open_feedback_endpoint 는 이것만 필요하다."""

    async def serve_feedback(
        self, host: str, port: int
    ) -> asyncio.AbstractServer: ...

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_HOME_TZ",
    "resolve_home_tz",
    "resolve_db_path",
    "feedback_block_reason",
    "open_feedback_endpoint",
    "resolve_notify_secret",
    "main",
]


# SQLite 기본 경로의 단일 원천. 데몬과 데모 서버(liot.demo_server)가 같은 DB 를
# 보려면 기본값이 한 곳에만 있어야 한다 — 양쪽에 따로 적어 두면 한쪽만 바뀌어
# 관제실에서 입력한 거주자·보호자·동의가 데몬에 안 보이는 일이 조용히 생긴다.
DEFAULT_DB_PATH = "liot.db"


def resolve_db_path() -> str:
    """LIOT_DB_PATH 를 읽어 정규화한다. 미설정·빈 값·공백이면 DEFAULT_DB_PATH."""
    return os.environ.get("LIOT_DB_PATH", "").strip() or DEFAULT_DB_PATH


def resolve_notify_secret(raw: str | None) -> str:
    """LIOT_NOTIFY_SECRET 값을 정규화한다. 빈 값·공백은 '미설정'과 같다.

    주변 공백을 벗겨 내는 이유는 두 가지다.
      - 빈 문자열이 '진짜 비밀키가 설정됨'으로 오인되면 아래 게이트가 통과하고,
        HMAC 이 빈 키로 계산돼 누구나 링크를 위조할 수 있다.
      - 서명(ntfy_transport)은 이미 .strip() 한 값을 쓴다. 검증 쪽이 안 벗기면
        `LIOT_NOTIFY_SECRET="abc "` 같은 값에서 모든 버튼이 403 이 된다.
    """
    return (raw or "").strip() or DEV_SECRET_TEXT


def feedback_block_reason(secret: str, *, allow_dev_key: bool = False) -> str | None:
    """피드백 수신구를 열면 안 되는 이유(한 줄) 또는 None(열어도 됨).

    규칙은 하나다: **소스에 공개된 dev 키를 쓰는 동안에는 열지 않는다.**

    이전 판(2026-08-29 오전)은 '루프백 바인드면 안전'으로 봤지만 그건 틀린
    전제였다. 프로세스 안에서는 앞단의 리버스 프록시·터널(nginx·Cloudflare
    Tunnel·ngrok)을 알 수 없고, README 가 안내하는 폰 연동 구성이 바로 그
    형태다 — 루프백에 바인드한 채로 공개돼 있으면 게이트가 통과해 버렸다.
    그래서 도달 가능성을 추정하지 않고, 위조 가능한 키인지만 본다.

    로컬에서 수신구를 굳이 띄워 봐야 한다면 LIOT_FEEDBACK_ALLOW_DEV_KEY=1 로
    명시적으로 켠다(기본 꺼짐 — 침묵이 아니라 선언을 요구한다).
    """
    if secret.strip() != DEV_SECRET_TEXT:
        return None
    if allow_dev_key:
        return None
    return (
        "LIOT_NOTIFY_SECRET 이 설정되지 않아 소스에 공개된 dev 키를 쓰고 있습니다. "
        "이 키로 서명한 수락/거절 링크는 저장소를 읽은 누구나 위조할 수 있습니다"
    )


async def open_feedback_endpoint(
    service: FeedbackServing,
    *,
    host: str,
    port: int,
    secret: str,
    allow_dev_key: bool = False,
) -> asyncio.AbstractServer | None:
    """피드백 수신구를 연다 — 열어도 되는 경우에만. 아니면 사유를 찍고 None.

    가드와 개방을 **한 함수 안에** 둔다. 둘이 떨어져 있으면 리팩터 중에 가드만
    조용히 빠져도 아무 테스트가 울지 않는다(2026-08-29 실측: 호출부 한 줄을
    지워도 310 테스트 전원 통과했다). 여기서는 가드를 지우면 곧바로 적색이다.
    """
    if not port:
        return None
    blocked = feedback_block_reason(secret, allow_dev_key=allow_dev_key)
    if blocked:
        print(f"[feedback] 수신구를 열지 않습니다 — {blocked}.")
        print(
            "[feedback] LIOT_NOTIFY_SECRET 에 임의의 긴 비밀키를 넣으면 열립니다"
            " (로컬 확인용으로만 열려면 LIOT_FEEDBACK_ALLOW_DEV_KEY=1)."
        )
        return None
    server = await service.serve_feedback(host, port)
    print(f"[feedback] http://{host}:{port}/feedback 수신 대기")
    return server


DEFAULT_HOME_TZ = "Asia/Seoul"


def resolve_home_tz(raw: str | None) -> str:
    """``LIOT_HOME_TZ`` 값 → 검증된 IANA 시간대 이름. 비었으면 기본 Asia/Seoul.

    모르는 이름은 ``ValueError``(한국어 한 줄)로 기동을 멈춘다 — 조용히 UTC 로 돌면 한국
    아침이 '밤'으로 판정돼 야간 정숙·시간대 예산이 엉뚱한 시각에 걸린다.
    """
    name = (raw or "").strip() or DEFAULT_HOME_TZ
    try:
        resolve_tz(name)
    except ValueError:
        raise ValueError(
            f"LIOT_HOME_TZ 를 알 수 없습니다: {name!r} — IANA 시간대 이름을 쓰세요(예: Asia/Seoul)."
        ) from None
    return name


class _StopSignals:
    """SIGTERM·SIGINT 를 받으면 주 태스크를 취소해 finally(피드백 서버·DB 닫기)가 돌게 한다.

    launchd 는 SIGTERM 으로 끈다. 처리기가 없으면 프로세스가 finally 없이 죽어 DB 의 마지막 쓰기가
    닫히지 못한다. 취소는 **한 번만** 건다 — 정리 중(DB 닫는 중)에 신호가 또 와도 정리를 끊지 않는다.
    """

    def __init__(self) -> None:
        self.requested = False
        self._signals: list[signal.Signals] = []
        self._prior: dict[signal.Signals, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def install(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        loop = asyncio.get_running_loop()
        self._loop = loop

        def _request() -> None:
            if not self.requested:
                self.requested = True
                task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            prior = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, _request)
            except (NotImplementedError, RuntimeError, ValueError):
                continue  # 주 스레드 밖·지원 안 하는 플랫폼 — 처리기 없이 돈다
            self._signals.append(sig)
            self._prior[sig] = prior

    def remove(self) -> None:
        if self._loop is None:
            return
        for sig in self._signals:
            self._loop.remove_signal_handler(sig)
            # 설치 전 처리기로 되돌린다(asyncio.run 이 SIGINT 에 건 처리기 등을 지우지 않는다).
            prior = self._prior.get(sig)
            if prior is not None:
                signal.signal(sig, prior)
        self._signals.clear()
        self._prior.clear()


async def _amain() -> int:
    home_id = os.environ.get("LIOT_HOME_ID", "singleton").strip() or "singleton"
    db_path = resolve_db_path()
    tick_seconds = float(os.environ.get("LIOT_TICK_SECONDS", "300") or "300")
    secret = resolve_notify_secret(os.environ.get("LIOT_NOTIFY_SECRET"))
    topic = os.environ.get("LIOT_NTFY_TOPIC", "liot").strip() or "liot"
    ha_url = os.environ.get("LIOT_HA_URL", "").strip()
    ha_token = os.environ.get("LIOT_HA_TOKEN", "").strip()
    fb_host = os.environ.get("LIOT_FEEDBACK_HOST", "127.0.0.1").strip()
    fb_port = int(os.environ.get("LIOT_FEEDBACK_PORT", "8099") or "8099")
    allow_dev_key = env_flag("LIOT_FEEDBACK_ALLOW_DEV_KEY")

    home_tz = resolve_home_tz(os.environ.get("LIOT_HOME_TZ"))
    clock = Clock(tz=home_tz)
    db = await init_db(db_path, durable=True)

    # DB 를 연 직후부터 끝까지 하나의 try/finally 다. DB 를 연 뒤 어디서 실패하든(HA 호출·조립·수신구)
    # aiosqlite 의 비데몬 스레드가 닫히지 않아 프로세스가 멈춰 남는 일이 없다.
    stop = _StopSignals()
    fb_server: asyncio.AbstractServer | None = None
    try:
        stop.install(asyncio.current_task())

        # HA(있으면): 연결·카테고리 맵·재동기화는 서비스의 감독이 맡는다(끊기면 다시 붙는다).
        # 시작 때는 카탈로그 맵으로 시작하고 첫 연결에서 HA 맵으로 갈아 낀다. HA 가 닿지 않아도
        # 사다리·틱·피드백은 HA 와 무관하게 돈다.
        ha_factory: BridgeFactory | None = None
        if ha_url and ha_token:
            ha_factory = functools.partial(connect_ha, ha_url, ha_token)
            print("[HA] 감독 시작 — 연결·재동기화는 백그라운드에서(끊기면 자동 재연결).")
        else:
            print("[HA] LIOT_HA_URL/TOKEN 미설정 → 센서 스트림 없이 능동 틱만 구동.")

        app = LiotApp(clock=clock, categories={}, llm_backend=build_llm_backend())
        transport = build_ntfy_transport()

        # 조작(불변식 8): LIOT_EXECUTION_ENABLED 가 참이고 HA 설정이 있을 때만 활성.
        # HA 가 없으면 조작 대상이 없으므로 executor 미구성(제안-만 유지). 실행기는 호출마다 짧은
        # 별도 연결(열기 → 인증 → 호출 → 닫기)을 쓴다 — 스트림 소켓을 공유하면 같은 소켓에서
        # 동시에 recv() 해 websockets 가 ConcurrencyError 를 낸다. HA 에 닿지 못하면 호출이 예외로
        # 끝나 실행 실패(fail-closed)가 된다.
        policy = build_execution_policy()
        executor = None
        if policy.enabled:
            if ha_factory is not None:
                executor = HAExecutor(
                    functools.partial(call_service_once, ha_factory),
                    functools.partial(get_state_once, ha_factory),
                )
            else:
                print("[exec] LIOT_EXECUTION_ENABLED 이나 HA 미연결 → 조작 비활성(제안-만).")

        service = LiotService(
            app, db, home_id=home_id, clock=clock, transport=transport,
            topic=topic, secret=secret, executor=executor, policy=policy,
        )
        print(
            f"[LIOT] backend={app.judge.backend.model_id} "
            f"ntfy={'on' if transport else 'sanitize-only'} "
            f"exec={'ON' if executor else 'off(제안-만)'} tick={tick_seconds}s db={db_path} "
            f"tz={home_tz}"
        )

        fb_server = await open_feedback_endpoint(
            service, host=fb_host, port=fb_port, secret=secret, allow_dev_key=allow_dev_key
        )

        await service.run(ha_factory=ha_factory, tick_seconds=tick_seconds)
    except asyncio.CancelledError:
        if not stop.requested:
            raise  # 신호가 아닌 취소(호출자·시험)는 그대로 올린다
        # 종료 신호로 우리가 건 취소다 — 정상 종료. 아래 finally 가 수신구와 DB 를 닫는다.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        print("[LIOT] 종료 신호를 받아 정리하고 끝냅니다.")
    finally:
        stop.remove()
        if fb_server is not None:
            fb_server.close()
        await db.close()
    return 0


def main() -> int:
    # 재연결·감시 중단 같은 운영 로그가 보이게 한다(처리기가 없으면 WARNING 이상만 기본 처리기로 나간다).
    # aiosqlite 의 DEBUG 는 SQL 인자(보호자 토픽 포함)를 찍으므로 INFO 위로 둔다(engineering-notes).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiosqlite").setLevel(logging.INFO)
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
