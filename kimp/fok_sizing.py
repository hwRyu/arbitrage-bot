"""
업비트 지정가 FOK 진입 크기와 안전 확인 (docs/issues.md #3 A·B)

업비트 코인 진입 매수는 지정가 FOK 다 (전량 즉시 체결 아니면 취소). 놓침을 줄이려고 두 가지를 한다.

A. 지정가 = 우리 수량이 닿는 마지막 매도 호가 줄의 가격 (평균가 내림이 아니라)
   지정가는 '이 가격까지는 사겠다' 는 상한이라 거래소는 싼 줄부터 채우고, 체결 평균은 판단한 평균 그대로다.
   평균가를 내림해 내면 주문이 두 줄에 걸칠 때 첫 줄 물량만 쓸 수 있어 늘 놓쳤다.
   대신 주문 직전에 앞쪽 싼 줄이 먼저 팔려 나가면 전부 마지막 줄 가격에 살 수도 있다
   → 안전 확인: "전부 그 가격에 사도 진입 기준을 넘나" 를 판단 때 본다.

B. 주문 크기 ≤ 지정가 안쪽 물량 × 비율 (ENTRY_MAX_DEPTH_SHARE_PERCENT, 기본 50%)
   '지정가 안쪽' = 그 가격에 전부 사도 진입 기준을 넘는 가장 비싼 매도 호가 줄까지. 그 물량 합의 비율까지만 산다.
   남이 0.5초 사이 일부를 먼저 사 가도 남은 물량으로 전량 체결될 여유를 두는 것이다. 넘으면 숏 개수를 줄인다.
   비율은 줄마다가 아니라 합에 건다 — 업비트 주문 하나는 가격 상한 하나와 수량 하나뿐이고, 줄 사이 배분은
   거래소가 싼 줄부터 채우며 정한다.

예) 매도 호가 88.0원 3,000개 / 88.1원 5,000개 / 88.2원 10,000개, 88.1원까지는 전부 그 가격에 사도 기준을 넘고
    88.2원은 못 넘음 → 안쪽 물량 8,000개 → 50% 는 4,000개 → 원래 5,680개였으면 4,000개로 줄임
    → 4,000개는 88.0원 3,000 + 88.1원 1,000 → 지정가 88.1원, 예상 평균 88.025원.
    누가 88.0원 3,000개를 먼저 사 가도 88.1원 5,000개로 우리 4,000개가 다 채워진다.

익절 매도는 A 만 쓴다 (엔진의 익절 판단에서 안전 확인). 포지션은 코인·숏을 같이 다 닫아야 해서 B 처럼 일부만 팔 수 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from kimp.premium import Level, Quote, premium_percent

DEFAULT_DEPTH_SHARE_PERCENT = 50.0


@dataclass(frozen=True)
class EntryFit:
    """
    진입 크기 맞추기 결과

    Attributes:
        status: 'ok' / 'depth_reject'(호가 모자람) / 'depth_fail'(평균 김프가 기준 밖) / 'worst_fail'(안전 확인 실패)
        bybit_qty: 최종 숏 개수 (B 로 줄었을 수 있음)
        premium: 평균 체결가로 낸 진입 김프 (판단값)
        worst_premium: 코인을 전부 마지막 줄 가격에 샀을 때 진입 김프 (안전 확인)
        limit_price: 지정가 후보 = 마지막으로 닿는 매도 호가 줄 가격 (호가 단위 맞추기 전)
        max_price: 전부 그 가격에 사도 진입 기준을 넘는 가장 비싼 매도 호가 줄
        within_qty: max_price 안쪽 물량 합 (코인 개수)
        coin_qty: 사려는 코인 개수 (최종)
        original_qty: 처음 숏 개수 (줄였으면 bybit_qty 와 다름)
    """
    status: str
    bybit_qty: float
    premium: Optional[float] = None
    worst_premium: Optional[float] = None
    limit_price: Optional[float] = None
    max_price: Optional[float] = None
    within_qty: float = 0.0
    coin_qty: float = 0.0
    original_qty: float = 0.0

    @property
    def shrunk(self) -> bool:
        """B 로 숏 개수를 줄였나"""
        return self.bybit_qty < self.original_qty * (1 - 1e-9)

    def describe(self) -> str:
        """로그용 한 줄"""
        return (f"지정가 후보 {self.limit_price:g} · 안전 확인 김프 {self.worst_premium:+.3f}% · "
                f"기준 지키는 최고가 {self.max_price:g} · 안쪽 물량 {self.within_qty:,.6g}개 · 코인 {self.coin_qty:,.6g}개")


def max_acceptable_price(levels: Sequence[Level], ok: Callable[[float], bool]) -> Optional[float]:
    """
    좋은 가격부터 줄을 보며 ok(가격) 가 참인 마지막 줄 가격 (첫 줄부터 거짓이면 None)

    매수에서 가격이 비쌀수록 김프가 나빠지므로 한 번 거짓이 되면 뒤는 볼 필요가 없다.
    """
    best = None
    for price, _size in levels:
        if not ok(price):
            break
        best = price
    return best


def fit_entry(quote: Quote, bybit_qty: float, fee_rate: float, threshold: float,
              share_percent: float, round_qty: Callable[[float], float], max_rounds: int = 4) -> EntryFit:
    """
    진입 숏 개수를 B 로 맞추고, A 의 지정가 후보와 안전 확인 김프를 낸다

    Args:
        quote: 그 순간 시세
        bybit_qty: 처음 정한 숏 개수 (포지션 크기 ÷ 바이비트 가격)
        fee_rate: 업비트 코인 요율
        threshold: 진입 기준 김프 (이 값 이하면 진입)
        share_percent: B 비율 (%). 100 이면 '안쪽 물량을 넘지만 않게' (안전 확인만)
        round_qty: 바이비트 주문 단위로 내림하는 함수
    """
    original = bybit_qty
    plan = quote.entry_plan(bybit_qty, fee_rate)
    if plan is None or not plan.is_complete:
        return EntryFit("depth_reject", bybit_qty, original_qty=original)

    # 기준을 지키는 가장 비싼 매도 호가 줄 (바이비트·USDT 는 이 크기의 평균가 — 줄이면 더 좋아지니 보수적)
    asks = quote.upbit.asks
    by_avg, us_avg = plan.bybit.avg_price, plan.usdt.avg_price
    max_price = max_acceptable_price(asks, lambda p: premium_percent(p, by_avg, us_avg) <= threshold)
    if max_price is None:   # 맨 앞 줄부터 기준 밖 → 어떤 크기로도 안 된다
        return EntryFit("depth_fail", bybit_qty, premium=plan.premium, original_qty=original)
    within = sum(size for price, size in asks if price <= max_price)
    # 순서가 중요하다: 크기를 먼저 안쪽 물량에 맞춘 뒤 평균 김프를 본다. 원래 크기로는 기준 밖 줄까지 먹어서
    # 평균이 기준을 못 넘어도, 안쪽 물량 안으로 줄이면 넘을 수 있다 (그 기회를 버리지 않는다)
    cap = within * share_percent / 100.0

    for _ in range(max_rounds):
        if plan.upbit.filled_qty <= cap * (1 + 1e-9):
            break
        try:
            smaller = round_qty(bybit_qty * cap / plan.upbit.filled_qty * 0.999)   # 0.999 = 가격 변화로 다시 넘지 않게
        except Exception:
            # 거래소 주문 단위보다 작게 줄어들면 ccxt 가 예외를 던진다 (GMT 2026-10-30 06:50: 안쪽 물량이 거의 사라진 호가에서
            # 1개 미만 → 'must be greater than minimum amount precision of 1'). 예외가 스캔 전체를 멈추면 보유 포지션 관리까지
            # 건너뛰므로, 여기서 '이 크기로는 못 들어감' 으로 끝낸다
            smaller = 0.0
        if smaller <= 0 or smaller >= bybit_qty:
            return EntryFit("depth_reject", bybit_qty, premium=plan.premium, max_price=max_price,
                            within_qty=within, original_qty=original)
        bybit_qty = smaller
        plan = quote.entry_plan(bybit_qty, fee_rate)
        if plan is None or not plan.is_complete:
            return EntryFit("depth_reject", bybit_qty, original_qty=original)
    else:
        return EntryFit("depth_reject", bybit_qty, premium=plan.premium, max_price=max_price,
                        within_qty=within, original_qty=original)

    worst = plan.upbit.worst_price
    worst_premium = premium_percent(worst, plan.bybit.avg_price, plan.usdt.avg_price)
    if plan.premium > threshold:
        status = "depth_fail"
    elif worst_premium > threshold:
        status = "worst_fail"
    else:
        status = "ok"
    return EntryFit(status, bybit_qty, premium=plan.premium, worst_premium=worst_premium, limit_price=worst,
                    max_price=max_price, within_qty=within, coin_qty=plan.upbit.filled_qty, original_qty=original)
