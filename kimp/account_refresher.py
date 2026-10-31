"""
계좌 갱신 스레드 — 거래소 잔고·숏 포지션을 판단 루프 **밖에서** 받아 두는 전담 스레드 (docs/issues.md N7, 2026-10-31)

왜 있나
    실거래 봇은 2초마다 업비트 잔고·바이비트 잔고·바이비트 포지션(코인 수만큼)을 REST 로 받았고, 그 호출이 판단 루프와
    같은 스레드였다. 바이비트가 응답을 안 주면 호출마다 타임아웃(처음 10초, 10-31 부터 5초)까지 기다려 스캔이 24~87초 멈췄다
    (10-28 48번·최대 93초, 10-30 28번, 10-31 18:00~18:50 네 번·최대 79.7초). 멈춘 동안 익절·강제청산 감시가 서고 신호 필터
    시계열도 끊긴다. 정상일 때도 그 스캔은 약 340ms 라 판단 8번 중 1번이 0.3초 늦었다.

어떻게
    이 스레드가 자기 ccxt 객체(키 들어간 별도 인스턴스 — ccxt 는 스레드 안전이 아니다)로 2초마다 한 바퀴 받아 **스냅샷**을
    갈아 끼운다. 스냅샷은 통째로 바꾸므로(불변 객체 교체) 읽는 쪽은 잠금 없이 일관된 값을 본다. 실패하면 옛 스냅샷을 그대로
    두고 성공 시각만 안 올린다 → 읽는 쪽은 "얼마나 오래된 값인가(age)" 로 판단한다.

    실행기(LiveExecutor)는 스캔 앞의 prefetch 에서 이 스냅샷을 가져다 쓴다. 단 **주문을 낸 뒤**에는 그 거래소 값이 낡았으므로
    (dirty), 주문 뒤에 시작한 바퀴의 스냅샷이 올 때까지는 예전처럼 그 자리에서 REST 로 받는다 — 주문 흐름의 정확성은 그대로.

    엔진의 규칙(kimp/engine.py):
      - 스냅샷 나이 ≤ ACCOUNT_STALE_SEC(10초): 지금처럼 판단
      - 넘으면: 신규 진입 보류, '숏 사라짐' 판정 보류 (조회에 성공한 값으로만 판정한다 — 오판하면 헷지를 깬다),
        익절·손절·경계 위 청산은 마지막 값으로 계속 (주문 시 거래소가 거절하면 기존 경로대로)
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, Iterable, Optional, Set

from kimp.market_data import bybit_symbol

logger = logging.getLogger(__name__)

FAIL_LOG_COOLDOWN_SEC = 60.0


@dataclass(frozen=True)
class AccountSnapshot:
    """한 바퀴 받은 결과. 교체만 하고 고치지 않는다 (읽는 쪽이 잠금 없이 써도 되게)"""
    up_bal: Optional[dict] = None          # ccxt fetch_balance (업비트)
    by_bal: Optional[dict] = None          # ccxt fetch_balance (바이비트)
    shorts: Dict[str, float] = field(default_factory=dict)          # 코인 → 숏 개수 (받은 코인만)
    liq: Dict[str, Optional[float]] = field(default_factory=dict)   # 코인 → 거래소 강제청산가
    up_started: float = -1.0               # 이 업비트 값을 받기 시작한 시각 (monotonic). -1 = 아직 없음
    up_at: float = -1.0                    # 받은 시각
    by_started: float = -1.0
    by_at: float = -1.0
    up_error: str = ""                     # 마지막 실패 문구 (성공하면 빈 문자열)
    by_error: str = ""


class AccountRefresher:
    """
    잔고·숏 스냅샷을 주기적으로 갈아 끼우는 스레드

    upbit / bybit: 키가 들어간 ccxt 객체 (주문용과 **다른 인스턴스**여야 한다)
    interval_sec: 바퀴 간격. kick() 으로 즉시 한 바퀴 더 돌릴 수 있다 (주문 뒤)
    """

    def __init__(self, upbit, bybit, interval_sec: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.up = upbit
        self.by = bybit
        self.interval_sec = interval_sec
        self._clock = clock
        self._snap = AccountSnapshot()
        self._watch: Set[str] = set()
        self._watch_lock = threading.Lock()
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fail_log: Dict[str, float] = {}
        self._failing: Dict[str, bool] = {}

    # ------------------------------------------------------------------ 읽기
    def snapshot(self) -> AccountSnapshot:
        return self._snap

    def age(self) -> float:
        """가장 오래된 쪽(업비트/바이비트) 스냅샷 나이 (초). 한 번도 못 받았으면 inf"""
        snap = self._snap
        now = self._clock()
        ages = [now - t if t >= 0 else float("inf") for t in (snap.up_at, snap.by_at)]
        return max(ages)

    # ------------------------------------------------------------------ 제어
    def watch(self, coins: Iterable[str]) -> None:
        """숏을 받아 둘 코인 (열린 포지션). 매 스캔 실행기가 알려준다"""
        with self._watch_lock:
            self._watch = set(coins)

    def kick(self) -> None:
        """지금 바로 한 바퀴 (주문 뒤 값이 낡았을 때)"""
        self._kick.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="account-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    # ------------------------------------------------------------------ 한 바퀴
    def cycle(self) -> AccountSnapshot:
        """
        한 바퀴: 업비트 잔고 ∥ (바이비트 잔고 → 포지션들). 테스트에서는 이걸 직접 부른다

        어느 쪽이 실패해도 다른 쪽은 반영하고, 실패한 쪽은 옛 값을 그대로 둔다 (성공 시각만 안 올라간다)
        """
        started = self._clock()
        with self._watch_lock:
            coins = sorted(self._watch)

        def up_side():
            return self.up.fetch_balance()

        def by_side():
            bal = self.by.fetch_balance()
            shorts: Dict[str, float] = {}
            liq: Dict[str, Optional[float]] = {}
            for coin in coins:
                qty, lp = 0.0, None
                for p in self.by.fetch_positions([bybit_symbol(coin)]):
                    if p.get("side") != "short":
                        continue
                    qty += abs(float(p.get("contracts") or 0.0))
                    try:
                        lp = float(p.get("liquidationPrice") or 0) or None
                    except (TypeError, ValueError):
                        lp = None
                shorts[coin] = qty
                liq[coin] = lp
            return bal, shorts, liq

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="account") as pool:
            fu = pool.submit(up_side)
            fb = pool.submit(by_side)
            up_res, up_err = self._result(fu, "upbit")
            by_res, by_err = self._result(fb, "bybit")

        now = self._clock()
        snap = self._snap
        changes = {}
        if up_err is None:
            changes.update(up_bal=up_res, up_started=started, up_at=now, up_error="")
        else:
            changes.update(up_error=up_err)
        if by_err is None:
            bal, shorts, liq = by_res
            changes.update(by_bal=bal, shorts=shorts, liq=liq, by_started=started, by_at=now, by_error="")
        else:
            changes.update(by_error=by_err)
        self._snap = replace(snap, **changes)
        return self._snap

    def _result(self, future, name: str):
        """future 결과와 오류 문구. 실패는 60초에 한 번만 경고, 다시 되면 알림"""
        try:
            value = future.result()
        except Exception as exc:   # noqa: BLE001 — 어떤 오류든 이 스레드는 죽지 않는다
            text = str(exc)
            now = self._clock()
            if now - self._last_fail_log.get(name, -1e18) >= FAIL_LOG_COOLDOWN_SEC:
                self._last_fail_log[name] = now
                logger.warning("[계좌 갱신 실패] %s | %s | 마지막 성공값으로 계속 (엔진은 나이로 판단)", name, text[:200])
            self._failing[name] = True
            return None, text
        if self._failing.get(name):
            self._failing[name] = False
            logger.info("[계좌 갱신 복구] %s", name)
        return value, None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:   # noqa: BLE001
                logger.warning("[계좌 갱신 스레드 오류] %s", exc)
            self._kick.wait(self.interval_sec)
            self._kick.clear()
