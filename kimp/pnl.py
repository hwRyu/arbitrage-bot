"""
헷지 포지션 손익 계산 (모든 비용 포함, 원화 기준)

이 파일은 계산만 한다. (테스트하기 쉬움) 기준 문서: docs/concept.md 5번

포지션 구조
------------------------------------------------------------------
    업비트: 원화로 코인 매수 (롱)
    바이비트: 같은 금액만큼 USDT 무기한 숏 (레버리지 1배)

손익은 어떻게 계산하나? (원화 기준)
------------------------------------------------------------------
원화는 업비트에, USDT 는 바이비트에 그대로 남는다. 둘을 합쳐 보려고
바이비트 쪽 USDT 손익만 달러 환율로 원화로 바꿔서 더한다.

    업비트 손익(원)   = 업비트 개수 × (판 가격 - 산 가격)
    바이비트 손익(원) = 바이비트 개수 × (숏 친 가격 - 숏 닫은 가격) × 청산 때 달러 환율
    비용(원)          = 업비트 수수료 + 바이비트 수수료 × 환율 + 전송비 × 환율
    펀딩비(원)        = 받은(+)/낸(-) 펀딩비 USDT × 청산 때 달러 환율
    순손익(원)        = 업비트 손익 + 바이비트 손익 + 펀딩비 - 비용

예) 진입: 업비트 748.28개를 1,770.7원에, 바이비트 740.74개를 1.35 USDT 에 숏 (달러 환율 1,325원)
    청산: 업비트 1,806.7원, 바이비트 1.35 USDT (김프 -1.0% → +1.0%)
    업비트 손익 = 748.28 × 36 = +26,904원, 바이비트 손익 = 0
    → 비용 빼기 전 +26,904원 (+2.03%)
"""
from __future__ import annotations

from dataclasses import dataclass

from kimp.config import FeeSettings


@dataclass(frozen=True)
class EntryLegs:
    """
    진입할 때 정해지는 값들

    Attributes:
        upbit_qty: 업비트에서 산 개수
        bybit_qty: 바이비트에서 숏 친 개수
        upbit_price_krw: 업비트 평균 매수가 (원)
        bybit_price_usdt: 바이비트 평균 숏 진입가 (USDT)
        fx_krw: 진입 때 달러 환율
        upbit_fee_krw: 업비트 매수 수수료 (원)
        bybit_fee_usdt: 바이비트 진입 수수료 (USDT)
    """

    upbit_qty: float
    bybit_qty: float
    upbit_price_krw: float
    bybit_price_usdt: float
    fx_krw: float
    upbit_fee_krw: float
    bybit_fee_usdt: float

    @property
    def notional_usdt(self) -> float:
        """바이비트 숏 금액 (USDT) = 개수 × 진입가"""
        return self.bybit_qty * self.bybit_price_usdt

    @property
    def notional_krw(self) -> float:
        """
        포지션 규모 (원) = 바이비트 숏 금액 × 진입 때 달러 환율

        수익률(%)을 계산할 때 나누는 값이다.
        금액 헷징이라 업비트에서 산 금액과 거의 같다.
        """
        return self.notional_usdt * self.fx_krw

    @property
    def upbit_cost_krw(self) -> float:
        """업비트에서 실제로 쓴 원화 (수수료 제외)"""
        return self.upbit_qty * self.upbit_price_krw


@dataclass(frozen=True)
class ExitLegs:
    """
    청산할 때의 값들 (실제로 청산했든, '지금 청산하면' 가정이든 같은 모양)

    Attributes:
        upbit_price_krw: 업비트 평균 매도가 (원)
        bybit_price_usdt: 바이비트 평균 숏 종료가 (USDT)
        fx_krw: 청산 때 달러 환율
        upbit_fee_krw: 업비트 매도 수수료 (원)
        bybit_fee_usdt: 바이비트 청산 수수료 (USDT)
    """

    upbit_price_krw: float
    bybit_price_usdt: float
    fx_krw: float
    upbit_fee_krw: float
    bybit_fee_usdt: float


