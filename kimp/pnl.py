"""
헷지 포지션 손익 계산 (모든 비용 포함, USDT 기준)

이 파일은 계산만 한다. (테스트하기 쉬움) 기준 문서: docs/concept.md 5번

포지션 구조
------------------------------------------------------------------
    업비트: USDT 를 팔아 원화로 코인을 사고 → 청산 때 코인을 팔아 원화로 USDT 를 되산다
    바이비트: 같은 금액만큼 USDT 무기한 숏

손익은 어떻게 계산하나? (전부 USDT)
------------------------------------------------------------------
    업비트 (USDT)  = 되산 USDT 개수 - 판 USDT 개수   (수수료, 호가 간격까지 다 들어간 실제 개수)
    바이비트 (USDT) = 숏 개수 × (숏 친 가격 - 숏 닫은 가격) - 바이비트 수수료
    펀딩비 (USDT)  = 보유 중 받은(+)/낸(-) 펀딩비
    순손익 (USDT)  = 업비트 + 바이비트 + 펀딩비 - 전송비

예) USDT 1,000 개를 팔아 XRP 를 사고, 바이비트 1,000 USDT 숏 (김프 -1.5%)
    김프가 +0.5% 가 되어 청산 → 업비트에서 USDT 1,020.3 개를 되삼, 바이비트 가격 그대로
    → 업비트 +20.3 USDT, 바이비트 0 → 비용 빼기 전 +20.3 USDT (+2.03%)

업비트 USDT 가격(환율 × 테더 프리미엄)이 움직여도, 판 USDT 와 되산 USDT 로 계산하니 자동으로 상쇄된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kimp.config import FeeSettings


@dataclass(frozen=True)
class EntryLegs:
    """
    진입할 때 정해지는 값들

    Attributes:
        usdt_sold: 업비트에서 판 USDT 개수 (= 바이비트 숏 금액)
        usdt_price_krw: USDT 판 평균가 (원)
        usdt_fee_krw: USDT 팔 때 수수료 (원)
        upbit_qty: 업비트에서 산 코인 개수
        upbit_price_krw: 코인 평균 매수가 (원)
        upbit_fee_krw: 코인 살 때 수수료 (원)
        bybit_qty: 바이비트 숏 개수
        bybit_price_usdt: 바이비트 평균 숏 가격 (USDT)
        bybit_fee_usdt: 바이비트 진입 수수료 (USDT)
    """

    usdt_sold: float
    usdt_price_krw: float
    usdt_fee_krw: float
    upbit_qty: float
    upbit_price_krw: float
    upbit_fee_krw: float
    bybit_qty: float
    bybit_price_usdt: float
    bybit_fee_usdt: float

    @property
    def notional_usdt(self) -> float:
        """포지션 크기 = 바이비트 숏 금액 (USDT). 수익률(%)을 계산할 때 나누는 값"""
        return self.bybit_qty * self.bybit_price_usdt


@dataclass(frozen=True)
class ExitLegs:
    """
    청산할 때의 값들 (실제로 청산했든, '지금 청산하면' 가정이든 같은 모양)

    Attributes:
        upbit_price_krw: 코인 평균 매도가 (원)
        upbit_fee_krw: 코인 팔 때 수수료 (원)
        usdt_price_krw: USDT 되산 평균가 (원)
        usdt_fee_krw: USDT 살 때 수수료 (원)
        usdt_bought: 되산 USDT 개수
        bybit_price_usdt: 바이비트 평균 숏 종료가 (USDT)
        bybit_fee_usdt: 바이비트 청산 수수료 (USDT)
    """

    upbit_price_krw: float
    upbit_fee_krw: float
    usdt_price_krw: float
    usdt_fee_krw: float
    usdt_bought: float
    bybit_price_usdt: float
    bybit_fee_usdt: float


@dataclass(frozen=True)
class PnLBreakdown:
    """
    손익 상세 (모두 USDT. 비용은 양수로 적는다)

    순손익 = 업비트 손익 + 바이비트 손익 + 펀딩비 - 비용 합계
    """

    upbit_leg_usdt: float  # 업비트 손익 (수수료 빼기 전. 호가 간격은 들어 있음)
    bybit_leg_usdt: float  # 바이비트 숏 손익 (수수료 빼기 전)
    funding_usdt: float  # 펀딩비 (+ 받음 / - 냄)
    upbit_fee_usdt: float  # 업비트 수수료 4번 (USDT 매도, 코인 매수, 코인 매도, USDT 매수)
    bybit_fee_usdt: float  # 바이비트 수수료 (열기 + 닫기)
    transfer_fee_usdt: float  # 사이클당 전송비
    notional_usdt: float  # 포지션 크기 (수익률 계산용)

    @property
    def gross_usdt(self) -> float:
        """비용 빼기 전 손익 = 업비트 + 바이비트"""
        return self.upbit_leg_usdt + self.bybit_leg_usdt

    @property
    def total_cost_usdt(self) -> float:
        """비용 합계 = 업비트 수수료 + 바이비트 수수료 + 전송비"""
        return self.upbit_fee_usdt + self.bybit_fee_usdt + self.transfer_fee_usdt

    @property
    def net_usdt(self) -> float:
        """최종 순손익 (USDT)"""
        return self.gross_usdt + self.funding_usdt - self.total_cost_usdt

    @property
    def net_percent(self) -> float:
        """순손익률 (%) = 순손익 ÷ 포지션 크기"""
        if self.notional_usdt <= 0:
            return 0.0
        return self.net_usdt / self.notional_usdt * 100


def upbit_fee_krw(cost_krw: float, fees: FeeSettings, coin: Optional[str] = None) -> float:
    """업비트 수수료 (원) = 거래 금액 × 그 코인의 수수료율 (USDT/KRW 매매는 coin 없이 → 공통값)"""
    return cost_krw * fees.upbit_trade_rate(coin)


def bybit_fee_usdt(price_usdt: float, qty: float, fees: FeeSettings, coin: Optional[str] = None) -> float:
    """바이비트 수수료 (USDT) = 거래 금액 × 그 코인의 수수료율.  예) 1,000 USDT × 0.055% = 0.55 USDT"""
    return price_usdt * qty * fees.bybit_taker_rate(coin)


def compute_pnl(entry: EntryLegs, exit_: ExitLegs, funding_usdt: float, fees: FeeSettings,
                bybit_leg_override_usdt: Optional[float] = None) -> PnLBreakdown:
    """
    한 번의 진입~청산 손익 계산

    Args:
        entry: 진입 값들
        exit_: 청산 값들 (실제 또는 '지금 청산하면')
        funding_usdt: 보유하는 동안 쌓인 펀딩비 (USDT, + 받음)
        fees: 비용 설정 (전송비를 여기서 가져온다)
        bybit_leg_override_usdt: 바이비트 손익을 직접 정할 때 (강제청산: 증거금 전체 손실)
    """
    # 업비트: 실제로 되산 USDT - 판 USDT = 수수료까지 다 빠진 업비트 손익
    #
    # 이 한 줄이 '자본을 USDT 로 재는' 설계의 핵심이다. 중간에 원화와 코인을 거쳤지만
    # **시작도 USDT, 끝도 USDT** 라서 그 차이가 곧 손익이다.
    # 업비트 USDT 가격(테더 프리미엄 포함)은 팔 때와 되살 때 양쪽에 똑같이 들어가 약분되므로,
    # 테더 프리미엄과 달러 환율이 아무리 움직여도 이 값에 섞이지 않는다 (docs/concept.md 3~4번).
    upbit_net = exit_.usdt_bought - entry.usdt_sold

    # 업비트 수수료 4번을 USDT 로 환산 (보여주기용으로 따로 뺀다)
    #   진입: USDT 매도 수수료 + 코인 매수 수수료 → 판 가격으로 환산
    #   청산: 코인 매도 수수료 + USDT 매수 수수료 → 산 가격으로 환산
    upbit_fees = ((entry.usdt_fee_krw + entry.upbit_fee_krw) / entry.usdt_price_krw
                  + (exit_.upbit_fee_krw + exit_.usdt_fee_krw) / exit_.usdt_price_krw)

    # 바이비트: 숏이라 가격이 내려야 이익
    if bybit_leg_override_usdt is not None:
        bybit_leg = bybit_leg_override_usdt
        bybit_fees = entry.bybit_fee_usdt  # 강제청산 땐 닫는 수수료 없이 증거금을 잃은 것으로 본다
    else:
        # 숏 손익 = (진입가 − 청산가) x 개수. 가격이 내려가야 이익이다.
        # 김프가 좁혀질 때 보통 바이비트가 오르므로 이 몫은 손실이고, 업비트 몫이 그보다
        # 더 벌어서 합이 플러스가 되는 구조다 (양쪽이 서로 상쇄하며 가격 위험을 지운다).
        bybit_leg = entry.bybit_qty * (entry.bybit_price_usdt - exit_.bybit_price_usdt)
        bybit_fees = entry.bybit_fee_usdt + exit_.bybit_fee_usdt

    return PnLBreakdown(
        # 수수료를 '업비트 몫' 에 되돌려 더해서 보여주기용으로 따로 뺀다 —
        # upbit_net 에는 이미 수수료가 빠져 있으므로, 더해 주면 '수수료 전' 금액이 된다.
        # 최종 순손익은 PnLBreakdown.net_usdt 에서 다시 빼므로 이중 계산이 아니다.
        upbit_leg_usdt=upbit_net + upbit_fees,  # 수수료 빼기 전 = 실제 결과 + 낸 수수료
        bybit_leg_usdt=bybit_leg,
        funding_usdt=funding_usdt,
        upbit_fee_usdt=upbit_fees,
        bybit_fee_usdt=bybit_fees,
        transfer_fee_usdt=fees.transfer_fee_usdt_per_cycle,
        notional_usdt=entry.notional_usdt,
    )


def liquidation_price(entry_price_usdt: float, leverage: float, mmr: float) -> float:
    """
    바이비트 숏 강제청산 가격 (격리 마진, 대략)

        강제청산 가격 ≈ 진입가 × (1 + 1/레버리지 - 유지증거금률)

    예) 1.35 USDT 숏, 3배, 유지증거금률 0.5% → 1.35 × (1 + 0.333 - 0.005) ≈ 1.793 (약 +32.8%)
        1배면 1.35 × 1.995 ≈ 2.69 (약 2배)
    """
    return entry_price_usdt * (1 + 1 / leverage - mmr)


def liquidation_price_from_margin(entry_price_usdt: float, qty: float, margin_usdt: float, mmr: float) -> float:
    """
    증거금을 더 넣은 뒤의 숏 강제청산 가격 (격리, 대략)

        강제청산 가격 ≈ 진입가 × (1 + 증거금/숏금액 − 유지증거금률)

    liquidation_price(진입가, 레버리지, mmr) 와 같은 식이다 — 증거금/숏금액 = 1/레버리지 이므로.
    예) 1.35 USDT × 740개 = 999 USDT 숏, 증거금 999 (1배) → 1.35 × 1.995 ≈ 2.69
        증거금 1,998 로 올리면 → 1.35 × 2.995 ≈ 4.04
    """
    notional = entry_price_usdt * qty
    if notional <= 0:
        return entry_price_usdt
    return entry_price_usdt * (1 + margin_usdt / notional - mmr)



def estimated_exit_premium(entry_premium: float, target_net_profit_percent: float, notional_usdt: float,
                           funding_usdt: float, fees: FeeSettings, usdt_spread_percent: float = 0.0,
                           coin: Optional[str] = None) -> float:
    """
    "코인 몫 김프가 대략 몇 % 까지 올라야 목표 수익이 나나?" 추정 (로그에 보여주는 용도)

        필요한 청산 김프 ≈ 진입 김프 + 목표 + 왕복 수수료(0.31%) + 전송비 비율 + USDT 호가 간격 + 여유 - 펀딩 수익률

    예) 진입 -1.0%, 목표 1.0%, 전송비 1 USDT / 1,000 USDT = 0.1%, USDT 호가 간격 0.075%, 여유 0.05%
        → -1 + 1 + 0.31 + 0.1 + 0.075 + 0.05 ≈ +0.54%
    실제 청산은 이 추정치가 아니라 compute_pnl 로 계산한 순이익으로 정한다.
    """
    if notional_usdt <= 0:
        raise ValueError("포지션 크기는 0 보다 커야 합니다")
    return (entry_premium + target_net_profit_percent + fees.round_trip_trade_fee_percent(coin)
            + fees.transfer_fee_usdt_per_cycle / notional_usdt * 100 + usdt_spread_percent
            + fees.slippage_buffer_percent - funding_usdt / notional_usdt * 100)
