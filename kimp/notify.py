"""
텔레그램 알림과 명령

알림: 로그 중 중요한 것(경고 이상 + 진입·청산·송금)을 텔레그램으로 보낸다
------------------------------------------------------------------
- 엔진·브로커는 지금처럼 로그만 남기면 된다. TelegramLogHandler 가 골라서 보낸다
  예) "[진입] #3 CAP ..."  "[청산 TAKE_PROFIT] ..."  "[송금 요청 ...]"  "[되돌리기 실패] ..."(긴급)
- 보내기는 따로 스레드에서 → 텔레그램이 느리거나 죽어도 매매는 안 멈춘다
- 같은 문장이 1분 안에 또 오면 건너뛰고, 1분에 최대 20개까지만

명령 (TELEGRAM_CHAT_ID 에서 온 것만 받는다)
------------------------------------------------------------------
  /status   잔고·포지션·진입 가능 여부        /premium  지금 김프 (가운데·진입·청산, 기준까지 남은 폭)
  /stop     신규 진입 중단 (청산은 계속)        /resume   재개
  /close    강제 청산 + 신규 진입 중단 (/close, /close CAP,SEI)   /collect  수집 현황 (start 40 · stop)
  /help     명령 목록

  명령에 인자를 붙일 수 있다 (/close CAP,SEI). 핸들러가 인자를 받으면 넘겨준다.

준비: 텔레그램 @BotFather 에서 봇을 만들어 토큰 받기 → 봇에게 아무 말이나 보낸 뒤
      https://api.telegram.org/bot<토큰>/getUpdates 에서 chat.id 확인 → .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import html
import json
import logging
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 로그 한 줄의 모양: "[태그] #번호 코인 | 항목 | 항목 ..." — 텔레그램에서는 한 줄로 길게 오면 눈에 안 들어온다
_TAG_RE = re.compile(r"^(\[[^\]]+\])\s*(.*)$", re.S)
_POS_RE = re.compile(r"(#\d+ [A-Z0-9]+)")
_LONG_LINE = 70


def pretty(text: str, icon: str = "") -> str:
    """
    로그 한 줄 → 텔레그램 HTML (읽기 좋게)

        🚨 <b>[청산 TAKE_PROFIT]</b> <b>#3 IRYS</b>
        김프 -0.268% → -0.605%
        업비트 USDT 19.74개 → 21.53개 (+1.79)
        ...

    - 맨 앞 [태그] 와 '#번호 코인' 은 굵게, 첫 줄에
    - ' | ' 로 이어 붙인 항목은 줄마다 하나씩. 그래도 긴 줄은 ' / ' 에서 한 번 더 나눈다
    - 태그가 없는 문장은 그대로 (HTML 특수문자만 피한다)
    """
    m = _TAG_RE.match(text)
    if not m:
        return icon + html.escape(text)
    tag, body = m.group(1), m.group(2)
    parts = [p.strip() for p in body.split(" | ") if p.strip()]
    first = f"{icon}<b>{html.escape(tag)}</b>"
    if parts:
        pm = _POS_RE.match(parts[0])
        if pm:
            first += f" <b>{html.escape(pm.group(1))}</b>{html.escape(parts[0][pm.end():])}"
            parts = parts[1:]
    lines = [first]
    for p in parts:
        if len(p) > _LONG_LINE and " / " in p:
            lines.extend("• " + html.escape(x.strip()) for x in p.split(" / "))
        else:
            lines.append("• " + html.escape(p))
    return "\n".join(lines)


def plain(text: str) -> str:
    """HTML 을 못 보낼 때 쓰는 평문 (굵게 표시만 벗기고 특수문자는 되돌린다)"""
    return html.unescape(re.sub(r"</?b>", "", text))

# INFO 로그 중에서도 보낼 것 (메시지가 이걸로 시작하면)
INFO_TAGS = ("[진입]", "[청산", "[송금", "[펀딩비 진입]", "[펀딩비 청산]", "[복구]", "[신규 진입 재개]", "[하루 요약]")


def _post(url: str, data: Dict[str, str], timeout: float = 10.0) -> dict:
    """
    텔레그램 API 에 POST (표준 라이브러리만 — requests 같은 것을 쓰지 않는다)

    테스트에서는 이 함수를 가짜로 바꿔 끼운다 (TelegramNotifier(post=...)).
    """
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class TelegramNotifier:
    """
    텔레그램으로 보내기 (큐 + 보내는 스레드)

    **왜 큐와 스레드인가**: 매매 스레드가 텔레그램을 기다리면 안 된다. 텔레그램이 느리거나
    죽어 있어도 진입·청산은 제 속도로 돌아야 한다. 그래서 send() 는 큐에 넣고 바로 돌아오고,
    실제 전송은 데몬 스레드가 한다 (실패해도 로그만 남기고 삼킨다).

    **왜 중복·과다를 버리나**: 같은 경고가 스캔마다(20초) 반복되면 텔레그램이 요청을 막는다.
    같은 문장은 1분에 한 번, 전체는 1분에 20개까지만 보낸다.
    """

    def __init__(self, token: str, chat_id: str, prefix: str = "", post: Callable = _post):
        """
        Args:
            token: @BotFather 에서 받은 봇 토큰
            chat_id: 받을 채팅 번호 (이 번호에서 온 명령만 받는다)
            prefix: 모든 메시지 앞에 붙일 글자 (여러 봇을 구분할 때)
            post: 보내는 함수 (테스트에서 가짜로 바꿔 끼운다)
        """
        self.token = token
        self.chat_id = str(chat_id)
        self.prefix = html.escape(prefix)   # 메시지는 HTML 로 보내므로 접두어도 피한다
        self._post = post
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._sent_at: Dict[str, float] = {}
        self._window: list = []
        self._thread = threading.Thread(target=self._worker, name="telegram-send", daemon=True)
        self._thread.start()

    def url(self, method: str) -> str:
        """텔레그램 API 주소 (sendMessage / getUpdates)"""
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        """보낼 문장을 큐에 넣기 (중복·과다는 버림)"""
        now = time.time()
        if now - self._sent_at.get(text, -1e9) < 60:
            return
        self._window = [t for t in self._window if now - t < 60]
        if len(self._window) >= 20:
            return
        self._sent_at[text] = now
        self._window.append(now)
        try:
            self._q.put_nowait(f"{self.prefix}{text}"[:4000])
        except queue.Full:
            pass

    def _worker(self) -> None:
        """
        보내는 스레드 (데몬) — 큐에서 꺼내 하나씩 보낸다

        실패해도 **다시 시도하지 않고 넘어간다.** 알림은 매매보다 중요하지 않고, 재시도하다
        큐가 막히면 뒤에 쌓인 더 중요한 알림이 늦어진다.
        로그는 'kimp.notify.quiet' 로 남긴다 — 이 이름은 TelegramLogHandler 가 걸러내서,
        '텔레그램 실패' 알림을 또 텔레그램으로 보내려는 무한 고리를 막는다.
        """
        while True:
            text = self._q.get()
            try:
                self._post(self.url("sendMessage"), {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})
            except Exception as exc:
                # HTML 이 깨져서 거절됐을 수 있다 → 평문으로 한 번 더 (알림을 잃는 것보다 낫다)
                try:
                    self._post(self.url("sendMessage"), {"chat_id": self.chat_id, "text": plain(text)})
                except Exception as exc2:  # 알림 실패로 매매를 멈추지 않는다
                    logging.getLogger("kimp.notify.quiet").debug("텔레그램 보내기 실패: %s / %s", exc, exc2)

    def flush(self, timeout: float = 10.0) -> bool:
        """
        큐에 남은 메시지를 다 보낼 때까지 기다린다 (종료 직전에 부른다)

        보내기는 데몬 스레드가 한다 → 프로그램이 끝나면 파이썬이 그 스레드를 **즉시 죽인다.**
        그래서 이걸 안 부르면 마지막 알림(특히 '청산')이 통째로 사라진다.
        ONE_SHOT 처럼 몇 초 만에 끝나는 실행에서 실제로 겪었다 (2026-10-23).

        Returns:
            다 보냈으면 True, timeout 안에 못 비웠으면 False (그래도 종료를 막지는 않는다)
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        while not self._q.empty() and _time.monotonic() < deadline:
            _time.sleep(0.1)
        # 큐가 비어도 마지막 한 건은 아직 보내는 중일 수 있다 → 조금 더 기다린다
        _time.sleep(min(1.0, max(0.0, deadline - _time.monotonic())))
        left = self._q.qsize()
        if left:
            logger.warning("텔레그램 메시지 %d개를 못 보내고 종료합니다", left)
        return left == 0

    def send_now(self, text: str) -> bool:
        """바로 보내고 성공 여부 (live_check 테스트 메시지용)"""
        try:
            r = self._post(self.url("sendMessage"),
                           {"chat_id": self.chat_id, "text": f"{self.prefix}{html.escape(text)}", "parse_mode": "HTML"})
            return bool(r.get("ok"))
        except Exception as exc:
            logger.warning("텔레그램 보내기 실패: %s", exc)
            return False


