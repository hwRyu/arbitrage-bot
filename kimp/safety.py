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
from typing import Callable, Dict, List, Optional, Tuple

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
    """
    신규 진입 허용 여부. 엔진이 진입 전에 entries_allowed() 를 묻는다

    **막는 것은 신규 진입뿐이다.** 이미 가진 포지션의 관리·청산·되돌리기는 계속 돌아간다 —
    안전장치가 청산까지 막으면 한쪽만 열린 상태로 갇혀서 더 위험해진다.
    엔진의 한 스캔 순서도 그래서 이 모양이다: 대조 → 관리·청산 → 리밸런싱 → (여기서 물어봄) → 진입.

    상태는 메모리에만 둔다 (DB 에 안 적는다). 봇을 다시 켜면 수동 정지·연속 실패는 풀리고,
    DB 를 보는 두 가지(ERROR 포지션 / 하루 손실)는 다시 켜도 그대로 막힌다 — 의도한 것이다.
    """

    def __init__(self, max_consecutive_failures: int = 3, daily_loss_limit_usdt: Optional[float] = None,
                 stop_file: Optional[str] = None, clock: Callable[[], float] = time.time):
        """
        Args:
            max_consecutive_failures: 주문이 연달아 이만큼 실패하면 막는다
            daily_loss_limit_usdt: 하루 확정 손실 한도 (양수로 적는다). None 이면 안 막음
            stop_file: 이 파일이 생기면 막는다 (기본 data/STOP). 터미널이 없어도 멈출 수 있는 수단
            clock: 지금 시각 함수 (테스트에서 날짜를 바꿔 끼우려고 받는다)
        """
        self.max_failures = max_consecutive_failures
        self.daily_loss_limit = daily_loss_limit_usdt
        self.stop_file = stop_file
        self._clock = clock
        self.manual_stop = False
        self.failures = 0
        self._blocks: Dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings, clock: Callable[[], float] = time.time) -> "Safety":
        """설정(.env)에서 만들기 — 엔진이 쓰는 경로"""
        return cls(settings.max_consecutive_failures, settings.daily_loss_limit_usdt, settings.stop_file, clock)

    # 다른 곳에서 알려주는 것들
    def block(self, key: str, reason: str) -> None:
        """이유(key)별로 막기. 같은 key 를 clear 하면 풀린다"""
        if self._blocks.get(key) != reason:
            logger.critical("[신규 진입 중단] %s", reason)
        self._blocks[key] = reason

    def clear(self, key: str) -> None:
        """
        그 이유로 막힌 것을 푼다 (없던 key 면 아무 일도 없다)

        대조 불일치처럼 '다음 스캔에 맞으면 저절로 풀려야 하는' 것들이 이걸 쓴다.
        """
        if self._blocks.pop(key, None) is not None:
            logger.warning("[신규 진입 재개] %s 해결", key)

    def record_failure(self, what: str) -> None:
        """
        주문 실패 하나 세기 (연속 카운트)

        성공하면 record_success() 로 0 이 된다. 즉 '연속' 실패만 센다 — 하루에 띄엄띄엄
        한 번씩 실패하는 것으로는 안 막힌다.
        거래소 규정 거절(출금지연제 등)은 여기로 오지 않는다. 우리가 고칠 수 없는 것으로
        봇을 멈추면 안 되기 때문이다 (kimp/broker.py 의 is_policy_reject).
        """
        self.failures += 1
        if self.failures == self.max_failures:
            logger.critical("[신규 진입 중단] 주문 연속 %d번 실패 (마지막: %s) → 확인 후 /resume", self.failures, what)

    def record_success(self) -> None:
        """주문 성공 → 연속 실패 횟수를 0 으로"""
        self.failures = 0

    def resume(self) -> List[str]:
        """
        사람이 재개 (텔레그램 /resume): 수동 정지·연속 실패 횟수를 풀고, 비상정지 파일이 있으면 지운다

        서버에 못 들어가는 상황(이동 중)에서도 텔레그램만으로 파일 정지를 풀 수 있어야 한다.
        Returns: 실제로 푼 것들 (답장용). 아무것도 안 막혀 있었으면 빈 목록
        """
        cleared = []
        if self.manual_stop:
            cleared.append("수동 정지")
        if self.failures:
            cleared.append(f"연속 실패 {self.failures}회")
        self.manual_stop = False
        self.failures = 0
        if self.stop_file and os.path.exists(self.stop_file):
            os.remove(self.stop_file)
            cleared.append(f"비상정지 파일 {self.stop_file} 삭제")
        return cleared

    def daily_loss_usdt(self, session: Session) -> float:
        """
        오늘(한국 시간 0시부터) 확정 손익 합 — 손실이면 음수

        CLOSED(정상 청산) 와 ABORTED(진입 실패 되돌림) 만 센다. 아직 들고 있는 포지션의
        평가 손익은 **세지 않는다** — 김프는 되돌아오는 성질이라, 평가손으로 막으면
        회복될 포지션 때문에 매매를 멈추게 된다.
        한국 시간 기준이라 자정이 지나면 저절로 0 부터 다시 센다.
        """
        since = kst_midnight_utc(self._clock())
        total = session.scalar(
            select(func.coalesce(func.sum(Position.net_pnl_usdt), 0.0)).where(
                Position.status.in_([PositionStatus.CLOSED, PositionStatus.ABORTED]),
                Position.closed_at >= since)
        )
        return float(total or 0.0)

    def entries_allowed(self, session: Session) -> Tuple[bool, str]:
        """
        지금 신규 진입을 해도 되나

        위에서부터 순서대로 보고 **처음 걸린 이유 하나만** 돌려준다 (사람이 고칠 것 하나를
        보여주려고). 사람이 직접 멈춘 것(파일·텔레그램)을 먼저 보는 이유: 가장 강한 의사 표시다.

        Returns:
            (허용 여부, 막힌 이유). 허용이면 이유는 빈 글자
        """
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
