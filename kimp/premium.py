"""
김프 계산 + 호가창으로 실제 체결가 미리 계산하기

이 파일은 계산만 한다. (거래소 접속, DB 저장 없음 → 테스트하기 쉬움)
전략의 기준은 docs/concept.md 이다.

김프란? (우리 봇 기준 = 업비트 USDT 가격 기준 '코인 몫' 김프)
------------------------------------------------------------------
    김프(%) = (업비트 코인 원화 가격 ÷ (바이비트 USDT 가격 × 업비트 USDT 원화 가격) - 1) × 100

    예) 업비트 XRP 1,795원, 바이비트 XRP 1.345 USDT, 업비트 USDT 1,333원
        1.345 × 1,333 = 1,792.9원 → 1,795 ÷ 1,792.9 - 1 = +0.12%

    업비트 USDT 가격으로 나누니까 테더 프리미엄과 달러 환율이 빠진 '코인 자체' 의 김프다.
    김프 사이트 숫자(달러 환율 기준, 테더 포함)와 다르다: 사이트 김프 ≈ 이 값 + 테더 프리미엄

업비트 쪽 매매 = USDT → 코인 → USDT 한 바퀴
------------------------------------------------------------------
    진입: USDT/KRW 에서 USDT 를 판다 (매수 호가 bids) → 받은 원화로 코인을 산다 (매도 호가 asks)
    청산: 코인을 판다 (bids) → 받은 원화로 USDT 를 산다 (asks)
    바이비트: 진입 때 숏 (bids 에 판다), 청산 때 숏 닫기 (asks 에서 산다)

그래서 '진입 김프' 는 살 때 가격들로, '청산 김프' 는 팔 때 가격들로 계산한다.
둘의 차이가 세 군데(업비트 코인, 업비트 USDT, 바이비트) 호가 간격 비용이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# 호가 한 줄: (가격, 수량)
Level = Tuple[float, float]

# ----------------------------------------------------------------------
# 업비트 원화 마켓 호가 단위 (한 칸)
# ----------------------------------------------------------------------
# 가격대별 한 칸. 실거래로 확인한 값: CAP 140 → 141원 (1원), PYTH 100 → 101원 (1원), ZAMA 93.5원은 0.1원.
# (상한, 한 칸) — 가격이 상한 '미만' 이면 그 칸. 예) 99.9원 → 0.1원, 100원 → 1원, 999원 → 1원, 1,000원 → 5원
_UPBIT_KRW_TICKS = ((0.1, 0.0001), (1, 0.001), (10, 0.01), (100, 0.1), (1_000, 1), (10_000, 5),
                    (100_000, 10), (500_000, 50), (1_000_000, 100), (2_000_000, 500))


def upbit_krw_tick(price: float) -> float:
    """업비트 원화 마켓에서 이 가격의 호가 한 칸 (원). 예) 140원 → 1원 (= 0.71%), 93.5원 → 0.1원 (= 0.11%)"""
    for upper, tick in _UPBIT_KRW_TICKS:
        if price < upper:
            return tick
    return 1_000.0


def near_tick_boundary(price: float, margin_percent: float) -> Optional[Tuple[float, float, float]]:
    """
    호가 단위가 커지는 경계 바로 아래(또는 딱 경계)인가 — 그렇다면 (경계, 아래 한 칸, 위 한 칸)

    업비트는 100원 미만이면 한 칸 0.1원, 100원부터 1원이다 (1,000·10,000원 등도 같다). 매도 호가가 경계 바로 아래나
    딱 경계에 있으면 지금 왕복 호가는 좁아서 스프레드 가드를 통과하지만, 그 줄이 팔리면 다음 줄이 한 번에 큰 칸 위다.
    그래서 주문이 닿기 전에 놓치거나(CAP #27~#30 2026-10-30: 47초에 4번), 들고 있다가 경계를 넘으면 팔 때 한 칸이
    커져 익절이 어려워진다 (docs/issues.md #4).

    판정: 경계 B 에 대해 B × (1 − margin%) ≤ 가격 ≤ B. margin 0 이면 보지 않음 (None).
    예) margin 1% → 99.0 ~ 100원이 '100원 경계 근처'. 98.9원·101원은 아님 (101원은 왕복 호가가 넓어 스프레드 가드가 막는다)
    """
    if margin_percent <= 0 or price <= 0:
        return None
    for (upper, tick), (_next_upper, next_tick) in zip(_UPBIT_KRW_TICKS, _UPBIT_KRW_TICKS[1:]):
        if upper * (1 - margin_percent / 100) <= price <= upper:
            return upper, tick, next_tick
    last_upper, last_tick = _UPBIT_KRW_TICKS[-1]
    if last_upper * (1 - margin_percent / 100) <= price <= last_upper:
        return last_upper, last_tick, 1_000.0
    return None


def tick_boundaries_crossed(low: float, high: float) -> List[float]:
    """[low, high] 가격 구간이 넘나드는 업비트 호가 단위 경계들 (예: 93~103원 → [100]). 스크리너의 '경계왕래' 판정용"""
    if low <= 0 or high < low:
        return []
    return [upper for upper, _tick in _UPBIT_KRW_TICKS if low < upper <= high]


def upbit_limit_price(price: float, side: str) -> float:
    """
    지정가를 호가 단위에 맞춘다 — 판단한 가격보다 나쁘게는 절대 안 되게

        사는 쪽("buy")  : 내림. 140.37원으로 판단했으면 140원 (141원에 사면 판단보다 0.7% 비싸다)
        파는 쪽("sell") : 올림. 140.37원이면 141원

    판단가는 실제 호가 줄의 평균이라 보통 이미 한 칸 위에 있고, 그때는 그대로 나온다 (부동소수점 오차만 정리).
    """
    tick = upbit_krw_tick(price)
    n = price / tick
    k = math.floor(n + 1e-9) if side == "buy" else math.ceil(n - 1e-9)
    return round(k * tick, 10)


@dataclass(frozen=True)
class OrderBook:
    """
    호가창 한 장

    Attributes:
        bids: 사겠다는 주문들. 비싼 가격부터 (맨 앞이 가장 높은 매수 호가)
        asks: 팔겠다는 주문들. 싼 가격부터 (맨 앞이 가장 낮은 매도 호가)
        timestamp_ms: 거래소가 알려준 시각
    """

    bids: Sequence[Level]
    asks: Sequence[Level]
    timestamp_ms: Optional[int] = None

    @property
    def best_bid(self) -> Optional[float]:
        """지금 바로 팔면 받는 가격 (가장 높은 매수 호가)"""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """지금 바로 사면 내는 가격 (가장 낮은 매도 호가)"""
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        """가운데 가격 = (best_bid + best_ask) ÷ 2. 화면 표시용 '시세'"""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


def premium_percent(upbit_krw: float, bybit_usdt: float, krw_per_usdt: float) -> float:
    """
    김프(%) 계산

    Args:
        upbit_krw: 업비트 코인 가격 (원)
        bybit_usdt: 바이비트 코인 가격 (USDT)
        krw_per_usdt: 1 USDT 를 몇 원으로 볼지.
                      봇 판단에는 업비트 USDT 가격을 넣는다 (코인 몫 김프).
                      달러 환율을 넣으면 김프 사이트와 같은 '사이트식 김프' (테더 포함, 표시용)

    Returns:
        김프(%). +면 업비트가 비쌈, -면 업비트가 쌈

    Example:
        premium_percent(1795, 1.345, 1333) → 약 +0.12
    """
    if bybit_usdt <= 0 or krw_per_usdt <= 0:
        raise ValueError(f"가격이 올바르지 않습니다: 바이비트 {bybit_usdt}, 원/USDT {krw_per_usdt}")
    return (upbit_krw / (bybit_usdt * krw_per_usdt) - 1) * 100


def tether_premium_percent(usdt_krw: float, fx_krw: float) -> float:
    """
    테더 프리미엄(%) = 업비트 USDT 가격이 달러 환율보다 얼마나 비싼지

    예) 업비트 USDT 1,332원, 달러 환율 1,325원 → +0.53%
    봇의 매매 판단에는 쓰지 않고, 로그에 보여주기만 한다. (USDT 사고팔기는 사용자 담당)
    """
    return (usdt_krw / fx_krw - 1) * 100


@dataclass(frozen=True)
class FillEstimate:
    """
    "지금 시장가로 주문하면 이렇게 체결된다" 는 계산 결과

    Attributes:
        avg_price: 평균 체결가
        filled_qty: 체결되는 코인 개수
        best_price: 맨 앞 호가 가격 (얼마나 밀렸는지 비교 기준)
        is_complete: 원하는 만큼 다 채워지는지 (호가가 모자라면 False)
        worst_price: 마지막으로 닿은 호가 줄의 가격 (사면 가장 비싼 줄, 팔면 가장 싼 줄).
            업비트 지정가 FOK 의 지정가로 쓴다 — 평균가로 내면 두 칸에 걸칠 때 첫 칸 물량만 쓸 수 있어 늘 놓친다
            (docs/issues.md #3 A). 지정가는 상한이라 거래소는 싼 줄부터 채우고, 체결 평균은 avg_price 그대로다
    """

    avg_price: float
    filled_qty: float
    best_price: float
    is_complete: bool
    worst_price: Optional[float] = None

    @property
    def cost(self) -> float:
        """체결 금액 = 평균가 × 개수 (업비트면 원, 바이비트면 USDT)"""
        return self.avg_price * self.filled_qty

    @property
    def slippage_percent(self) -> float:
        """
        맨 앞 호가보다 평균 체결가가 몇 % 나빠졌는지 (슬리피지, 항상 0 이상)

        예) 맨 앞 매도 호가 100원인데 수량이 많아서 평균 101원에 샀다 → 1%
        """
        return abs(self.avg_price / self.best_price - 1) * 100


# 계산 오차 허용치 (아주 작은 소수점 차이는 '다 채웠다'로 본다)
_EPS = 1e-9


def estimate_fill(levels: Sequence[Level], qty: float) -> Optional[FillEstimate]:
    """
    '개수'를 정해서 시장가 주문할 때의 체결 계산

    바이비트 숏 열기/닫기, 업비트에서 팔기처럼 '몇 개' 를 정해서 주문할 때 쓴다.
    사는 주문이면 asks, 파는 주문이면 bids 를 넣는다.

    예) asks = [(100원, 1개), (101원, 1개), (102원, 10개)] 에서 3개 사기
        → 100 + 101 + 102 = 303원, 평균 101원

    Returns:
        FillEstimate. 호가가 비었거나 개수가 0 이하면 None.
        호가가 모자라면 채운 만큼만 계산하고 is_complete=False.
    """
    if qty <= 0 or not levels:
        return None

    remaining = qty  # 아직 못 채운 개수
    cost = 0.0
    filled = 0.0
    worst = None     # 마지막으로 가져간 줄의 가격
    for price, size in levels:
        if remaining <= qty * _EPS:
            break
        take = min(size, remaining)  # 이 줄에서 가져갈 개수
        if take <= 0:
            continue
        cost += price * take
        filled += take
        remaining -= take
        worst = price

    if filled <= 0:
        return None
    return FillEstimate(
        avg_price=cost / filled,
        filled_qty=filled,
        best_price=levels[0][0],
        is_complete=filled >= qty * (1 - _EPS),
        worst_price=worst,
    )


def estimate_fill_by_cost(levels: Sequence[Level], cost: float) -> Optional[FillEstimate]:
    """
    '금액'을 정해서 시장가로 살 때의 체결 계산

    업비트 시장가 매수는 원래 "몇 원어치 사줘" 방식이다.
    금액 헷징에서 업비트를 바이비트 숏 금액만큼 살 때 쓴다.

    예) asks = [(100원, 1개), (200원, 10개)] 에서 200원어치 사기
        → 100원짜리 1개(100원) + 200원짜리 0.5개(100원) = 1.5개, 평균 133.33원

    Returns:
        FillEstimate. 호가가 비었거나 금액이 0 이하면 None.
        호가가 모자라면 채운 만큼만 계산하고 is_complete=False.
    """
    if cost <= 0 or not levels:
        return None

    remaining = cost  # 아직 못 쓴 금액
    spent = 0.0
    filled = 0.0
    worst = None      # 마지막으로 돈을 쓴 줄의 가격
    for price, size in levels:
        if remaining <= cost * _EPS:
            break
        # 이 호가 줄을 전부 먹을 수도 있고(price x size), 남은 금액만큼만 먹을 수도 있다
        take_cost = min(price * size, remaining)  # 이 줄에서 쓸 금액
        if take_cost <= 0:
            continue
        filled += take_cost / price               # 금액 ÷ 그 줄의 가격 = 받는 개수
        spent += take_cost
        remaining -= take_cost
        worst = price

    if filled <= 0:
        return None
    return FillEstimate(
        avg_price=spent / filled,      # 금액 가중 평균가 (단순 평균이 아니다)
        filled_qty=filled,
        best_price=levels[0][0],       # 맨 앞 호가 — 여기와 비교해 밀림(slippage)을 낸다
        # 부동소수 오차 때문에 딱 맞아도 cost 보다 조금 작게 나올 수 있어 _EPS 만큼 여유를 둔다
        is_complete=spent >= cost * (1 - _EPS),
        worst_price=worst,
    )


@dataclass(frozen=True)
class EntryPlan:
    """
    "지금 진입하면 이렇게 체결된다" 계산 결과 (업비트 2단계 + 바이비트)

    Attributes:
        bybit: 바이비트 숏 (매수 호가에 판다)
        usdt: 업비트 USDT 매도 (USDT/KRW 매수 호가에 판다)
        upbit: 업비트 코인 매수 (받은 원화로, 매도 호가에서 산다)
        usdt_sold: 판 USDT 개수 (= 바이비트 숏 금액)
        usdt_fee_krw: USDT 팔 때 수수료 (원)
        coin_fee_krw: 코인 살 때 수수료 (원, 주문 금액과 별도로 빠짐)
    """

    bybit: FillEstimate
    usdt: FillEstimate
    upbit: FillEstimate
    usdt_sold: float
    usdt_fee_krw: float
    coin_fee_krw: float

    @property
    def is_complete(self) -> bool:
        """세 군데 모두 호가가 충분해서 다 채워지는지"""
        return self.bybit.is_complete and self.usdt.is_complete and self.upbit.is_complete

    @property
    def premium(self) -> float:
        """
        실제 체결가로 계산한 진입 김프 (%) — 세 평균 체결가를 공식에 넣는다

        가운데 가격으로 낸 김프보다 **항상 불리하다**(높다). 세 군데 모두 불리한 쪽
        호가를 쓰기 때문이다. 그 차이가 호가 간격 비용이다.
        """
        return premium_percent(self.upbit.avg_price, self.bybit.avg_price, self.usdt.avg_price)


@dataclass(frozen=True)
class ExitPlan:
    """
    "지금 청산하면 이렇게 체결된다" 계산 결과

    Attributes:
        upbit: 업비트 코인 매도 (매수 호가에 판다)
        usdt: 업비트 USDT 매수 (받은 원화로, USDT/KRW 매도 호가에서 산다)
        bybit: 바이비트 숏 닫기 (매도 호가에서 산다)
        coin_fee_krw: 코인 팔 때 수수료 (원)
        usdt_fee_krw: USDT 살 때 수수료 (원)
        usdt_bought: 되산 USDT 개수
    """

    upbit: FillEstimate
    usdt: FillEstimate
    bybit: FillEstimate
    coin_fee_krw: float
    usdt_fee_krw: float
    usdt_bought: float

    @property
    def is_complete(self) -> bool:
        """세 군데 모두 호가가 충분한지. 하나라도 모자라면 청산 판단을 보류한다"""
        return self.bybit.is_complete and self.usdt.is_complete and self.upbit.is_complete

    @property
    def premium(self) -> float:
        """
        실제 체결가로 계산한 청산 김프 (%)

        가운데 가격보다 **항상 낮다.** '진입 김프 − 청산 김프' 가 바로 왕복 호가 간격 비용이고,
        김프가 그만큼은 올라야 본전이다.
        """
        return premium_percent(self.upbit.avg_price, self.bybit.avg_price, self.usdt.avg_price)


@dataclass(frozen=True)
class Quote:
    """
    코인 하나의 '한 순간' 시세 묶음

    모든 김프 계산은 이 묶음 하나로 한다. 그래서 같은 순간의 값끼리만 비교된다.

    Attributes:
        coin: 코인 이름 (예: XRP)
        upbit: 업비트 코인 원화 마켓 호가창 (원)
        bybit: 바이비트 USDT 무기한 호가창 (USDT)
        usdt: 업비트 USDT/KRW 호가창 (원) ← 김프 계산과 업비트 매매에 쓰는 기준
        fx_krw: 달러 환율 (표시용: 사이트식 김프, 테더 프리미엄). 없을 수도 있음
        bybit_high: 강제청산 확인용 가격 (시뮬레이션에서 1분봉 고가). 평소엔 None → 매도 호가 사용
        skew_ms: 업비트 호가와 바이비트 호가를 받은 시각 차이 (ms, 분석용). 클수록 '시차 김프' 가 섞일 수 있다

    금액 헷징이란?
        바이비트 숏 금액과 같은 개수의 USDT 를 업비트에서 팔아 그 원화로 코인을 산다.
        예) 바이비트 740.7개 × 1.35 = 1,000 USDT 숏 → 업비트에서 USDT 1,000개를 팔아 코인 매수
        그러면 코인 몫 김프가 그대로일 때는 코인 가격, 환율, 테더 값이 어떻게 움직여도 손익이 0 이다.
    """

    coin: str
    upbit: OrderBook
    bybit: OrderBook
    usdt: OrderBook
    fx_krw: Optional[float] = None
    bybit_high: Optional[float] = None
    skew_ms: Optional[int] = None

    # ---------------- 화면 표시용 ----------------
    @property
    def usdt_krw(self) -> Optional[float]:
        """업비트 USDT 가운데 가격"""
        return self.usdt.mid

    @property
    def mid_premium(self) -> Optional[float]:
        """가운데 가격끼리 계산한 김프 (%, 코인 몫). 봇 기준 '시장 김프'"""
        if self.upbit.mid is None or self.bybit.mid is None or self.usdt.mid is None:
            return None
        return premium_percent(self.upbit.mid, self.bybit.mid, self.usdt.mid)

    @property
    def entry_premium_top(self) -> Optional[float]:
        """맨 앞 호가로 계산한 진입 김프 (%): 코인 살 가격 vs 바이비트 숏 가격 × USDT 팔 가격"""
        if None in (self.upbit.best_ask, self.bybit.best_bid, self.usdt.best_bid):
            return None
        return premium_percent(self.upbit.best_ask, self.bybit.best_bid, self.usdt.best_bid)

    @property
    def exit_premium_top(self) -> Optional[float]:
        """맨 앞 호가로 계산한 청산 김프 (%): 코인 팔 가격 vs 바이비트 숏 닫을 가격 × USDT 살 가격"""
        if None in (self.upbit.best_bid, self.bybit.best_ask, self.usdt.best_ask):
            return None
        return premium_percent(self.upbit.best_bid, self.bybit.best_ask, self.usdt.best_ask)

    @property
    def usdt_spread_percent(self) -> Optional[float]:
        """업비트 USDT/KRW 호가 간격 (%). 예) 1,333 / 1,334원 → 0.075%"""
        if self.usdt.best_bid is None or self.usdt.best_ask is None:
            return None
        return (self.usdt.best_ask / self.usdt.best_bid - 1) * 100

    @property
    def tether_premium(self) -> Optional[float]:
        """테더 프리미엄 (%) = 업비트 USDT 가격이 달러 환율보다 얼마나 비싼지 (표시용)"""
        if not self.fx_krw or self.usdt.mid is None:
            return None
        return tether_premium_percent(self.usdt.mid, self.fx_krw)

    @property
    def site_premium(self) -> Optional[float]:
        """사이트식 김프 (%, 달러 환율 기준 = 테더 포함). 김프 사이트 숫자와 비교하는 용도"""
        if not self.fx_krw or self.upbit.mid is None or self.bybit.mid is None:
            return None
        return premium_percent(self.upbit.mid, self.bybit.mid, self.fx_krw)

    @property
    def liquidation_check_price(self) -> Optional[float]:
        """강제청산 확인용 바이비트 가격: 시뮬레이션은 1분봉 고가, 평소엔 매도 호가"""
        return self.bybit_high if self.bybit_high is not None else self.bybit.best_ask

    # ---------------- 실제 판단용 ----------------
    def with_flat_upbit(self, side: str, price: float) -> "Quote":
        """
        업비트 코인 호가 한쪽을 '이 가격 한 줄, 물량 무한' 으로 바꾼 시세 — "전부 이 가격에 체결되면?" 계산용

        업비트 지정가 FOK 안전 확인에 쓴다 (docs/issues.md #3 A). 지정가를 마지막으로 닿는 줄 가격으로 내면,
        주문 직전에 앞쪽 싼 줄이 먼저 팔려 나갈 경우 전부 그 가격에 체결될 수 있다. 그 최악에서도
        진입 김프·익절 순이익이 기준을 넘는지 이 시세로 다시 계산한다.

        Args:
            side: 'asks' (사는 쪽, 진입) / 'bids' (파는 쪽, 익절)
        """
        flat = [(price, float("inf"))]
        upbit = (OrderBook(bids=self.upbit.bids, asks=flat, timestamp_ms=self.upbit.timestamp_ms) if side == "asks"
                 else OrderBook(bids=flat, asks=self.upbit.asks, timestamp_ms=self.upbit.timestamp_ms))
        return Quote(coin=self.coin, upbit=upbit, bybit=self.bybit, usdt=self.usdt, fx_krw=self.fx_krw,
                     bybit_high=self.bybit_high, skew_ms=self.skew_ms)

    def entry_plan(self, bybit_qty: float, upbit_fee_rate: float) -> Optional[EntryPlan]:
        """
        진입하면 어떻게 체결될지 계산 (금액 헷징)

        1) 바이비트: bybit_qty 개 숏 → 숏 금액 N USDT
        2) 업비트: USDT N 개를 USDT/KRW 매수 호가에 판다 → 원화 (수수료 빼고)
        3) 업비트: 그 원화로 코인을 산다 (살 때 수수료는 따로 빠지므로 원화 ÷ (1 + 수수료율) 만큼 주문)

        Returns:
            EntryPlan. 어느 단계든 호가가 비어 있으면 None. 다 못 채우면 is_complete=False
        """
        # 1) 바이비트 숏: 파는 쪽이니 매수 호가(bids)를 소진한다
        by = estimate_fill(self.bybit.bids, bybit_qty)
        if not by:
            return None
        # 금액 헷징의 핵심: 숏 '금액'(개수 x 가격)이 업비트에서 팔 USDT 개수가 된다.
        # 개수를 맞추는 게 아니라 금액을 맞추기 때문에 양쪽 코인 개수는 달라진다.
        usdt_sold = by.cost
        us = estimate_fill(self.usdt.bids, usdt_sold)  # USDT 를 판다 → bids
        if not us:
            return None
        usdt_fee = us.cost * upbit_fee_rate
        # 받은 원화에서 코인을 살 때, 업비트는 **주문 금액과 별도로** 수수료를 더 떼어 간다.
        # 그래서 원화를 그대로 주문하면 수수료만큼 잔고가 모자라 거절된다.
        #   주문금액 x (1 + 수수료율) <= 가진 원화  →  주문금액 = 가진 원화 ÷ (1 + 수수료율)
        krw_for_coin = (us.cost - usdt_fee) / (1 + upbit_fee_rate)
        up = estimate_fill_by_cost(self.upbit.asks, krw_for_coin)  # 코인을 산다 → asks
        if not up:
            return None
        return EntryPlan(bybit=by, usdt=us, upbit=up, usdt_sold=usdt_sold, usdt_fee_krw=usdt_fee,
                         coin_fee_krw=up.cost * upbit_fee_rate)

    def exit_plan(self, upbit_qty: float, bybit_qty: float, upbit_fee_rate: float) -> Optional[ExitPlan]:
        """
        청산하면 어떻게 체결될지 계산 (가진 개수 그대로 정리)

        1) 업비트: 코인 upbit_qty 개를 매수 호가에 판다 → 원화 (수수료 빼고)
        2) 업비트: 그 원화로 USDT 를 산다 (살 때 수수료는 따로 빠짐)
        3) 바이비트: 숏 bybit_qty 개를 매도 호가에서 사서 닫는다
        """
        up = estimate_fill(self.upbit.bids, upbit_qty)
        by = estimate_fill(self.bybit.asks, bybit_qty)
        if not up or not by:
            return None
        coin_fee = up.cost * upbit_fee_rate
        krw_for_usdt = (up.cost - coin_fee) / (1 + upbit_fee_rate)
        us = estimate_fill_by_cost(self.usdt.asks, krw_for_usdt)
        if not us:
            return None
        return ExitPlan(upbit=up, usdt=us, bybit=by, coin_fee_krw=coin_fee,
                        usdt_fee_krw=us.cost * upbit_fee_rate, usdt_bought=us.filled_qty)

    def entry_premium_for(self, bybit_qty: float, upbit_fee_rate: float) -> Optional[float]:
        """
        실제로 진입하면 받게 될 김프 (%) ← 진입 여부는 이 값으로 정한다

        **주문 수량만큼 호가를 실제로 소진해서** 나온 평균 체결가로 계산한다. 그래서 수량이
        크면 호가가 밀린 만큼 김프가 불리하게 나오고, 진입 기준을 그만큼 못 넘긴다.
        '호가 간격을 상수로 더하는' 방식(백테스트용)과 달리 근사가 없다.

        호가가 모자라서 어느 한 군데라도 다 못 채우면 None (= 진입 불가).
        반만 사고 끝내면 한쪽만 열린 포지션이 되므로 아예 진입하지 않는다.
        """
        plan = self.entry_plan(bybit_qty, upbit_fee_rate)
        if plan is None or not plan.is_complete:
            return None
        return plan.premium
