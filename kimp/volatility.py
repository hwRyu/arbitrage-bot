"""
직전 몇 초 바이비트 가격 범위 — 변동성 가드 1단계: 기록만 (docs/issues.md #5, #1 E)

왜: 실거래 바이비트 밀림 7건 중 6건이 바이비트가 크게 출렁이던 때였다 (BLEND #21 익절 +0.647%, CAP #16 진입 +0.210% …).
급등락 순간엔 바이비트가 먼저 튀어 김프가 한순간 기준을 넘고, 1초 안에 되돌아가 바이비트 다리가 밀린다.
막는 기준(몇 초에 몇 %)을 정하려면 진입·익절 때마다 그 직전 출렁임을 쌓아야 한다 → 지금은 기록만 하고 매매는 바꾸지 않는다.

    범위(%) = (직전 WINDOW_SEC 초 바이비트 가운데 가격의 최고 − 최저) ÷ 최저 × 100

예) 평소 XPL·MINA 진입 직전 10초 0.01~0.07%, CAP #16 진입 직전 10초 0.90% (바이비트 공개 체결 기록으로 잰 값).
엔진이 매 스캔·확인 시세마다 record() 하고, 진입·익절 판단 때 describe() 를 로그와 decisions note 에 붙인다.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional, Tuple

WINDOW_SEC = 10.0


class RangeTracker:
    """코인별 최근 WINDOW_SEC 초 가격만 들고 있다가 범위를 낸다 (메모리: 코인당 수십 개)"""

    def __init__(self, window_sec: float = WINDOW_SEC):
        self.window_sec = window_sec
        self._points: Dict[str, Deque[Tuple[float, float]]] = {}

    def record(self, coin: str, now: float, price: Optional[float]) -> None:
        """가격 하나 기록 (None·0 은 무시). 창 밖으로 나간 옛 값은 버린다"""
        if not price or price <= 0:
            return
        q = self._points.setdefault(coin, deque())
        q.append((now, price))
        edge = now - self.window_sec
        while q and q[0][0] < edge:
            q.popleft()

    def range_percent(self, coin: str, now: float) -> Optional[Tuple[float, int]]:
        """(범위 %, 값 개수). 창 안 값이 2개 미만이면 None"""
        q = self._points.get(coin)
        if not q:
            return None
        edge = now - self.window_sec
        prices = [p for t, p in q if t >= edge]
        if len(prices) < 2:
            return None
        lo, hi = min(prices), max(prices)
        return (hi / lo - 1) * 100, len(prices)

    def describe(self, coin: str, now: float) -> str:
        """로그용. 예) '바이비트 10초 범위 0.042% (31개)' / 값이 모자라면 '바이비트 10초 범위 ?'"""
        r = self.range_percent(coin, now)
        if r is None:
            return f"바이비트 {self.window_sec:g}초 범위 ?"
        return f"바이비트 {self.window_sec:g}초 범위 {r[0]:.3f}% ({r[1]}개)"
