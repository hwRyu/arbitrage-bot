"""
익절 실행 여유 (TP_EXEC_BUFFER) — "밀릴 만큼을 미리 얹어서 더 높은 데서 판다"

왜 필요한가 (BLEND #10, 2026-10-24): 판단 때 순손익 +0.3% 로 익절했는데 0.7초 사이 업비트 매수벽이
3칸 빠져 −0.25% 로 체결됐다. 청산은 김프가 "튀는 순간" 이라 호가가 얇고 빠르게 움직인다 —
오늘 청산 다리 밀림은 거의 매번 불리했고(−0.12 ~ −1.58%), 진입 다리는 거의 0 이었다.

익절 조건: 순손익 ≥ 목표 + 여유.  여유는 고정값이 아니라 **그 순간 호가판** 에서 매번 계산한다:

    여유 = max(업비트 한 칸 %, 스트레스 밀림 %) + 바이비트 노이즈(설정, 기본 0.05)

    업비트 한 칸 % = 호가 한 칸(원) ÷ 맨 앞 매수호가 × 100.  맨 앞 한 줄만 빠져도 최소 이만큼 밀린다
                     (10 → 100 → 1,000원 경계마다 한 칸이 10배로 뛴다: 99.9원 0.10% / 100원 1.0%)
    스트레스 밀림 % = 우리 수량 × 배수(기본 2) 로 호가를 걸었을 때 평균가가, 우리 수량 평균가보다 얼마나 나쁜가.
                     "판단 → 주문 사이에 우리만 한 매도가 하나 더 끼어든다" 는 가정. 두꺼우면 0, 얇으면 커진다.

두꺼운 코인은 여유 ≈ 0.05 라 지금과 거의 같고, 얇은 코인에서만 익절이 늦어진다 (대신 손실로 끝나는 익절이 없어진다).
한계: 호가가 통째로 사라지는 순간(BLEND 처럼 3줄이 한꺼번에 빠짐)은 배수 2 로는 못 잡는다 — 그래서 판단 때 호가 상위
5단을 로그에 남겨, 나중에 배수를 맞출 수 있게 한다. 끄면(TP_EXEC_BUFFER_ENABLED=false) 여유 0 = 예전 동작.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from kimp.premium import Level, Quote, estimate_fill

BOOK_LEVELS_TO_LOG = 5   # 판단 때 기록해 두는 업비트 매수호가 줄 수


@dataclass(frozen=True)
class ExitBuffer:
    """익절 실행 여유의 구성 (모두 %)"""
    tick_percent: float      # 업비트 한 칸
    stress_percent: float    # 스트레스 밀림
    bybit_percent: float     # 바이비트 노이즈 (설정)
    top_levels: Tuple[Level, ...] = ()   # 판단 때 업비트 매수호가 상위 몇 줄 (기록용)

    @property
    def total(self) -> float:
        return max(self.tick_percent, self.stress_percent) + self.bybit_percent

    def describe(self) -> str:
        return (f"여유 {self.total:.3f}%p = max(한 칸 {self.tick_percent:.3f}, 스트레스 {self.stress_percent:.3f}) "
                f"+ 바이비트 {self.bybit_percent:.2f}")

    def book_text(self) -> str:
        return " ".join(f"{p:g}×{q:g}" for p, q in self.top_levels)


def book_tick(levels: Sequence[Level]) -> Optional[float]:
    """호가창에서 한 칸(원) 알아내기 = 이어진 호가 가격 차이 중 가장 작은 것 (줄이 하나뿐이면 모름 → None)"""
    diffs = [round(abs(b[0] - a[0]), 10) for a, b in zip(levels, levels[1:])]
    diffs = [d for d in diffs if d > 1e-12]
    return min(diffs) if diffs else None


def stress_slippage_percent(bids: Sequence[Level], qty: float, multiplier: float) -> float:
    """
    우리 수량 × 배수를 팔 때 평균가가 우리 수량 평균가보다 얼마나 나쁜가 (%)

    호가가 배수만큼을 못 받아 주면 "마지막 줄까지 다 쓸었다" 고 보고 마지막 호가 가격을 쓴다 (보수적).
    """
    if qty <= 0 or not bids:
        return 0.0
    ours = estimate_fill(bids, qty)
    if ours is None or ours.filled_qty <= 0 or ours.avg_price <= 0:
        return 0.0
    stressed = estimate_fill(bids, qty * multiplier)
    if stressed is None:
        return 0.0
    worse_avg = stressed.avg_price if stressed.is_complete else min(stressed.avg_price, bids[-1][0])
    return max(0.0, (ours.avg_price - worse_avg) / ours.avg_price * 100)


def exit_buffer(settings, quote: Optional[Quote], upbit_qty: float) -> Optional[ExitBuffer]:
    """
    이 시세에서 이 포지션(업비트 코인 개수)을 팔 때의 익절 실행 여유. 꺼져 있거나 호가가 없으면 None (= 여유 0)
    """
    if not getattr(settings, "tp_exec_buffer_enabled", False) or quote is None:
        return None
    bids = quote.upbit.bids
    if not bids or bids[0][0] <= 0:
        return None
    tick = book_tick(bids)
    tick_percent = tick / bids[0][0] * 100 if tick else 0.0
    stress = stress_slippage_percent(bids, upbit_qty, settings.tp_stress_qty_multiplier)
    return ExitBuffer(tick_percent=tick_percent, stress_percent=stress,
                      bybit_percent=settings.tp_bybit_buffer_percent,
                      top_levels=tuple(bids[:BOOK_LEVELS_TO_LOG]))


def buffer_total(buf: Optional[ExitBuffer]) -> float:
    """None(꺼짐) 이면 0"""
    return buf.total if buf else 0.0
