"""
익절 실행 여유 (TP_EXEC_BUFFER) — "밀릴 만큼을 미리 얹어서 더 높은 데서 판다". 고정값 없이 그 순간 호가판에서만 계산

왜 필요한가 (BLEND #10, 2026-10-24): 판단 때 순손익 +0.3% 로 익절했는데 0.7초 사이 업비트 매수벽이
3칸 빠져 −0.25% 로 체결됐다. 청산은 김프가 "튀는 순간" 이라 호가가 얇고 빠르게 움직인다 —
오늘 청산 다리 밀림은 거의 매번 불리했고(−0.12 ~ −1.58%), 진입 다리는 거의 0 이었다.

익절 조건: 순손익 ≥ 목표 + 여유.   여유 = 업비트 항 + 바이비트 항, 거래소마다 같은 식:

    한 칸 항     = 한 칸 % × min(1, 우리 수량 × 배수 ÷ 맨 앞 줄 수량)
                   "맨 앞 줄이 판단 → 주문 사이에 사라지면 최소 한 칸 밀린다" 를, 맨 앞 줄이 우리보다 얼마나
                   두꺼운지로 깎는다. 맨 앞에 우리 배수의 10배가 쌓여 있으면 한 칸의 1/10 만 본다.
                   한 칸 % = 호가판 이어진 가격 차 최소값 ÷ 맨 앞 가격 (10 → 100 → 1,000원 경계마다 10배로 뛴다)
    스트레스 밀림 = 우리 수량 × 배수(기본 2) 로 호가를 걸었을 때 평균가가, 우리 수량 평균가보다 얼마나 나쁜가.
                   "우리 앞에 우리만 한 주문이 하나 더 끼어든다" 는 가정. 두꺼우면 0, 얇으면 커진다.
    거래소 항    = max(한 칸 항, 스트레스 밀림)

업비트는 매수호가(코인을 파는 쪽), 바이비트는 매도호가(숏을 닫으려고 사는 쪽). 두꺼운 호가판이면 두 항 모두 0 에
가까워 예전과 같고, 얇을 때만 익절이 늦어진다. 끄면(TP_EXEC_BUFFER_ENABLED=false) 여유 0.
한계: 호가가 통째로 사라지는 순간(BLEND 처럼 3줄이 한꺼번에 빠짐)은 배수 2 로는 못 잡는다 — 그래서 판단 때
업비트 매수호가 상위 5단을 로그에 남겨, 나중에 배수를 맞출 수 있게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from kimp.premium import Level, Quote, estimate_fill

BOOK_LEVELS_TO_LOG = 5   # 판단 때 기록해 두는 업비트 매수호가 줄 수


@dataclass(frozen=True)
class SideBuffer:
    """거래소 한쪽의 여유 구성 (모두 %)"""
    tick_percent: float      # 한 칸 % (맨 앞 줄 두께로 깎은 뒤)
    stress_percent: float    # 스트레스 밀림

    @property
    def total(self) -> float:
        return max(self.tick_percent, self.stress_percent)


@dataclass(frozen=True)
class ExitBuffer:
    upbit: SideBuffer
    bybit: SideBuffer
    top_levels: Tuple[Level, ...] = ()   # 판단 때 업비트 매수호가 상위 몇 줄 (기록용)

    @property
    def total(self) -> float:
        return self.upbit.total + self.bybit.total

    def describe(self) -> str:
        return (f"여유 {self.total:.3f}%p = 업비트 max(한 칸 {self.upbit.tick_percent:.3f}, 스트레스 "
                f"{self.upbit.stress_percent:.3f}) + 바이비트 max(한 칸 {self.bybit.tick_percent:.3f}, 스트레스 "
                f"{self.bybit.stress_percent:.3f})")

    def book_text(self) -> str:
        return " ".join(f"{p:g}×{q:g}" for p, q in self.top_levels)


def book_tick(levels: Sequence[Level]) -> Optional[float]:
    """호가창에서 한 칸(원·USDT) 알아내기 = 이어진 호가 가격 차이 중 가장 작은 것 (줄이 하나뿐이면 모름 → None)"""
    diffs = [round(abs(b[0] - a[0]), 10) for a, b in zip(levels, levels[1:])]
    diffs = [d for d in diffs if d > 1e-12]
    return min(diffs) if diffs else None


def stress_slippage_percent(levels: Sequence[Level], qty: float, multiplier: float) -> float:
    """
    우리 수량 × 배수를 체결할 때 평균가가 우리 수량 평균가보다 얼마나 나쁜가 (%)

    호가가 배수만큼을 못 받아 주면 "마지막 줄까지 다 쓸었다" 고 보고 마지막 줄 가격을 쓴다 (보수적).
    파는 쪽(매수호가)은 평균가가 내려가는 게 나쁘고, 사는 쪽(매도호가)은 올라가는 게 나쁘다 — 절대값으로 본다.
    """
    if qty <= 0 or not levels:
        return 0.0
    ours = estimate_fill(levels, qty)
    if ours is None or ours.filled_qty <= 0 or ours.avg_price <= 0:
        return 0.0
    stressed = estimate_fill(levels, qty * multiplier)
    if stressed is None:
        return 0.0
    worse_avg = stressed.avg_price if stressed.is_complete else levels[-1][0]
    return abs(worse_avg - ours.avg_price) / ours.avg_price * 100


def side_buffer(levels: Sequence[Level], qty: float, multiplier: float) -> SideBuffer:
    """거래소 한쪽: 한 칸 항(맨 앞 줄 두께로 깎음) 과 스트레스 밀림"""
    if not levels or levels[0][0] <= 0 or qty <= 0:
        return SideBuffer(0.0, 0.0)
    top_price, top_qty = levels[0]
    tick = book_tick(levels)
    tick_percent = tick / top_price * 100 if tick else 0.0
    thickness = min(1.0, qty * multiplier / top_qty) if top_qty > 0 else 1.0
    return SideBuffer(tick_percent=tick_percent * thickness,
                      stress_percent=stress_slippage_percent(levels, qty, multiplier))


def exit_buffer(settings, quote: Optional[Quote], upbit_qty: float, bybit_qty: float) -> Optional[ExitBuffer]:
    """
    이 시세에서 이 포지션을 청산할 때의 익절 실행 여유. 꺼져 있거나 호가가 없으면 None (= 여유 0)

    업비트는 코인을 파니 매수호가, 바이비트는 숏을 닫으려 사니 매도호가.
    """
    if not getattr(settings, "tp_exec_buffer_enabled", False) or quote is None:
        return None
    bids, asks = quote.upbit.bids, quote.bybit.asks
    if not bids:
        return None
    mult = settings.tp_stress_qty_multiplier
    return ExitBuffer(upbit=side_buffer(bids, upbit_qty, mult), bybit=side_buffer(asks, bybit_qty, mult),
                      top_levels=tuple(bids[:BOOK_LEVELS_TO_LOG]))


def buffer_total(buf: Optional[ExitBuffer]) -> float:
    """None(꺼짐) 이면 0"""
    return buf.total if buf else 0.0
