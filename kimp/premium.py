"""
김프 계산 + 호가창으로 실제 체결가 미리 계산하기

이 파일은 계산만 한다. (거래소 접속, DB 저장 없음 → 테스트하기 쉬움)
전략의 기준은 docs/concept.md 이다.

김프란? (우리 봇 기준 = 김프 사이트와 같은 방식)
------------------------------------------------------------------
    김프(%) = (업비트 원화 가격 ÷ (바이비트 USDT 가격 × 달러 환율) - 1) × 100

    예) 업비트 XRP 1,770원, 바이비트 XRP 1.35 USDT, 달러 환율 1,325원
        바이비트 가격을 원화로 바꾸면 1.35 × 1,325 = 1,788.75원
        1,770 ÷ 1,788.75 - 1 = -1.05%  → 업비트가 1.05% 싸다 (역프)

    이 값에는 '테더 프리미엄'(업비트에서 USDT 가 달러보다 비싼 정도)이 포함돼 있다.
    원화는 업비트에, USDT 는 바이비트에 그대로 두는 전제에서는 테더 프리미엄 변화도
    실제 돈의 변화라서 일부러 포함한다. (docs/concept.md 4번)

왜 최근 체결가가 아니라 '호가창'으로 계산하나?
------------------------------------------------------------------
실제로 주문을 넣으면 최근 체결가가 아니라 호가창에 걸린 가격에 사고판다.

    진입: 업비트에서 산다   → 매도 호가(asks)에 걸린 물건을 산다
          바이비트에서 숏   → 매수 호가(bids)에 걸린 사람에게 판다
    청산: 업비트에서 판다   → 매수 호가(bids)
          바이비트 숏 종료  → 매도 호가(asks)

그래서 같은 순간이라도 '진입 김프'는 조금 높게, '청산 김프'는 조금 낮게 나온다.
그 차이가 호가 간격(스프레드) 비용이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

# 호가 한 줄: (가격, 수량)
Level = Tuple[float, float]


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


def premium_percent(upbit_krw: float, bybit_usdt: float, fx_krw: float) -> float:
    """
    김프(%) 계산

    Args:
        upbit_krw: 업비트 코인 가격 (원)
        bybit_usdt: 바이비트 코인 가격 (USDT)
        fx_krw: 1 USDT 를 몇 원으로 볼지. 우리 봇은 달러 환율을 넣는다.

    Returns:
        김프(%). +면 업비트가 비쌈(김프), -면 업비트가 쌈(역프)

    Example:
        premium_percent(1770, 1.35, 1325) → 약 -1.05
    """
    if bybit_usdt <= 0 or fx_krw <= 0:
        raise ValueError(f"가격/환율이 올바르지 않습니다: 바이비트 {bybit_usdt}, 환율 {fx_krw}")
    return (upbit_krw / (bybit_usdt * fx_krw) - 1) * 100


def tether_premium_percent(usdt_krw: float, fx_krw: float) -> float:
    """
    테더 프리미엄(%) = 업비트 USDT 가격이 달러 환율보다 얼마나 비싼지

    예) 업비트 USDT 1,332원, 달러 환율 1,325원 → +0.53%
    봇의 매매 판단에는 쓰지 않고, 로그에 보여주기만 한다. (USDT 사고팔기는 사용자 담당)
    """
    return (usdt_krw / fx_krw - 1) * 100


def coin_premium_percent(total_premium: float, tether_premium: float) -> float:
    """
    전체 김프에서 테더 몫을 빼고 남은 '코인 몫' 김프(%)

    전체 김프는 테더 프리미엄과 코인 김프가 곱해져서 만들어진다:
        (1 + 전체) = (1 + 테더) × (1 + 코인)
    그래서 코인 몫 = (1 + 전체) ÷ (1 + 테더) - 1

    예) 전체 김프 +0.49%, 테더 +0.52% → 코인 몫 약 -0.03%
        (이 코인 자체는 USDT 대비 거의 제값이고, 김프는 거의 전부 테더 때문)
    분석과 로그용이다. 매매 판단은 전체 김프로 한다.
    """
    return ((1 + total_premium / 100) / (1 + tether_premium / 100) - 1) * 100


@dataclass(frozen=True)
class FillEstimate:
    """
    "지금 시장가로 주문하면 이렇게 체결된다" 는 계산 결과

    Attributes:
        avg_price: 평균 체결가
        filled_qty: 체결되는 코인 개수
        best_price: 맨 앞 호가 가격 (얼마나 밀렸는지 비교 기준)
        is_complete: 원하는 만큼 다 채워지는지 (호가가 모자라면 False)
    """

    avg_price: float
    filled_qty: float
    best_price: float
    is_complete: bool

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
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(size, remaining)  # 이 줄에서 가져갈 개수
        cost += price * take
        filled += take
        remaining -= take

    if filled <= 0:
        return None
    return FillEstimate(
        avg_price=cost / filled,
        filled_qty=filled,
        best_price=levels[0][0],
        is_complete=filled >= qty * (1 - _EPS),
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
    for price, size in levels:
        if remaining <= 0:
            break
        take_cost = min(price * size, remaining)  # 이 줄에서 쓸 금액
        filled += take_cost / price
        spent += take_cost
        remaining -= take_cost

    if filled <= 0:
        return None
    return FillEstimate(
        avg_price=spent / filled,
        filled_qty=filled,
        best_price=levels[0][0],
        is_complete=spent >= cost * (1 - _EPS),
    )


@dataclass(frozen=True)
class Quote:
    """
    코인 하나의 '한 순간' 시세 묶음

    모든 김프 계산은 이 묶음 하나로 한다. 그래서 같은 순간의 값끼리만 비교된다.

    Attributes:
        coin: 코인 이름 (예: XRP)
        upbit: 업비트 원화 마켓 호가창 (원)
        bybit: 바이비트 USDT 무기한 호가창 (USDT)
        fx_krw: 달러 환율 (1 USDT 를 몇 원으로 볼지) ← 김프와 금액 헷징에 쓰는 값
        usdt_krw: 업비트 USDT 가격 (테더 프리미엄 표시용. 없을 수도 있음)

    금액 헷징이란?
        진입할 때 양쪽 '금액'을 같게 맞춘다.
          1) 바이비트에서 숏 칠 개수를 먼저 정한다 (바이비트는 개수 단위가 정해져 있어서)
          2) 그 숏 금액(USDT) × 달러 환율 만큼의 원화로 업비트에서 산다
        예) 바이비트 740.74개 × 1.35 = 1,000 USDT 숏 → 업비트에서 1,000 × 1,325 = 1,325,000원어치 매수
        이렇게 하면 김프가 그대로일 때는 코인 가격이 올라도 내려도 손익이 0 이다.
    """

    coin: str
    upbit: OrderBook
    bybit: OrderBook
    fx_krw: float
    usdt_krw: Optional[float] = None

    # ---------------- 화면 표시용 김프 ----------------
    @property
    def mid_premium(self) -> Optional[float]:
        """가운데 가격끼리 비교한 김프 (%). 김프 사이트 숫자와 비슷한 값"""
        if self.upbit.mid is None or self.bybit.mid is None:
            return None
        return premium_percent(self.upbit.mid, self.bybit.mid, self.fx_krw)

    @property
    def entry_premium_top(self) -> Optional[float]:
        """
        맨 앞 호가로 계산한 '진입 김프' (%)

        업비트에서 바로 살 가격 vs 바이비트에서 바로 숏 칠 가격.
        개수를 고려하지 않은 대략적인 값이라, 진입 판단은 entry_premium_for 로 한다.
        """
        if self.upbit.best_ask is None or self.bybit.best_bid is None:
            return None
        return premium_percent(self.upbit.best_ask, self.bybit.best_bid, self.fx_krw)

    @property
    def exit_premium_top(self) -> Optional[float]:
        """맨 앞 호가로 계산한 '청산 김프' (%). 업비트에서 바로 팔 가격 vs 바이비트 숏 바로 닫을 가격"""
        if self.upbit.best_bid is None or self.bybit.best_ask is None:
            return None
        return premium_percent(self.upbit.best_bid, self.bybit.best_ask, self.fx_krw)

    @property
    def tether_premium(self) -> Optional[float]:
        """테더 프리미엄 (%). 업비트 USDT 가격이 없으면 None (표시용)"""
        if not self.usdt_krw:
            return None
        return tether_premium_percent(self.usdt_krw, self.fx_krw)

    def split_premium(self, total_premium: float) -> Tuple[Optional[float], Optional[float]]:
        """
        전체 김프를 (테더 몫, 코인 몫) 으로 나눈다 (분석/로그용)

        업비트 USDT 가격이 없으면 둘 다 None.
        예) 전체 -1.0%, 테더 +0.5% → (테더 +0.5%, 코인 약 -1.49%)
        """
        tether = self.tether_premium
        if tether is None:
            return None, None
        return tether, coin_premium_percent(total_premium, tether)

    # ---------------- 실제 판단용 ----------------
    def entry_fills(self, bybit_qty: float) -> Tuple[Optional[FillEstimate], Optional[FillEstimate]]:
        """
        진입하면 양쪽이 어떻게 체결될지 계산 (금액 헷징)

        1) 바이비트: bybit_qty 개 숏 → 매수 호가(bids)에 판다
        2) 업비트: (바이비트 숏 금액 × 달러 환율) 원어치를 매도 호가(asks)에서 산다

        Returns:
            (업비트 매수 계산, 바이비트 숏 계산).
            바이비트를 다 못 채우면 업비트 금액을 정할 수 없어서 업비트 쪽은 None.
        """
        by = estimate_fill(self.bybit.bids, bybit_qty)
        if not by or not by.is_complete:
            return None, by
        up = estimate_fill_by_cost(self.upbit.asks, by.cost * self.fx_krw)
        return up, by

    def exit_fills(self, upbit_qty: float, bybit_qty: float) -> Tuple[Optional[FillEstimate], Optional[FillEstimate]]:
        """
        청산하면 양쪽이 어떻게 체결될지 계산 (가진 개수 그대로 정리)

        Args:
            upbit_qty: 업비트에 가진 개수 → 매수 호가(bids)에 판다
            bybit_qty: 바이비트 숏 개수 → 매도 호가(asks)에서 사서 숏을 닫는다
        """
        return estimate_fill(self.upbit.bids, upbit_qty), estimate_fill(self.bybit.asks, bybit_qty)

    def entry_premium_for(self, bybit_qty: float) -> Optional[float]:
        """
        실제로 진입하면 받게 될 김프 (%) ← 진입 여부는 이 값으로 정한다

        호가가 모자라서 어느 한쪽이라도 다 못 채우면 None (= 진입 불가).
        """
        up, by = self.entry_fills(bybit_qty)
        if not up or not by or not up.is_complete or not by.is_complete:
            return None
        return premium_percent(up.avg_price, by.avg_price, self.fx_krw)