class TelegramLogHandler(logging.Handler):
    """
    로그 → 텔레그램 (경고 이상, 또는 INFO_TAGS 로 시작하는 INFO)

    엔진·브로커는 텔레그램을 모른다. 그냥 로그만 남기고, 이 핸들러가 **보낼 것만 골라낸다.**
    덕분에 매매 코드에 알림 코드가 섞이지 않는다.
    """

    def __init__(self, notifier: TelegramNotifier):
        super().__init__(level=logging.INFO)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        """
        로그 한 줄을 보고 텔레그램으로 보낼지 정한다

        보내는 것: WARNING 이상 전부 + INFO_TAGS 로 시작하는 INFO (진입·청산·송금 등)
        'kimp.notify' 로 시작하는 로그는 **반드시 건너뛴다** — 텔레그램 전송 실패 로그를
        또 텔레그램으로 보내려 하면 무한히 반복된다.
        앞에 붙는 그림: CRITICAL 🚨 / WARNING ⚠️ / INFO 없음
        """
        if record.name.startswith("kimp.notify"):
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        if record.levelno >= logging.WARNING or msg.startswith(INFO_TAGS):
            icon = "🚨 " if record.levelno >= logging.CRITICAL else ("⚠️ " if record.levelno >= logging.WARNING else "")
            self.notifier.send(pretty(msg, icon))


