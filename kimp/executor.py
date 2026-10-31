"""
실행기: 주문 '한 동작' 씩만 담당 (페이퍼 / 드라이런 / 실거래가 여기만 다르다)

원칙
------------------------------------------------------------------
진입·청산 순서, 되돌리기, 손익 계산, 송금 흐름, 대조, 안전장치는 전부 kimp/broker.py 의 HedgeBroker 한 곳에 있고
페이퍼든 실거래든 똑같이 돈다. 실행기는 "USDT 팔아줘", "코인 사줘", "숏 열어줘", "잔고 알려줘", "출금해줘" 만 한다.

    HedgeBroker (공통 로직)
        └ executor.open_short / sell_usdt / buy_coin_with_krw / sell_coin / buy_usdt_with_krw / close_short
          executor.free / coin_total / short_qty / withdraw / transfer_arrived ...
              ├ PaperExecutor   : 그 순간 호가창으로 체결가 계산, 잔고는 DB(paper_balances)
              ├ DryRunExecutor  : 실제 잔고로 시작 + 체결은 페이퍼와 같게 계산, "이 주문을 냈을 것" 로그 (live_executor.py)
              └ LiveExecutor    : 실제 주문, 거래소가 돌려준 체결값·잔고 (live_executor.py)

한 동작의 결과는 LegFill 하나. 실패하면 OrderFailed 를 던진다 (부분 체결도 결과로 돌려준다).
"""
from __future__ import annotations

import itertools
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Set

from sqlalchemy.orm import Session

from kimp.config import FeeSettings
from kimp.db import PaperBalance
from kimp.premium import FillEstimate, Quote, estimate_fill, estimate_fill_by_cost

logger = logging.getLogger(__name__)

# 잔고 이름 (페이퍼 장부 / 브로커가 실행기에 물어볼 때 공통으로 쓰는 이름)
UPBIT_USDT = "upbit:USDT"
UPBIT_KRW = "upbit:KRW"  # 매매 중 생기는 자투리 원화
BYBIT_USDT = "bybit:USDT"  # 바이비트에 남은 USDT (증거금으로 묶인 돈은 제외)


def coin_key(coin: str) -> str:
    """업비트 코인 잔고 이름. 예) XRP → 'upbit:XRP'"""
    return f"upbit:{coin}"


def short_key(coin: str) -> str:
    """(페이퍼 장부) 바이비트 숏 개수. 예) XRP → 'bybit:SHORT:XRP'"""
    return f"bybit:SHORT:{coin}"


# 업비트 원화 마켓 최소 주문 금액 기본값 (원). 실제 값은 설정 UPBIT_MIN_ORDER_KRW / 실거래는 거래소 조회
DEFAULT_UPBIT_MIN_ORDER_KRW = 5_000.0


def new_client_id() -> str:
    """주문 추적번호 (거래소에 같이 보내고 DB 에 먼저 저장). 예) kimp-1a2b3c4d5e6f"""
    return "kimp-" + uuid.uuid4().hex[:12]


class OrderFailed(Exception):
    """주문 한 동작이 실패함 = 주문이 안 들어간 게 확실함 (거절, 호가 부족, 조회해도 없음)"""


class OrderUnknown(OrderFailed):
    """주문을 보냈는데 들어갔는지 모름 (응답 끊김 + 조회도 실패). 추측해서 진행하지 않는다"""


@dataclass(frozen=True)
class LegFill:
    """
    주문 한 동작의 결과

    Attributes:
        qty: 체결 개수 (USDT 매매면 USDT 개수)
        avg_price: 평균 체결가 (업비트는 원, 바이비트는 USDT)
        cost: 체결 금액 = 개수 × 평균가 (업비트 원 / 바이비트 USDT)
        fee: 수수료 (업비트 원 / 바이비트 USDT)
        order_id: 거래소 주문 번호 (페이퍼는 paper-1, paper-2 ...)
        slippage_percent: 맨 앞 호가보다 얼마나 밀렸나
    """

    qty: float
    avg_price: float
    cost: float
    fee: float
    order_id: str = ""
    slippage_percent: float = 0.0


