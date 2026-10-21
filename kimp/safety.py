"""
안전장치: 신규 진입을 막아야 하는 상황인지 판단 (페이퍼·실거래 똑같이)

막는 이유 (하나라도 있으면 신규 진입 안 함. 가진 포지션 관리·청산은 계속)
------------------------------------------------------------------
- 비상정지 파일(STOP_FILE, 기본 data/STOP)이 있음 → 지우면 재개
- 텔레그램 /stop → /resume 으로 재개
- 주문이 연속 MAX_CONSECUTIVE_FAILURES 번 실패 → /resume 또는 재시작으로 재개
- 사람이 확인해야 하는 포지션(ERROR)이 있음 → 거래소에서 정리하고 DB 상태를 고친 뒤 재개
- 대조 불일치 (거래소 실제 잔고·포지션 ≠ DB) → 다음 대조에서 맞으면 자동 재개
- 하루(한국 시간 0시부터) 확정 손실이 DAILY_LOSS_LIMIT_USDT 를 넘음 → 다음 날 자동 재개
  예) 한도 10 USDT, 오늘 −4, −7 → 합 −11 → 오늘은 그만
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kimp.db import Position, PositionStatus

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def kst_midnight_utc(now_sec: float) -> datetime:
    """오늘 한국 시간 0시를 UTC 로. 예) 한국 10/22 15:00 → 10/21 15:00 UTC"""
    kst = datetime.fromtimestamp(now_sec, KST)
    return kst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


class Safety:
    """신규 진입 허용 여부. 엔진이 진입 전에 entries_allowed() 를 묻는다"""

    def __init__(self, max_consecutive_failures: int = 3, daily_loss_limit_usdt: Optional[float] = None,
                 stop_file: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.max_failures = max_consecutive_failures
        self.daily_loss_limit = daily_loss_limit_usdt
        self.stop_file = stop_file
        self._clock = clock
        self.manual_stop = False
        self.failures = 0
        self._blocks: Dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings, clock: Callable[[], float] = time.time) -> "Safety":
        return cls(settings.max_consecutive_failures, settings.daily_loss_limit_usdt, settings.stop_file, clock)

    # 다른 곳에서 알려주는 것들
    def block(self, key: str, reason: str) -> None:
        """이유(key)별로 막기. 같은 key 를 clear 하면 풀린다"""
        if self._blocks.get(key) != reason:
            logger.critical("[신규 진입 중단] %s", reason)
        self._blocks[key] = reason

    def clear(self, key: str) -> None:
        if self._blocks.pop(key, None) is not None:
            logger.warning("[신규 진입 재개] %s 해결", key)

    def record_failure(self, what: str) -> None:
        self.failures += 1
        if self.failures == self.max_failures:
            logger.critical("[신규 진입 중단] 주문 연속 %d번 실패 (마지막: %s) → 확인 후 /resume", self.failures, what)

    def record_success(self) -> None:
        self.failures = 0

    def resume(self) -> None:
        """사람이 재개 (텔레그램 /resume): 수동 정지·연속 실패 횟수만 푼다"""
        self.manual_stop = False
        self.failures = 0

    def daily_loss_usdt(self, session: Session) -> float:
        """오늘(한국 시간) 확정 손익 합 (청산 + 되돌린 진입)"""
        since = kst_midnight_utc(self._clock())
        total = session.scalar(
            select(func.coalesce(func.sum(Position.net_pnl_usdt), 0.0)).where(
                Position.status.in_([PositionStatus.CLOSED, PositionStatus.ABORTED]),
                Position.closed_at >= since)
        )
        return float(total or 0.0)

    def entries_allowed(self, session: Session) -> Tuple[bool, str]:
        """(허용?, 막힌 이유)"""
        if self.stop_file and os.path.exists(self.stop_file):
            return False, f"비상정지 파일 {self.stop_file} 있음 (지우면 재개)"
        if self.manual_stop:
            return False, "텔레그램 /stop (재개: /resume)"
        if self.failures >= self.max_failures:
            return False, f"주문 연속 {self.failures}번 실패 (재개: /resume)"
        if self._blocks:
            return False, next(iter(self._blocks.values()))
        bad = session.scalar(select(Position.id).where(Position.status == PositionStatus.ERROR).limit(1))
        if bad is not None:
            return False, f"확인이 필요한 포지션 #{bad} (ERROR) — 거래소에서 직접 정리 후 상태를 고쳐야 함"
        if self.daily_loss_limit:
            loss = self.daily_loss_usdt(session)
            if loss <= -self.daily_loss_limit:
                return False, f"오늘 손실 {loss:+.2f} USDT 가 한도 −{self.daily_loss_limit:g} 를 넘음 (내일 재개)"
        return True, ""