class TelegramCommands:
    """텔레그램 명령 받기 (getUpdates 길게 기다리기, 따로 스레드)"""

    def __init__(self, notifier: TelegramNotifier, handlers: Dict[str, Callable[..., str]],
                 post: Optional[Callable] = None):
        self.n = notifier
        self.handlers = handlers
        self._post = post or notifier._post
        self._offset = 0

    def handle(self, update: dict) -> Optional[str]:
        """
        업데이트 하나 처리 → 답장 글자 (허용 안 된 채팅이면 None)

        **설정한 chat_id 에서 온 것만 받는다.** 봇 이름을 아는 사람은 누구나 말을 걸 수 있으므로,
        이 확인이 없으면 남이 /close 를 눌러 포지션을 정리할 수 있다.
        그룹에서는 명령이 '/stop@봇이름' 으로 오므로 @ 뒤를 떼어낸다.
        핸들러가 인자를 받는지(/close CAP) 안 받는지(/status)는 함수 서명을 보고 판단한다.
        처리 중 오류가 나도 예외를 올리지 않고 '처리 실패: …' 로 답장한다 (루프가 죽지 않게).
        """
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.n.chat_id:
            return None
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        head, _, rest = text.partition(" ")
        cmd = head.split("@")[0].lower()   # 그룹에서는 /stop@봇이름 처럼 온다
        fn = self.handlers.get(cmd)
        if fn is None:
            return "명령: " + " ".join(sorted(self.handlers))
        try:
            # 인자를 받는 핸들러(/close CAP)와 안 받는 핸들러(/status)를 모두 지원한다
            import inspect
            takes_arg = bool(inspect.signature(fn).parameters)
            return fn(rest.strip()) if takes_arg else fn()
        except Exception as exc:
            return f"처리 실패: {exc}"

    def run(self, stop_event: threading.Event) -> None:
        """
        명령 받기 루프 (getUpdates 롱 폴링)

        timeout=25 로 '새 메시지가 올 때까지 25초 기다려라' 고 요청한다 — 짧게 반복 조회하면
        텔레그램이 요청을 막는다. offset 은 '여기까지 받았다' 는 표시로, 같은 명령을 두 번
        처리하지 않게 한다 (봇을 다시 켜면 offset 이 0 이라 밀린 명령을 한 번 처리할 수 있다).
        어떤 오류가 나도 루프를 멈추지 않는다 — 텔레그램이 죽어도 매매는 계속돼야 한다.
        """
        while not stop_event.is_set():
            try:
                r = self._post(self.n.url("getUpdates"), {"timeout": "25", "offset": str(self._offset)}, timeout=35)
                for upd in r.get("result", []):
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    reply = self.handle(upd)
                    if reply:
                        self._post(self.n.url("sendMessage"), {"chat_id": self.n.chat_id, "text": reply[:4000]})
            except Exception as exc:
                logging.getLogger("kimp.notify.quiet").debug("텔레그램 명령 받기 실패: %s", exc)
                stop_event.wait(5)

    def start(self, stop_event: threading.Event) -> threading.Thread:
        """명령 받기를 데몬 스레드로 시작 (메인이 끝나면 같이 끝난다)"""
        t = threading.Thread(target=self.run, args=(stop_event,), name="telegram-cmd", daemon=True)
        t.start()
        return t
