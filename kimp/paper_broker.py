"""
페이퍼 트레이딩 (가짜 주문, 진짜 호가)

실제 주문은 안 내고, 그 순간의 '진짜 호가창'으로 체결가를 계산해서 잔고만 바꾼다.
그래서 호가가 얇은 코인은 페이퍼에서도 손해(슬리피지)가 그대로 보인다.
(old 봇은 최근 체결가로 체결시켜서 슬리피지가 항상 0 이었다)

가장 중요한 원칙: 양쪽은 '같이' 체결되거나 '둘 다' 안 된다
------------------------------------------------------------------
헷지는 두 다리가 함께 있어야 의미가 있다. 그래서
  1) 양쪽 체결을 먼저 '계산만' 해본다 (호가 충분한지, 잔고 충분한지, 너무 밀리지 않는지)
  2) 둘 다 괜찮을 때만 잔고 / 포지션 / 체결 기록을 한 번에 저장한다
하나라도 안 되면 아무것도 바꾸지 않는다. (old 봇은 한쪽이 실패해도 그냥 진행했었다)

금액 헷징
------------------------------------------------------------------
바이비트 숏 개수를 먼저 정하고, 그 숏 금액(USDT) × 달러 환율 만큼의 원화로 업비트에서 산다.
그래서 업비트 개수와 바이비트 개수가 다르다. 청산할 때는 각자 가진 개수만큼 정리한다.

잔고가 바뀌는 방식
------------------------------------------------------------------
- 업비트: 살 때 원화에서 (산 금액 + 수수료) 빠짐, 코인 늘어남
          팔 때 코인 빠짐, 원화에 (판 금액 - 수수료) 들어옴
- 바이비트: 숏 열 때 USDT 에서 (증거금 + 수수료) 빠짐. 레버리지 1배라 증거금 = 숏 금액
           숏 닫을 때 USDT 에 (증거금 + 숏 손익 - 수수료) 들어옴
           펀딩비는 정산 때마다 USDT 잔고에 바로 더하거나 뺀다
- 전송비는 실제 잔고에서 빠지지 않는다. (봇이 송금을 안 하니까) 손익 계산에만 넣는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from kimp.config import FeeSettings
from kimp.db import Fill, FundingEvent, PaperBalance, Position, PositionStatus, utcnow
from kimp.pnl import EntryLegs, ExitLegs, PnLBreakdown, bybit_fee_usdt, compute_pnl, upbit_fee_krw
from kimp.premium import FillEstimate, Quote, premium_percent

logger = logging.getLogger(__name__)

# 잔고 이름
KRW = "upbit:KRW"
USDT = "bybit:USDT"


def coin_key(coin: str) -> str:
    """업비트 코인 잔고 이름. 예) XRP → 'upbit:XRP'"""
    return f"upbit:{coin}"


class EntryRejected(Exception):
    """진입 못 함 (호가 부족, 잔고 부족, 너무 많이 밀림 등). 아무것도 바뀌지 않았다"""


@dataclass(frozen=True)
class ExitValuation:
    """
    "지금 청산하면 얼마?" 계산 결과

    Attributes:
        legs: 가정한 청산 값들
        pnl: 손익 상세
        exit_premium: 실제 체결가로 계산한 청산 김프 (%)
        upbit_fill / bybit_fill: 호가창 계산 결과 (슬리피지 기록용)
        usdt_krw: 그 순간 업비트 USDT 가격 (참고용)
        exit_tether_premium / exit_coin_premium: 청산 김프를 테더 몫과 코인 몫으로 나눈 값 (분석용)
    """

    legs: ExitLegs
    pnl: PnLBreakdown
    exit_premium: float
    upbit_fill: FillEstimate
    bybit_fill: FillEstimate
    usdt_krw: Optional[float]
    exit_tether_premium: Optional[float] = None
    exit_coin_premium: Optional[float] = None


def entry_legs_of(pos: Position) -> EntryLegs:
    """DB 에 저장된 포지션 → 손익 계산용 진입 값"""
    return EntryLegs(
        upbit_qty=pos.upbit_qty,
        bybit_qty=pos.bybit_qty,
        upbit_price_krw=pos.entry_upbit_price_krw,
        bybit_price_usdt=pos.entry_bybit_price_usdt,
        fx_krw=pos.entry_fx_krw,
        upbit_fee_krw=pos.entry_upbit_fee_krw,
        bybit_fee_usdt=pos.entry_bybit_fee_usdt,
    )


class PaperBroker:
    """페이퍼 트레이딩 체결 + 잔고 관리"""

    def __init__(self, fees: FeeSettings, max_slippage_percent: float):
        """
        Args:
            fees: 수수료 설정 (가짜 체결에도 같은 수수료를 써서 손익 계산과 맞춘다)
            max_slippage_percent: 진입할 때 맨 앞 호가보다 이 % 이상 밀리면 진입 안 함
        """
        self.fees = fees
        self.max_slippage_percent = max_slippage_percent

    # ------------------------------------------------------------------
    # 잔고
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_initial_balances(session: Session, initial_krw: float, initial_usdt: float) -> None:
        """
        처음 실행할 때만 시작 자본을 넣는다

        이미 잔고가 있으면 아무것도 안 한다. (껐다 켜도 페이퍼 잔고 유지)
        처음부터 다시 하고 싶으면 DB 파일을 지우면 된다.
        """
        if session.get(PaperBalance, KRW) is None:
            session.add(PaperBalance(asset=KRW, amount=initial_krw))
            logger.info("페이퍼 시작 원화: %s원", f"{initial_krw:,.0f}")
        if session.get(PaperBalance, USDT) is None:
            session.add(PaperBalance(asset=USDT, amount=initial_usdt))
            logger.info("페이퍼 시작 USDT: %s", f"{initial_usdt:,.2f}")
        session.commit()

    @staticmethod
    def balance(session: Session, asset: str) -> float:
        """잔고 보기 (없으면 0)"""
        row = session.get(PaperBalance, asset)
        return row.amount if row else 0.0

    @staticmethod
    def _add(session: Session, asset: str, delta: float) -> None:
        """잔고 더하기/빼기 (저장은 부르는 쪽에서 한 번에)"""
        row = session.get(PaperBalance, asset)
        if row is None:
            row = PaperBalance(asset=asset, amount=0.0)
            session.add(row)
        row.amount += delta

    # ------------------------------------------------------------------
    # 진입
    # ------------------------------------------------------------------
    def open_hedge(self, session: Session, quote: Quote, bybit_qty: float) -> Position:
        """
        진입: 바이비트 bybit_qty 개 숏 + 같은 금액(달러 환율로 환산)만큼 업비트 매수

        Args:
            session: DB 연결
            quote: 진입을 결정한 그 순간의 시세 (같은 시세로 체결시킨다)
            bybit_qty: 바이비트 숏 개수 (주문 단위에 맞춘 값)

        Returns:
            새로 만든 OPEN 포지션 (저장 완료)

        Raises:
            EntryRejected: 진입 못 하는 이유가 있을 때. 이때는 아무것도 바뀌지 않는다
        """
        # 1) 양쪽 체결을 계산만 해본다
        up, by = quote.entry_fills(bybit_qty)
        if not by or not by.is_complete:
            raise EntryRejected("바이비트 매수 호가가 모자람")
        if not up or not up.is_complete:
            raise EntryRejected("업비트 매도 호가가 모자람")
        if up.slippage_percent > self.max_slippage_percent:
            raise EntryRejected(f"업비트에서 너무 밀림 {up.slippage_percent:.3f}%")
        if by.slippage_percent > self.max_slippage_percent:
            raise EntryRejected(f"바이비트에서 너무 밀림 {by.slippage_percent:.3f}%")

        # 2) 필요한 돈 계산 + 잔고 확인
        upbit_qty = up.filled_qty  # 금액에 맞춰 체결된 업비트 개수
        up_fee_krw = upbit_fee_krw(up.avg_price, upbit_qty, self.fees)
        by_fee = bybit_fee_usdt(by.avg_price, bybit_qty, self.fees)
        margin = by.cost  # 레버리지 1배: 증거금 = 숏 금액

        krw_needed = up.cost + up_fee_krw
        usdt_needed = margin + by_fee
        krw_have = self.balance(session, KRW)
        usdt_have = self.balance(session, USDT)
        if krw_have < krw_needed:
            raise EntryRejected(f"원화 부족 {krw_have:,.0f} < {krw_needed:,.0f}")
        if usdt_have < usdt_needed:
            raise EntryRejected(f"USDT 부족 {usdt_have:,.2f} < {usdt_needed:,.2f}")

        # 3) 둘 다 괜찮으니 한 번에 저장
        entry_premium = premium_percent(up.avg_price, by.avg_price, quote.fx_krw)
        entry_tether, entry_coin = quote.split_premium(entry_premium)  # 분석용: 테더 몫 / 코인 몫
        pos = Position(
            coin=quote.coin,
            status=PositionStatus.OPEN,
            is_paper=True,
            opened_at=utcnow(),
            upbit_qty=upbit_qty,
            bybit_qty=bybit_qty,
            entry_upbit_price_krw=up.avg_price,
            entry_bybit_price_usdt=by.avg_price,
            entry_fx_krw=quote.fx_krw,
            entry_usdt_krw=quote.usdt_krw,
            entry_upbit_fee_krw=up_fee_krw,
            entry_bybit_fee_usdt=by_fee,
            entry_premium=entry_premium,
            entry_tether_premium=entry_tether,
            entry_coin_premium=entry_coin,
            bybit_margin_usdt=margin,
            funding_usdt=0.0,
        )
        session.add(pos)
        session.flush()  # pos.id 를 받아오기 위해

        self._add(session, KRW, -krw_needed)
        self._add(session, coin_key(quote.coin), upbit_qty)
        self._add(session, USDT, -usdt_needed)

        session.add(Fill(position_id=pos.id, exchange="upbit", side="buy", qty=upbit_qty, avg_price=up.avg_price,
                         fee=up_fee_krw, fee_currency="KRW", slippage_percent=up.slippage_percent))
        session.add(Fill(position_id=pos.id, exchange="bybit", side="open_short", qty=bybit_qty,
                         avg_price=by.avg_price, fee=by_fee, fee_currency="USDT",
                         slippage_percent=by.slippage_percent))
        session.commit()
        return pos

    # ------------------------------------------------------------------
    # 평가 / 청산
    # ------------------------------------------------------------------
    def value_exit(self, pos: Position, quote: Quote) -> Optional[ExitValuation]:
        """
        "지금 청산하면 얼마?" 계산 (잔고는 안 바꿈)

        청산 방향 호가(업비트 매수 호가, 바이비트 매도 호가)를 가진 개수만큼 긁어서 계산한다.
        그래서 실제로 받을 수 있는 가격으로 판단한다.

        Returns:
            ExitValuation. 호가가 모자라서 다 못 팔면 None
        """
        up, by = quote.exit_fills(pos.upbit_qty, pos.bybit_qty)
        if not up or not by or not up.is_complete or not by.is_complete:
            return None
        legs = ExitLegs(
            upbit_price_krw=up.avg_price,
            bybit_price_usdt=by.avg_price,
            fx_krw=quote.fx_krw,
            upbit_fee_krw=upbit_fee_krw(up.avg_price, pos.upbit_qty, self.fees),
            bybit_fee_usdt=bybit_fee_usdt(by.avg_price, pos.bybit_qty, self.fees),
        )
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees)
        exit_premium = premium_percent(up.avg_price, by.avg_price, quote.fx_krw)
        exit_tether, exit_coin = quote.split_premium(exit_premium)
        return ExitValuation(
            legs=legs,
            pnl=pnl,
            exit_premium=exit_premium,
            upbit_fill=up,
            bybit_fill=by,
            usdt_krw=quote.usdt_krw,
            exit_tether_premium=exit_tether,
            exit_coin_premium=exit_coin,
        )

    def close_hedge(self, session: Session, pos: Position, valuation: ExitValuation, reason: str) -> PnLBreakdown:
        """
        청산: 업비트 매도 + 바이비트 숏 닫기 (방금 계산한 값 그대로 체결)

        Args:
            session: DB 연결
            pos: OPEN 포지션
            valuation: 바로 전에 value_exit 로 계산한 값
            reason: 청산 이유 (ExitReason)

        Returns:
            확정 손익 (저장 완료)
        """
        legs = valuation.legs

        # 업비트: 가진 개수 전부 판다 → 코인 빠지고 원화 들어옴 (수수료 빼고)
        self._add(session, coin_key(pos.coin), -pos.upbit_qty)
        self._add(session, KRW, legs.upbit_price_krw * pos.upbit_qty - legs.upbit_fee_krw)

        # 바이비트: 숏 전부 닫는다 → 증거금 돌려받고 + 숏 손익 - 수수료
        short_pnl_usdt = (pos.entry_bybit_price_usdt - legs.bybit_price_usdt) * pos.bybit_qty
        self._add(session, USDT, pos.bybit_margin_usdt + short_pnl_usdt - legs.bybit_fee_usdt)

        session.add(Fill(position_id=pos.id, exchange="upbit", side="sell", qty=pos.upbit_qty,
                         avg_price=legs.upbit_price_krw, fee=legs.upbit_fee_krw, fee_currency="KRW",
                         slippage_percent=valuation.upbit_fill.slippage_percent))
        session.add(Fill(position_id=pos.id, exchange="bybit", side="close_short", qty=pos.bybit_qty,
                         avg_price=legs.bybit_price_usdt, fee=legs.bybit_fee_usdt, fee_currency="USDT",
                         slippage_percent=valuation.bybit_fill.slippage_percent))

        pnl = valuation.pnl
        pos.status = PositionStatus.CLOSED
        pos.closed_at = utcnow()
        pos.exit_upbit_price_krw = legs.upbit_price_krw
        pos.exit_bybit_price_usdt = legs.bybit_price_usdt
        pos.exit_fx_krw = legs.fx_krw
        pos.exit_usdt_krw = valuation.usdt_krw
        pos.exit_upbit_fee_krw = legs.upbit_fee_krw
        pos.exit_bybit_fee_usdt = legs.bybit_fee_usdt
        pos.exit_premium = valuation.exit_premium
        pos.exit_tether_premium = valuation.exit_tether_premium
        pos.exit_coin_premium = valuation.exit_coin_premium
        pos.exit_reason = reason
        pos.upbit_leg_krw = pnl.upbit_leg_krw
        pos.bybit_leg_usdt = pnl.bybit_leg_usdt
        pos.bybit_leg_krw = pnl.bybit_leg_krw
        pos.funding_krw = pnl.funding_krw
        pos.gross_pnl_krw = pnl.gross_krw
        pos.total_cost_krw = pnl.total_cost_krw
        pos.net_pnl_krw = pnl.net_krw
        pos.net_pnl_percent = pnl.net_percent
        session.commit()
        return pnl

    # ------------------------------------------------------------------
    # 펀딩비
    # ------------------------------------------------------------------
    def apply_funding(self, session: Session, pos: Position, rate: float, mark_price: float,
                      funding_time_ms: int) -> float:
        """
        펀딩비 한 번 반영 (우리는 숏)

        펀딩비 = 바이비트 숏 개수 × 기준 가격 × 비율
        - 비율이 + 면: 롱이 숏에게 준다 → 우리는 받는다
        - 비율이 - 면: 숏이 롱에게 준다 → 우리는 낸다
        예) 740.7개 × 1.35 USDT × 0.01% = +0.1 USDT 받음

        Returns:
            반영한 펀딩비 (USDT, + 받음)
        """
        amount = pos.bybit_qty * mark_price * rate
        pos.funding_usdt += amount
        self._add(session, USDT, amount)
        session.add(FundingEvent(position_id=pos.id, rate=rate, mark_price=mark_price,
                                 amount_usdt=amount, funding_time_ms=funding_time_ms))
        session.commit()
        return amount

    @staticmethod
    def open_positions(session: Session) -> list[Position]:
        """열려 있는 포지션 전부 (봇 재시작 때 이어서 관리하려고)"""
        return list(session.scalars(select(Position).where(Position.status == PositionStatus.OPEN)))