class PaperExecutor:
    """
    페이퍼 실행기: 실제 주문 없이, 그 순간 호가창으로 체결가를 계산하고 DB 장부(paper_balances)를 바꾼다

    잔고가 바뀌는 방식 (USDT 기준)
      - 업비트 USDT 매도: USDT −개수, 원화 +(금액 − 수수료)
      - 업비트 코인 매수(원화 금액 지정): 원화 −(금액 + 수수료), 코인 +개수
      - 업비트 코인 매도: 코인 −개수, 원화 +(금액 − 수수료) / USDT 매수: 원화 −(금액 + 수수료), USDT +개수
      - 바이비트 숏 열기: USDT −(증거금 + 수수료), 증거금 = 숏 금액 ÷ 레버리지
        숏 닫기: USDT +(증거금 + 숏 손익 − 수수료) / 강제청산: 돌아오는 것 없음 (증거금 잃음)
      - 펀딩비: 정산 때 USDT 에 바로 ±
      - 송금: 보내는 쪽 −개수 → (페이퍼는 바로) 받는 쪽 +(개수 − 수수료)
    """

    is_paper = True
    name = "paper"

    def __init__(self, fees: FeeSettings, leverage: float = 1.0,
                 upbit_min_order_krw: float = DEFAULT_UPBIT_MIN_ORDER_KRW):
        self.fees = fees
        self.leverage = leverage
        self.upbit_min_order_krw = upbit_min_order_krw
        self._ids = itertools.count(1)
        # 테스트용: 여기에 동작 이름을 넣으면 그 동작이 한 번 실패한다. 예) {"buy_coin_with_krw"}
        self.fail_next: Set[str] = set()
        # 테스트용: 동작 이름 → 체결 비율. 그 동작이 한 번 일부만 체결된다. 예) {"buy_coin_with_krw": 0.8}
        self.partial_next: Dict[str, float] = {}
        self._by_cid: Dict[str, LegFill] = {}

    # ------------------------------------------------------------------
    # 장부
    # ------------------------------------------------------------------
    @staticmethod
    def _get(session: Session, asset: str) -> float:
        """장부에서 잔고 읽기 (없는 자산은 0)"""
        row = session.get(PaperBalance, asset)
        return row.amount if row else 0.0

    @staticmethod
    def _add(session: Session, asset: str, delta: float) -> None:
        """
        장부 잔고 더하기/빼기 (없으면 0 에서 시작해서 만든다)

        commit 은 하지 않는다 — 브로커가 한 동작을 끝낼 때 체결 기록과 **같이** 커밋한다.
        그래야 '잔고는 바뀌었는데 체결 기록이 없는' 어긋난 상태가 안 생긴다.
        """
        row = session.get(PaperBalance, asset)
        if row is None:
            row = PaperBalance(asset=asset, amount=0.0)
            session.add(row)
        row.amount += delta

    def ensure_initial(self, session: Session, total_usdt: float, upbit_share: float) -> bool:
        """
        처음 실행할 때만 시작 자본을 목표 비율대로 나눠 넣는다

        예) 10,000 USDT, 업비트 목표 43.5% → 업비트 4,348 USDT / 바이비트 5,652 USDT
        이미 잔고가 있으면 아무것도 안 한다. (처음부터 다시 하려면 DB 파일을 지우면 된다)
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

    def refresh(self) -> None:
        """(실거래만) 잔고 캐시 비우기. 페이퍼는 장부를 바로 읽으니 할 일 없음"""

    def prefetch(self, coins) -> None:
        """(실거래만) 잔고·숏 미리 받아 두기. 페이퍼는 할 일 없음"""

    def account_age(self) -> float:
        """(실거래만) 계좌 스냅샷 나이. 페이퍼는 장부가 늘 최신이라 0"""
        return 0.0

    def min_order_krw(self, coin: str) -> float:
        """업비트 원화 마켓 최소 주문 금액 (coin='USDT' 면 USDT/KRW). 페이퍼는 설정값"""
        return self.upbit_min_order_krw

    def free(self, session: Session, asset: str) -> float:
        """쓸 수 있는 잔고 (UPBIT_USDT / UPBIT_KRW / BYBIT_USDT / coin_key(...))"""
        return self._get(session, asset)

    def coin_total(self, session: Session, coin: str) -> float:
        """업비트에 있는 코인 개수 (대조용)"""
        return self._get(session, coin_key(coin))

    def short_qty(self, session: Session, coin: str) -> float:
        """바이비트 숏 개수 (대조용)"""
        return self._get(session, short_key(coin))

    # ------------------------------------------------------------------
    # 주문 한 동작씩
    # ------------------------------------------------------------------
    def _check_fail(self, leg: str) -> float:
        """
        테스트용 실패·일부 체결 주입. 돌려주는 값 = 이번에 체결될 비율 (보통 1.0)

        한 번 쓰면 사라진다 (discard / pop) — '한 번만 실패하고 다음엔 성공' 을 만들어서
        브로커의 되돌리기·이어서 하기를 시험한다. 실거래에는 이 장치가 없다.
        """
        if leg in self.fail_next:
            self.fail_next.discard(leg)
            raise OrderFailed(f"(테스트) {leg} 실패")
        return self.partial_next.pop(leg, 1.0)

    def _leg(self, est: Optional[FillEstimate], what: str) -> LegFill:
        """
        호가창 체결 계산 결과 → LegFill (수수료는 부르는 쪽에서 채운다)

        물량이 모자라 일부만 채워지면(is_complete=False) 실패로 본다. 반만 사고 끝내면
        한쪽만 열린 포지션이 되기 때문이다 — 그 코인은 이번 스캔에서 건너뛴다.
        """
        if est is None or not est.is_complete:
            raise OrderFailed(f"{what}: 호가가 모자람")
        return LegFill(qty=est.filled_qty, avg_price=est.avg_price, cost=est.cost, fee=0.0,
                       order_id=f"paper-{next(self._ids)}", slippage_percent=est.slippage_percent)

    @staticmethod
    def _within_limit(levels, limit_price: float, side: str):
        """지정가 안쪽 호가만 — FOK 는 이 범위의 물량으로 전량 채워져야 체결, 아니면 주문 전체가 취소"""
        if side == "buy":
            return [lv for lv in levels if lv[0] <= limit_price]
        return [lv for lv in levels if lv[0] >= limit_price]

    @staticmethod
    def _nothing() -> LegFill:
        """FOK 미체결 = 취소, 아무 일도 안 일어남 (브로커는 qty 0 을 '놓침' 으로 본다)"""
        return LegFill(qty=0.0, avg_price=0.0, cost=0.0, fee=0.0, order_id="paper-fok-miss")

    def _done(self, cid: str, f: LegFill) -> LegFill:
        """추적번호로 결과를 기억해 둔다 (lookup 으로 되찾기 위해)"""
        self._by_cid[cid] = f
        return f

    def lookup(self, session: Session, side: str, coin: str, client_id: str) -> Optional[LegFill]:
        """추적번호로 주문 결과 찾기 (페이퍼는 이번 실행 중에 낸 것만 기억)"""
        return self._by_cid.get(client_id)

    def open_short(self, session: Session, quote: Quote, coin: str, qty: float, client_id: str = "") -> LegFill:
        """
        바이비트 숏 열기 — 매수 호가(bids)를 소진한다 (파는 쪽이니 불리한 쪽)

        USDT 에서 빠지는 것은 **숏 금액 전체가 아니라 증거금 + 수수료**다
        (증거금 = 숏 금액 ÷ 레버리지). 1배면 숏 금액 전체가 묶인다.
        """
        ratio = self._check_fail("open_short")
        f = self._leg(estimate_fill(quote.bybit.bids, qty * ratio), "바이비트 숏")
        fee = f.cost * self.fees.bybit_taker_rate(coin)
        self._add(session, BYBIT_USDT, -(f.cost / self.leverage + fee))
        self._add(session, short_key(coin), f.qty)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def close_short(self, session: Session, quote: Quote, coin: str, qty: float,
                    entry_price: float, margin_usdt: float, client_id: str = "") -> LegFill:
        """
        숏 qty 개 닫기 — 매도 호가(asks)를 소진한다

        USDT 로 돌아오는 것 = 증거금 + 숏 손익 − 수수료.
        숏 손익은 (진입가 − 청산가) x 개수다 (가격이 내려가야 이익).
        일부만 체결되면 증거금도 그 비율만큼만 돌려받는다 (남은 개수는 다음 스캔에서 이어서 닫는다).

        Args:
            margin_usdt: 닫는 개수에 해당하는 증거금
        """
        ratio = self._check_fail("close_short")
        f = self._leg(estimate_fill(quote.bybit.asks, qty * ratio), "바이비트 숏 닫기")
        fee = f.cost * self.fees.bybit_taker_rate(coin)
        released = margin_usdt * (f.qty / qty) if qty else 0.0
        self._add(session, BYBIT_USDT, released + (entry_price - f.avg_price) * f.qty - fee)
        self._add(session, short_key(coin), -f.qty)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def short_liquidated(self, session: Session, coin: str) -> bool:
        """거래소가 숏을 강제청산했나? 페이퍼 거래소는 강제청산 가격에 닿으면 바로 청산한다고 본다"""
        return True

    def short_gone(self, session: Session, coin: str) -> bool:
        """거래소에 숏이 남아 있지 않나 (장부 개수 0). short_liquidated 와 달리 '가격' 이 아니라 '실제 유무' 다"""
        return self._get(session, short_key(coin)) <= 0

    def mark_liquidated(self, session: Session, coin: str, qty: float) -> None:
        """바이비트가 숏을 강제청산함: 증거금은 이미 빠져 있으니 숏 개수만 0으로"""
        self._add(session, short_key(coin), -qty)

    def position_liq_price(self, session: Session, coin: str) -> Optional[float]:
        """거래소가 알려주는 강제청산가. 페이퍼 거래소는 없다 → None (봇 계산값을 쓴다)"""
        return None

    def add_margin(self, session: Session, coin: str, usdt: float) -> float:
        """숏 포지션 증거금에 USDT 를 더 넣는다 (계좌 여유 → 포지션 주머니). 페이퍼는 장부에서 뺀다"""
        self._add(session, BYBIT_USDT, -usdt)
        return usdt

    # ---- 업비트 네 동작. 진입은 USDT→원화→코인, 청산은 코인→원화→USDT 로 되돌아온다 ----
    # '개수 주문' 은 bids(파는 쪽), '금액 주문' 은 asks(사는 쪽) 를 소진한다. 항상 불리한 쪽이다.
    def sell_usdt(self, session: Session, quote: Quote, amount_usdt: float, client_id: str = "") -> LegFill:
        """진입 1단계: USDT 를 팔아 원화 만들기 (원화 += 금액 − 수수료)"""
        ratio = self._check_fail("sell_usdt")
        f = self._leg(estimate_fill(quote.usdt.bids, amount_usdt * ratio), "업비트 USDT 매도")
        fee = f.cost * self.fees.upbit_trade_fee_rate
        self._add(session, UPBIT_USDT, -f.qty)
        self._add(session, UPBIT_KRW, f.cost - fee)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def buy_coin_with_krw(self, session: Session, quote: Quote, coin: str, krw: float,
                          client_id: str = "", limit_price: Optional[float] = None) -> LegFill:
        """
        진입 2단계: 원화로 코인 사기 (원화 −= 금액 + 수수료). 금액 지정 주문이다

        limit_price 가 있으면 지정가 FOK: 그 가격 이하 호가로 전량 채워지면 체결, 아니면 체결 0 (취소).
        실거래와 같은 규칙 — 판단가보다 비싼 줄은 안 먹는다.
        """
        ratio = self._check_fail("buy_coin_with_krw")
        if limit_price is not None:
            est = estimate_fill_by_cost(self._within_limit(quote.upbit.asks, limit_price, "buy"), krw * ratio)
            if est is None or not est.is_complete:
                return self._done(client_id, self._nothing())
            f = self._leg(est, "업비트 코인 매수")
        else:
            f = self._leg(estimate_fill_by_cost(quote.upbit.asks, krw * ratio), "업비트 코인 매수")
        fee = f.cost * self.fees.upbit_trade_rate(coin)
        self._add(session, UPBIT_KRW, -(f.cost + fee))
        self._add(session, coin_key(coin), f.qty)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def sell_coin(self, session: Session, quote: Quote, coin: str, qty: float, client_id: str = "",
                  limit_price: Optional[float] = None) -> LegFill:
        """청산 1단계: 코인을 팔아 원화 만들기. limit_price 가 있으면 지정가 FOK (그 가격 이상 호가로 전량, 아니면 0)"""
        ratio = self._check_fail("sell_coin")
        if limit_price is not None:
            est = estimate_fill(self._within_limit(quote.upbit.bids, limit_price, "sell"), qty * ratio)
            if est is None or not est.is_complete:
                return self._done(client_id, self._nothing())
            f = self._leg(est, "업비트 코인 매도")
        else:
            f = self._leg(estimate_fill(quote.upbit.bids, qty * ratio), "업비트 코인 매도")
        fee = f.cost * self.fees.upbit_trade_rate(coin)
        self._add(session, coin_key(coin), -f.qty)
        self._add(session, UPBIT_KRW, f.cost - fee)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def buy_usdt_with_krw(self, session: Session, quote: Quote, krw: float, client_id: str = "") -> LegFill:
        """청산 2단계: 원화로 USDT 되사기 — 여기까지 오면 자본이 다시 USDT 가 된다"""
        ratio = self._check_fail("buy_usdt_with_krw")
        f = self._leg(estimate_fill_by_cost(quote.usdt.asks, krw * ratio), "업비트 USDT 매수")
        fee = f.cost * self.fees.upbit_trade_fee_rate
        self._add(session, UPBIT_KRW, -(f.cost + fee))
        self._add(session, UPBIT_USDT, f.qty)
        return self._done(client_id, LegFill(f.qty, f.avg_price, f.cost, fee, f.order_id, f.slippage_percent))

    def credit_funding(self, session: Session, amount_usdt: float) -> None:
        """펀딩비 정산 (실거래는 거래소가 알아서 하니 할 일 없음)"""
        self._add(session, BYBIT_USDT, amount_usdt)

    # ------------------------------------------------------------------
    # 송금
    # ------------------------------------------------------------------
    def withdraw(self, session: Session, to_bybit: bool, amount_usdt: float, network: str) -> str:
        """출금 요청: 보내는 쪽에서 바로 빠진다. 받는 쪽 입금은 transfer_arrived 에서"""
        self._check_fail("withdraw")
        self._add(session, UPBIT_USDT if to_bybit else BYBIT_USDT, -amount_usdt)
        return f"paper-w{next(self._ids)}"

    def transfer_arrived(self, session: Session, transfer) -> Optional[bool]:
        """
        받는 쪽에 들어왔나? True = 도착 / False = 실패 / None = 아직

        페이퍼는 바로 도착한다 (받는 쪽 +(개수 − 수수료)).
        """
        self._add(session, BYBIT_USDT if transfer.to_bybit else UPBIT_USDT,
                  transfer.amount_usdt - transfer.fee_usdt)
        return True
