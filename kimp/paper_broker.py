"""
페이퍼 트레이딩 (가짜 주문, 진짜 호가)

실제 주문은 안 내고, 그 순간의 '진짜 호가창'으로 체결가를 계산해서 잔고만 바꾼다.
그래서 호가가 얇은 코인은 페이퍼에서도 손해(슬리피지)가 그대로 보인다.

가장 중요한 원칙: 전부 '같이' 체결되거나 '전부' 안 된다
------------------------------------------------------------------
진입은 업비트 USDT 매도 + 업비트 코인 매수 + 바이비트 숏, 세 가지가 한 세트다.
  1) 세 가지 체결을 먼저 '계산만' 해본다 (호가 충분한지, 잔고 충분한지, 너무 밀리지 않는지)
  2) 전부 괜찮을 때만 잔고 / 포지션 / 체결 기록을 한 번에 저장한다
하나라도 안 되면 아무것도 바꾸지 않는다.

잔고가 바뀌는 방식 (USDT 기준)
------------------------------------------------------------------
- 업비트 진입: USDT 를 팔고(수수료 빼고 원화) → 그 원화로 코인을 산다(수수료는 원화에서 따로)
  업비트 청산: 코인을 팔고(수수료 빼고 원화) → 그 원화로 USDT 를 산다(수수료 따로)
  자투리 원화(1원 미만 등)는 upbit:KRW 에 남는다
- 바이비트 진입: 남은 USDT 에서 (증거금 + 수수료) 빠짐. 증거금 = 숏 금액 ÷ 레버리지
  바이비트 청산: 증거금 + 숏 손익 - 수수료 가 돌아옴
  강제청산: 증거금을 잃는다 (돌아오는 것 없음)
  펀딩비는 정산 때마다 남은 USDT 에 바로 더하거나 뺀다
- 송금(리밸런싱)은 필요할 때만: transfer() 가 한쪽 USDT 를 다른 쪽으로 옮기고 수수료(기본 1 USDT)를 한 번 뗀다.
  (페이퍼 잔고는 사용자가 실제로 못 보내니, 엔진이 '사용자가 보냈다' 고 치고 부른다)
- 사이클당 전송비(TRANSFER_FEE_USDT_PER_CYCLE, 기본 0)는 잔고에서 빠지지 않고 손익 계산에만 넣는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from kimp.config import FeeSettings
from kimp.db import Fill, FundingEvent, PaperBalance, Position, PositionStatus, utcnow
from kimp.pnl import EntryLegs, ExitLegs, PnLBreakdown, bybit_fee_usdt, compute_pnl, liquidation_price
from kimp.premium import ExitPlan, Quote

logger = logging.getLogger(__name__)

# 잔고 이름
UPBIT_USDT = "upbit:USDT"
UPBIT_KRW = "upbit:KRW"  # 매매 중 생기는 자투리 원화
BYBIT_USDT = "bybit:USDT"  # 바이비트에 남은 USDT (증거금으로 묶인 돈은 제외)


def coin_key(coin: str) -> str:
    """업비트 코인 잔고 이름. 예) XRP → 'upbit:XRP'"""
    return f"upbit:{coin}"


class EntryRejected(Exception):
    """진입 못 함 (호가 부족, 잔고 부족, 너무 많이 밀림 등). 아무것도 바뀌지 않았다"""


class InsufficientBalance(EntryRejected):
    """
    한쪽 USDT 가 모자라서 진입 못 함 → 리밸런싱(송금)하면 들어갈 수도 있다

    Attributes:
        side: 모자란 쪽 ('upbit' / 'bybit')
        need_usdt: 그쪽에 필요한 USDT (지금 있는 것 포함한 전체)
    """

    def __init__(self, message: str, side: str, need_usdt: float):
        super().__init__(message)
        self.side = side
        self.need_usdt = need_usdt


@dataclass(frozen=True)
class ExitValuation:
    """
    "지금 청산하면 얼마?" 계산 결과

    Attributes:
        plan: 체결 계산 (업비트 코인 매도, USDT 매수, 바이비트 숏 닫기)
        legs: 손익 계산용 청산 값
        pnl: 손익 상세
        tether_premium / site_premium / fx_krw: 참고용 (그 순간 테더 프리미엄, 사이트식 김프, 달러 환율)
    """

    plan: ExitPlan
    legs: ExitLegs
    pnl: PnLBreakdown
    tether_premium: Optional[float] = None
    site_premium: Optional[float] = None
    fx_krw: Optional[float] = None

    @property
    def exit_premium(self) -> float:
        """실제 체결가로 계산한 청산 김프 (코인 몫, %)"""
        return self.plan.premium


def entry_legs_of(pos: Position) -> EntryLegs:
    """DB 에 저장된 포지션 → 손익 계산용 진입 값"""
    return EntryLegs(
        usdt_sold=pos.usdt_sold, usdt_price_krw=pos.entry_usdt_price_krw, usdt_fee_krw=pos.entry_usdt_fee_krw,
        upbit_qty=pos.upbit_qty, upbit_price_krw=pos.entry_upbit_price_krw, upbit_fee_krw=pos.entry_upbit_fee_krw,
        bybit_qty=pos.bybit_qty, bybit_price_usdt=pos.entry_bybit_price_usdt,
        bybit_fee_usdt=pos.entry_bybit_fee_usdt,
    )


class PaperBroker:
    """페이퍼 트레이딩 체결 + 잔고 관리"""

    def __init__(self, fees: FeeSettings, max_slippage_percent: float, leverage: float = 1.0,
                 mmr: float = 0.005, now_fn: Callable[[], datetime] = utcnow):
        """
        Args:
            fees: 수수료 설정 (가짜 체결에도 같은 수수료를 써서 손익 계산과 맞춘다)
            max_slippage_percent: 진입할 때 맨 앞 호가보다 이 % 이상 밀리면 진입 안 함
            leverage: 바이비트 레버리지 (증거금 = 숏 금액 ÷ 레버리지)
            mmr: 바이비트 유지증거금률 (강제청산 가격 계산용)
            now_fn: 진입/청산 시각을 찍을 시계 (시뮬레이션에서는 과거 시각)
        """
        self.fees = fees
        self.max_slippage_percent = max_slippage_percent
        self.leverage = leverage
        self.mmr = mmr
        self._now = now_fn

    # ------------------------------------------------------------------
    # 잔고
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_initial_balances(session: Session, total_usdt: float, upbit_share: float) -> bool:
        """
        처음 실행할 때만 시작 자본을 목표 비율대로 나눠 넣는다 (페이퍼 전용)

        예) 10,000 USDT, 업비트 목표 43.5% → 업비트 4,348 USDT / 바이비트 5,652 USDT
        이미 잔고가 있으면 아무것도 안 한다. (처음부터 다시 하려면 DB 파일을 지우면 된다)

        Returns:
            새로 넣었으면 True
        """
        if session.get(PaperBalance, UPBIT_USDT) is not None or session.get(PaperBalance, BYBIT_USDT) is not None:
            return False
        upbit = total_usdt * upbit_share
        session.add(PaperBalance(asset=UPBIT_USDT, amount=upbit))
        session.add(PaperBalance(asset=BYBIT_USDT, amount=total_usdt - upbit))
        session.commit()
        logger.info("페이퍼 시작 자본 %s USDT → 업비트 %s / 바이비트 %s (목표 비율 %.1f : %.1f)",
                    f"{total_usdt:,.2f}", f"{upbit:,.2f}", f"{total_usdt - upbit:,.2f}",
                    upbit_share * 100, (1 - upbit_share) * 100)
        return True

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

    def transfer(self, session: Session, amount_usdt: float, fee_usdt: float) -> float:
        """
        페이퍼 송금: amount 가 + 면 업비트 → 바이비트, − 면 바이비트 → 업비트

        보내는 쪽 '남는 USDT' (코인·증거금으로 묶이지 않은 것) 까지만 보낼 수 있다.
        받는 쪽에는 출금 수수료를 뺀 만큼 들어간다.
        예) 업비트 → 바이비트 30 USDT, 수수료 1 → 업비트 −30, 바이비트 +29

        Returns:
            실제로 보낸 양 (부호 포함). 보낼 게 수수료보다 적으면 0 (안 보냄)
        """
        src, dst = (UPBIT_USDT, BYBIT_USDT) if amount_usdt > 0 else (BYBIT_USDT, UPBIT_USDT)
        send = min(abs(amount_usdt), self.balance(session, src))
        if send <= fee_usdt:
            return 0.0
        self._add(session, src, -send)
        self._add(session, dst, send - fee_usdt)
        session.commit()
        return send if amount_usdt > 0 else -send

    def capital_split(self, session: Session, positions: Iterable[Position],
                      quotes: Dict[str, Quote]) -> Tuple[float, float]:
        """
        지금 양쪽 거래소에 있는 자본 (USDT 로 환산)

        업비트 = 남은 USDT + 자투리 원화 + 들고 있는 코인을 지금 팔아 USDT 로 되사면 받을 개수
        바이비트 = 남은 USDT + 포지션 증거금 + 숏 평가 손익
        시세가 없는 코인은 진입 때 판 USDT 개수로 대신 친다.

        Returns:
            (업비트 쪽, 바이비트 쪽)
        """
        any_quote = next(iter(quotes.values()), None)
        usdt_mid = any_quote.usdt_krw if any_quote else None
        upbit = self.balance(session, UPBIT_USDT)
        if usdt_mid:
            upbit += self.balance(session, UPBIT_KRW) / usdt_mid
        bybit = self.balance(session, BYBIT_USDT)
        for pos in positions:
            quote = quotes.get(pos.coin)
            plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_fee_rate) if quote else None
            upbit += plan.usdt_bought if plan else pos.usdt_sold
            short_pnl = (pos.entry_bybit_price_usdt - plan.bybit.avg_price) * pos.bybit_qty if plan else 0.0
            bybit += pos.bybit_margin_usdt + short_pnl
        return upbit, bybit

    # ------------------------------------------------------------------
    # 진입
    # ------------------------------------------------------------------
    def open_hedge(self, session: Session, quote: Quote, bybit_qty: float) -> Position:
        """
        진입: 바이비트 bybit_qty 개 숏 + 업비트에서 같은 금액의 USDT 를 팔아 코인 매수

        Args:
            session: DB 연결
            quote: 진입을 결정한 그 순간의 시세 (같은 시세로 체결시킨다)
            bybit_qty: 바이비트 숏 개수 (주문 단위에 맞춘 값)

        Raises:
            EntryRejected: 진입 못 하는 이유가 있을 때. 이때는 아무것도 바뀌지 않는다
        """
        fee_rate = self.fees.upbit_trade_fee_rate
        plan = quote.entry_plan(bybit_qty, fee_rate)
        if plan is None or not plan.bybit.is_complete:
            raise EntryRejected("바이비트 매수 호가가 모자람")
        if not plan.usdt.is_complete:
            raise EntryRejected("업비트 USDT 매수 호가가 모자람")
        if not plan.upbit.is_complete:
            raise EntryRejected("업비트 코인 매도 호가가 모자람")
        for name, fill in (("업비트 코인", plan.upbit), ("업비트 USDT", plan.usdt), ("바이비트", plan.bybit)):
            if fill.slippage_percent > self.max_slippage_percent:
                raise EntryRejected(f"{name}에서 너무 밀림 {fill.slippage_percent:.3f}%")

        # 필요한 돈 확인
        by_fee = bybit_fee_usdt(plan.bybit.avg_price, bybit_qty, self.fees)
        margin = plan.bybit.cost / self.leverage
        upbit_have = self.balance(session, UPBIT_USDT)
        bybit_have = self.balance(session, BYBIT_USDT)
        if upbit_have < plan.usdt_sold:
            raise InsufficientBalance(f"업비트 USDT 부족 {upbit_have:,.2f} < {plan.usdt_sold:,.2f} "
                                      f"(업비트로 {plan.usdt_sold - upbit_have:,.2f} USDT 더 필요)",
                                      side="upbit", need_usdt=plan.usdt_sold)
        if bybit_have < margin + by_fee:
            raise InsufficientBalance(f"바이비트 USDT 부족 {bybit_have:,.2f} < {margin + by_fee:,.2f} "
                                      f"(바이비트로 {margin + by_fee - bybit_have:,.2f} USDT 더 필요)",
                                      side="bybit", need_usdt=margin + by_fee)

        # 전부 괜찮으니 한 번에 저장
        upbit_qty = plan.upbit.filled_qty
        pos = Position(
            coin=quote.coin, status=PositionStatus.OPEN, is_paper=True, opened_at=self._now(),
            usdt_sold=plan.usdt_sold, entry_usdt_price_krw=plan.usdt.avg_price, entry_usdt_fee_krw=plan.usdt_fee_krw,
            upbit_qty=upbit_qty, entry_upbit_price_krw=plan.upbit.avg_price, entry_upbit_fee_krw=plan.coin_fee_krw,
            bybit_qty=bybit_qty, entry_bybit_price_usdt=plan.bybit.avg_price, entry_bybit_fee_usdt=by_fee,
            entry_premium=plan.premium, entry_tether_premium=quote.tether_premium,
            entry_site_premium=quote.site_premium, entry_fx_krw=quote.fx_krw,
            leverage=self.leverage, bybit_margin_usdt=margin,
            liquidation_price=liquidation_price(plan.bybit.avg_price, self.leverage, self.mmr),
            funding_usdt=0.0,
        )
        session.add(pos)
        session.flush()  # pos.id 받기

        # 업비트: USDT 빠짐 → 원화 (수수료 뺀 만큼) → 코인 사는 데 씀 (코인값 + 수수료)
        krw_change = (plan.usdt.cost - plan.usdt_fee_krw) - (plan.upbit.cost + plan.coin_fee_krw)
        self._add(session, UPBIT_USDT, -plan.usdt_sold)
        self._add(session, UPBIT_KRW, krw_change)  # 거의 0 (자투리)
        self._add(session, coin_key(quote.coin), upbit_qty)
        # 바이비트: 증거금 + 수수료 빠짐
        self._add(session, BYBIT_USDT, -(margin + by_fee))

        for exchange, side, qty, price, fee, cur, slip in (
            ("upbit", "sell_usdt", plan.usdt_sold, plan.usdt.avg_price, plan.usdt_fee_krw, "KRW",
             plan.usdt.slippage_percent),
            ("upbit", "buy", upbit_qty, plan.upbit.avg_price, plan.coin_fee_krw, "KRW", plan.upbit.slippage_percent),
            ("bybit", "open_short", bybit_qty, plan.bybit.avg_price, by_fee, "USDT", plan.bybit.slippage_percent),
        ):
            session.add(Fill(position_id=pos.id, exchange=exchange, side=side, qty=qty, avg_price=price,
                             fee=fee, fee_currency=cur, slippage_percent=slip))
        session.commit()
        return pos

    # ------------------------------------------------------------------
    # 평가 / 청산
    # ------------------------------------------------------------------
    @staticmethod
    def _exit_legs(plan: ExitPlan, bybit_price: float, bybit_fee: float) -> ExitLegs:
        """체결 계산 → 손익 계산용 청산 값"""
        return ExitLegs(
            upbit_price_krw=plan.upbit.avg_price, upbit_fee_krw=plan.coin_fee_krw,
            usdt_price_krw=plan.usdt.avg_price, usdt_fee_krw=plan.usdt_fee_krw, usdt_bought=plan.usdt_bought,
            bybit_price_usdt=bybit_price, bybit_fee_usdt=bybit_fee,
        )

    def value_exit(self, pos: Position, quote: Quote) -> Optional[ExitValuation]:
        """
        "지금 청산하면 얼마?" 계산 (잔고는 안 바꿈)

        Returns:
            ExitValuation. 호가가 모자라서 다 못 팔면 None
        """
        plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_fee_rate)
        if plan is None or not plan.is_complete:
            return None
        by_fee = bybit_fee_usdt(plan.bybit.avg_price, pos.bybit_qty, self.fees)
        legs = self._exit_legs(plan, plan.bybit.avg_price, by_fee)
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees)
        return ExitValuation(plan=plan, legs=legs, pnl=pnl, tether_premium=quote.tether_premium,
                             site_premium=quote.site_premium, fx_krw=quote.fx_krw)

    def value_liquidation(self, pos: Position, quote: Quote) -> Optional[ExitValuation]:
        """
        바이비트 숏이 강제청산됐을 때의 결과 계산

        바이비트: 증거금을 전부 잃는다 (숏 손익 = -증거금)
        업비트: 남은 코인을 지금 팔아 USDT 로 되산다 (헷지가 깨졌으니 바로 정리)
        """
        plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_fee_rate)
        if plan is None or not plan.upbit.is_complete or not plan.usdt.is_complete:
            return None
        legs = self._exit_legs(plan, pos.liquidation_price, 0.0)
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees,
                          bybit_leg_override_usdt=-pos.bybit_margin_usdt)
        return ExitValuation(plan=plan, legs=legs, pnl=pnl, tether_premium=quote.tether_premium,
                             site_premium=quote.site_premium, fx_krw=quote.fx_krw)

    def close_hedge(self, session: Session, pos: Position, valuation: ExitValuation, reason: str,
                    liquidated: bool = False) -> PnLBreakdown:
        """
        청산: 업비트 코인 매도 → USDT 매수, 바이비트 숏 닫기 (방금 계산한 값 그대로 체결)

        Args:
            liquidated: True 면 바이비트는 강제청산 (증거금을 잃고 돌려받는 것 없음)
        Returns:
            확정 손익 (저장 완료)
        """
        plan, legs, pnl = valuation.plan, valuation.legs, valuation.pnl

        # 업비트: 코인 빠짐 → 원화 (수수료 빼고) → USDT 사는 데 씀 (USDT 값 + 수수료) → USDT 늘어남
        krw_change = (plan.upbit.cost - plan.coin_fee_krw) - (plan.usdt.cost + plan.usdt_fee_krw)
        self._add(session, coin_key(pos.coin), -pos.upbit_qty)
        self._add(session, UPBIT_KRW, krw_change)
        self._add(session, UPBIT_USDT, plan.usdt_bought)

        # 바이비트
        if liquidated:
            back = 0.0  # 증거금을 잃음
            by_side, by_price, by_fee, by_slip = "liquidated", pos.liquidation_price, 0.0, 0.0
        else:
            short_pnl = (pos.entry_bybit_price_usdt - legs.bybit_price_usdt) * pos.bybit_qty
            back = pos.bybit_margin_usdt + short_pnl - legs.bybit_fee_usdt
            by_side, by_price, by_fee, by_slip = ("close_short", legs.bybit_price_usdt, legs.bybit_fee_usdt,
                                                  plan.bybit.slippage_percent)
        self._add(session, BYBIT_USDT, back)

        for exchange, side, qty, price, fee, cur, slip in (
            ("upbit", "sell", pos.upbit_qty, plan.upbit.avg_price, plan.coin_fee_krw, "KRW", plan.upbit.slippage_percent),
            ("upbit", "buy_usdt", plan.usdt_bought, plan.usdt.avg_price, plan.usdt_fee_krw, "KRW",
             plan.usdt.slippage_percent),
            ("bybit", by_side, pos.bybit_qty, by_price, by_fee, "USDT", by_slip),
        ):
            session.add(Fill(position_id=pos.id, exchange=exchange, side=side, qty=qty, avg_price=price,
                             fee=fee, fee_currency=cur, slippage_percent=slip))

        pos.status = PositionStatus.CLOSED
        pos.closed_at = self._now()
        pos.exit_upbit_price_krw = plan.upbit.avg_price
        pos.exit_upbit_fee_krw = plan.coin_fee_krw
        pos.exit_usdt_price_krw = plan.usdt.avg_price
        pos.exit_usdt_fee_krw = plan.usdt_fee_krw
        pos.usdt_bought = plan.usdt_bought
        pos.exit_bybit_price_usdt = by_price
        pos.exit_bybit_fee_usdt = by_fee
        pos.exit_premium = valuation.exit_premium
        pos.exit_tether_premium = valuation.tether_premium
        pos.exit_site_premium = valuation.site_premium
        pos.exit_fx_krw = valuation.fx_krw
        pos.exit_reason = reason
        pos.upbit_leg_usdt = pnl.upbit_leg_usdt
        pos.bybit_leg_usdt = pnl.bybit_leg_usdt
        pos.gross_pnl_usdt = pnl.gross_usdt
        pos.total_cost_usdt = pnl.total_cost_usdt
        pos.net_pnl_usdt = pnl.net_usdt
        pos.net_pnl_percent = pnl.net_percent
        session.commit()
        return pnl

    # ------------------------------------------------------------------
    # 펀딩비 / 조회
    # ------------------------------------------------------------------
    def apply_funding(self, session: Session, pos: Position, rate: float, mark_price: float,
                      funding_time_ms: int) -> float:
        """
        펀딩비 한 번 반영 (우리는 숏)

        펀딩비 = 바이비트 숏 개수 × 기준 가격 × 비율
        - 비율이 + 면 우리는 받는다, - 면 낸다
        예) 740.7개 × 1.35 USDT × 0.01% = +0.1 USDT 받음

        Returns:
            반영한 펀딩비 (USDT, + 받음)
        """
        amount = pos.bybit_qty * mark_price * rate
        pos.funding_usdt += amount
        self._add(session, BYBIT_USDT, amount)
        session.add(FundingEvent(position_id=pos.id, rate=rate, mark_price=mark_price,
                                 amount_usdt=amount, funding_time_ms=funding_time_ms))
        session.commit()
        return amount

    @staticmethod
    def open_positions(session: Session) -> list[Position]:
        """열려 있는 포지션 전부 (봇 재시작 때 이어서 관리하려고)"""
        return list(session.scalars(select(Position).where(Position.status == PositionStatus.OPEN)))