@dataclass(frozen=True)
class PnLBreakdown:
    """
    손익 상세 (모두 원화, 비용은 양수로 적는다)

    순손익 = 업비트 손익 + 바이비트 손익 + 펀딩비 - 비용 합계
    """

    upbit_leg_krw: float  # 업비트 현물 손익 (판 가격 - 산 가격)
    bybit_leg_usdt: float  # 바이비트 숏 손익 (USDT 그대로, 참고용)
    bybit_leg_krw: float  # 바이비트 숏 손익 (원화 환산)
    funding_krw: float  # 펀딩비 (+ 받음 / - 냄)
    upbit_fee_krw: float  # 업비트 수수료 (산 것 + 판 것)
    bybit_fee_krw: float  # 바이비트 수수료 (연 것 + 닫은 것)
    transfer_fee_krw: float  # 사이클당 전송비
    notional_krw: float  # 포지션 규모 (수익률 계산용)

    @property
    def gross_krw(self) -> float:
        """비용 빼기 전 손익 = 업비트 손익 + 바이비트 손익"""
        return self.upbit_leg_krw + self.bybit_leg_krw

    @property
    def total_cost_krw(self) -> float:
        """비용 합계 = 업비트 수수료 + 바이비트 수수료 + 전송비"""
        return self.upbit_fee_krw + self.bybit_fee_krw + self.transfer_fee_krw

    @property
    def net_krw(self) -> float:
        """최종 순손익 (원)"""
        return self.gross_krw + self.funding_krw - self.total_cost_krw

    @property
    def net_percent(self) -> float:
        """순손익률 (%) = 순손익 ÷ 포지션 규모"""
        if self.notional_krw <= 0:
            return 0.0
        return self.net_krw / self.notional_krw * 100


def upbit_fee_krw(price_krw: float, qty: float, fees: FeeSettings) -> float:
    """업비트 수수료 (원) = 거래 금액 × 수수료율.  예) 1,325,000원 × 0.05% = 662.5원"""
    return price_krw * qty * fees.upbit_trade_fee_rate


def bybit_fee_usdt(price_usdt: float, qty: float, fees: FeeSettings) -> float:
    """바이비트 수수료 (USDT) = 거래 금액 × 수수료율.  예) 1,000 USDT × 0.055% = 0.55 USDT"""
    return price_usdt * qty * fees.bybit_taker_fee_rate


def compute_pnl(entry: EntryLegs, exit_: ExitLegs, funding_usdt: float, fees: FeeSettings) -> PnLBreakdown:
    """
    한 번의 진입~청산 손익 계산

    Args:
        entry: 진입 값들
        exit_: 청산 값들 (실제 또는 '지금 청산하면')
        funding_usdt: 보유하는 동안 쌓인 펀딩비 (USDT, + 받음)
        fees: 비용 설정 (전송비를 여기서 가져온다)
    """
    # 업비트: 원화로 사고 원화로 판다 → 그대로 원화 손익
    upbit_leg_krw = entry.upbit_qty * (exit_.upbit_price_krw - entry.upbit_price_krw)

    # 바이비트: 숏이라 가격이 내려야 이익 → USDT 손익을 청산 때 환율로 원화 환산
    bybit_leg_usdt = entry.bybit_qty * (entry.bybit_price_usdt - exit_.bybit_price_usdt)
    bybit_leg_krw = bybit_leg_usdt * exit_.fx_krw

    # 바이비트 수수료는 각각 낸 시점의 환율로 원화 환산
    bybit_fee_krw_total = entry.bybit_fee_usdt * entry.fx_krw + exit_.bybit_fee_usdt * exit_.fx_krw

    return PnLBreakdown(
        upbit_leg_krw=upbit_leg_krw,
        bybit_leg_usdt=bybit_leg_usdt,
        bybit_leg_krw=bybit_leg_krw,
        funding_krw=funding_usdt * exit_.fx_krw,
        upbit_fee_krw=entry.upbit_fee_krw + exit_.upbit_fee_krw,
        bybit_fee_krw=bybit_fee_krw_total,
        transfer_fee_krw=fees.transfer_fee_usdt_per_cycle * exit_.fx_krw,
        notional_krw=entry.notional_krw,
    )


def estimated_exit_premium(
    entry_premium: float,
    target_net_profit_percent: float,
    notional_usdt: float,
    funding_usdt: float,
    fees: FeeSettings,
) -> float:
    """
    "김프가 대략 몇 % 까지 올라야 목표 수익이 나나?" 를 추정 (로그에 보여주는 용도)

    대략 이렇게 된다:
        필요한 청산 김프 ≈ 진입 김프 + 목표 수익률 + 왕복 수수료율 + 전송비 비율 + 슬리피지 여유 - 펀딩 수익률

    예) 진입 -1.0%, 목표 1.0%, 수수료 0.21%, 전송비 1 USDT / 1,000 USDT = 0.1%, 여유 0.05%
        → 약 +0.36% 가 되면 청산

    실제 청산은 이 추정치가 아니라 compute_pnl 로 계산한 순이익으로 정한다.
    """
    if notional_usdt <= 0:
        raise ValueError("포지션 규모는 0 보다 커야 합니다")
    transfer_percent = fees.transfer_fee_usdt_per_cycle / notional_usdt * 100
    funding_percent = funding_usdt / notional_usdt * 100
    return (
        entry_premium
        + target_net_profit_percent
        + fees.round_trip_trade_fee_percent()
        + transfer_percent
        + fees.slippage_buffer_percent
        - funding_percent
    )
