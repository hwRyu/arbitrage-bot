"""
익절 실행 여유 (TP_EXEC_BUFFER) — "밀릴 만큼을 미리 얹어서 더 높은 데서 판다". 고정값 없이 그 순간 호가판에서만 계산

왜 필요한가 (BLEND #10, 2026-10-24): 판단 때 순손익 +0.3% 로 익절했는데 0.7초 사이 업비트 매수벽이
3칸 빠져 −0.25% 로 체결됐다. 청산은 김프가 "튀는 순간" 이라 호가가 얇고 빠르게 움직인다 —
그날 청산 다리 밀림은 거의 매번 불리했고(−0.12 ~ −1.58%), 진입 다리는 거의 0 이었다.

익절 조건: 순손익 ≥ 목표 + 여유.   여유 = 업비트 항 + 바이비트 항, 거래소마다 같은 식:

    지금 평균가 = 우리 수량 Q 를 맨 앞부터 체결한 평균
    밀린 평균가 = (배수 − 1) × Q 를 먼저 걷어낸 뒤, 남은 호가에서 Q 를 체결한 평균
                  = "판단 → 주문 사이에 우리만 한 주문이 (배수 − 1)개 먼저 들어온다" 는 가정
    거래소 항   = |지금 − 밀린| ÷ 지금 × 100

배수 2 = 우리만 한 주문 하나가 앞에 있다. 배수 1 = 아무도 없다 = 여유 0 (끈 것과 같다).
호가 한 칸·상수 같은 고정값은 없다 — 호가 간격은 호가판 줄 간격에 이미 들어 있다. 맨 앞 줄이 걷어낼 양보다
두꺼우면 여유 0 이라 예전과 같고, 얇을 때만 익절이 늦어진다 (대신 손실로 끝나는 익절이 없어진다).

업비트는 매수호가(코인을 파는 쪽), 바이비트는 매도호가(숏을 닫으려고 사는 쪽).
검증 (BLEND 호가 81.3×200 / 81.2×150 / 81.1×300 / 81.0×500, Q=329): 배수 2 → 0.19%, 배수 3 → 0.32%, 실제 0.37%.
그래서 판단 때 업비트 매수호가 상위 5단을 로그에 남긴다 — 익절이 쌓이면 실제 밀림과 비교해 배수를 정한다.
끄면(TP_EXEC_BUFFER_ENABLED=false) 여유 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from kimp.premium import Level, Quote, estimate_fill

BOOK_LEVELS_TO_LOG = 5   # 판단 때 기록해 두는 업비트 매수호가 줄 수


@dataclass(frozen=True)
class ExitBuffer:
    """익절 실행 여유의 구성 (모두 %)"""
    upbit_percent: float     # 업비트 매수호가에서 코인을 팔 때
    bybit_percent: float     # 바이비트 매도호가에서 숏을 닫을 때
    top_levels: Tuple[Level, ...] = ()   # 판단 때 업비트 매수호가 상위 몇 줄 (기록용)

    @property
    def total(self) -> float:
        return self.upbit_percent + self.bybit_percent

    def describe(self) -> str:
        return f"여유 {self.total:.3f}%p = 업비트 {self.upbit_percent:.3f} + 바이비트 {self.bybit_percent:.3f}"

    def book_text(self) -> str:
        return " ".join(f"{p:g}×{q:g}" for p, q in self.top_levels)


def consume(levels: Sequence[Level], qty: float) -> List[Level]:
    """호가판에서 앞쪽 qty 만큼을 걷어낸 나머지 (남이 먼저 체결하고 간 뒤의 호가판)"""
    remaining = qty
    out: List[Level] = []
    for price, size in levels:
        if remaining >= size:
            remaining -= size
            continue
        out.append((price, size - remaining))
        remaining = 0.0
    # remaining 이 남았으면(호가가 모자람) 아무것도 안 남는다
    return out


def displaced_slippage_percent(levels: Sequence[Level], qty: float, multiplier: float) -> float:
    """
    우리 수량 qty 앞에 (multiplier − 1) × qty 가 먼저 체결되면, 우리 평균가가 지금보다 얼마나 나빠지나 (%)

    걷어내고 나서 호가가 우리 수량을 다 못 받아 주면 "마지막 줄 가격까지 다 쓸었다" 고 본다 (보수적).
    파는 쪽(매수호가)은 내려가는 게, 사는 쪽(매도호가)은 올라가는 게 나쁘다 — 절대값.
    """
    if qty <= 0 or not levels or multiplier <= 1:
        return 0.0
    now = estimate_fill(levels, qty)
    if now is None or now.filled_qty <= 0 or now.avg_price <= 0:
        return 0.0
    rest = consume(levels, qty * (multiplier - 1))
    if not rest:
        later_avg = levels[-1][0]
    else:
        later = estimate_fill(rest, qty)
        later_avg = later.avg_price if later and later.is_complete else levels[-1][0]
    return abs(later_avg - now.avg_price) / now.avg_price * 100


def exit_buffer(settings, quote: Optional[Quote], upbit_qty: float, bybit_qty: float) -> Optional[ExitBuffer]:
    """이 시세에서 이 포지션을 청산할 때의 익절 실행 여유. 꺼져 있거나 호가가 없으면 None (= 여유 0)"""
    if not getattr(settings, "tp_exec_buffer_enabled", False) or quote is None:
        return None
    bids, asks = quote.upbit.bids, quote.bybit.asks
    if not bids:
        return None
    mult = settings.tp_stress_qty_multiplier
    return ExitBuffer(upbit_percent=displaced_slippage_percent(bids, upbit_qty, mult),
                      bybit_percent=displaced_slippage_percent(asks, bybit_qty, mult),
                      top_levels=tuple(bids[:BOOK_LEVELS_TO_LOG]))


def buffer_total(buf: Optional[ExitBuffer]) -> float:
    """None(꺼짐) 이면 0"""
    return buf.total if buf else 0.0
