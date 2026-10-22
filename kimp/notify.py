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
  /close    강제 청산 (/close 또는 /close CAP,SEI)   /collect  수집 현황 (/collect start 40 · stop)
  /help     명령 목록

  명령에 인자를 붙일 수 있다 (/close CAP,SEI). 핸들러가 인자를 받으면 넘겨준다.

준비: 텔레그램 @BotFather 에서 봇을 만들어 토큰 받기 → 봇에게 아무 말이나 보낸 뒤
      https://api.telegram.org/bot<토큰>/getUpdates 에서 chat.id 확인 → .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# INFO 로그 중에서도 보낼 것 (메시지가 이걸로 시작하면)
INFO_TAGS = ("[진입]", "[청산", "[송금", "[펀딩비 진입]", "[펀딩비 청산]", "[복구]", "[신규 진입 재개]", "[하루 요약]")


def _post(url: str, data: Dict[str, str], timeout: float = 10.0) -> dict:
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class TelegramNotifier:
    """텔레그램으로 보내기 (큐 + 보내는 스레드)"""

    def __init__(self, token: str, chat_id: str, prefix: str = "", post: Callable = _post):
        self.token = token
        self.chat_id = str(chat_id)
        self.prefix = prefix
        self._post = post
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._sent_at: Dict[str, float] = {}
        self._window: list = []
        self._thread = threading.Thread(target=self._worker, name="telegram-send", daemon=True)
        self._thread.start()

    def url(self, method: str) -> str:
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
        while True:
            text = self._q.get()
            try:
                self._post(self.url("sendMessage"), {"chat_id": self.chat_id, "text": text})
            except Exception as exc:  # 알림 실패로 매매를 멈추지 않는다
                logging.getLogger("kimp.notify.quiet").debug("텔레그램 보내기 실패: %s", exc)

    def send_now(self, text: str) -> bool:
        """바로 보내고 성공 여부 (live_check 테스트 메시지용)"""
        try:
            r = self._post(self.url("sendMessage"), {"chat_id": self.chat_id, "text": f"{self.prefix}{text}"})
            return bool(r.get("ok"))
        except Exception as exc:
            logger.warning("텔레그램 보내기 실패: %s", exc)
            return False


class TelegramLogHandler(logging.Handler):
    """로그 → 텔레그램 (경고 이상, 또는 INFO_TAGS 로 시작하는 INFO)"""

    def __init__(self, notifier: TelegramNotifier):
        super().__init__(level=logging.INFO)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("kimp.notify"):
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        if record.levelno >= logging.WARNING or msg.startswith(INFO_TAGS):
            icon = "🚨 " if record.levelno >= logging.CRITICAL else ("⚠️ " if record.levelno >= logging.WARNING else "")
            self.notifier.send(icon + msg)


class TelegramCommands:
    """텔레그램 명령 받기 (getUpdates 길게 기다리기, 따로 스레드)"""

    def __init__(self, notifier: TelegramNotifier, handlers: Dict[str, Callable[..., str]],
                 post: Optional[Callable] = None):
        self.n = notifier
        self.handlers = handlers
        self._post = post or notifier._post
        self._offset = 0

    def handle(self, update: dict) -> Optional[str]:
        """업데이트 하나 처리 → 답장 (허용 안 된 채팅이면 None)"""
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
        t = threading.Thread(target=self.run, args=(stop_event,), name="telegram-cmd", daemon=True)
        t.start()
        return t
