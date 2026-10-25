"""
헷지 브로커: 진입·청산·되돌리기·손익·송금·대조 (페이퍼·드라이런·실거래 공통 로직)

실행기(kimp/executor.py)만 바꿔 끼운다. 이 파일의 흐름은 모드와 상관없이 똑같이 돈다.

진입 (세 단계, 페이퍼도 실거래도 이 순서)
------------------------------------------------------------------
  0) 계산만 먼저: 그 순간 호가로 체결 계산(EntryPlan) → 호가 충분? 너무 밀리지 않나? 양쪽 돈 충분?
  1) 업비트 USDT N 개 매도 (N = 계획한 숏 금액)  → 원화 (수수료 빼고). USDT 호가는 깊어서 밀림 0
  2) 그 원화 ÷ (1 + 수수료율) 로 코인 매수      (살 때 수수료는 원화에서 따로 빠짐)
       UPBIT_COIN_ORDER_TYPE=fok 면 판단한 가격에 지정가 '전량 즉시 체결 아니면 취소'.
       취소(놓침)면 숏이 아직 없으니 판 USDT 만 되사고 끝 (ABORTED / ENTRY_MISSED, 비용 ≈ USDT 왕복 수수료)
  3) 바이비트 숏: 실제로 산 코인 개수만큼       → 산 만큼만 숏을 쳐서 크기 맞추기가 거의 필요 없다
  포지션은 0) 다음 OPENING 으로 먼저 저장하고, 단계마다 체결(Fill)을 바로 저장한다.
  → 중간에 프로그램이 꺼져도 어디까지 했는지 남아 있다.
  (2026-10-26 까지는 숏 → USDT → 코인 순서였다. 코인 매수가 맨 뒤라 숏과 0.75초 벌어졌고, 코인을 못 사면 숏까지
   되돌려야 했다. 지정가로 바꾸며 코인을 먼저 사고 그 개수로 숏을 치는 순서로 — 두 가격 다리 사이가 0.2초로 준다)

  4) 양쪽 크기 맞추기: 부분 체결로 금액이 1% 넘게 다르면 큰 쪽을 줄인다 (비용은 이 포지션 진입 비용으로)

  중간 실패 → 끝난 단계를 거꾸로 되돌린다 (ABORTED)
    2) 실패: 받은 원화로 USDT 되사기
    3) 실패: 코인 팔기 + 받은 원화로 USDT 되사기
  되돌리기도 실패 → ERROR (사람이 거래소에서 정리, 신규 진입 중단, 긴급 알림)

주문마다 추적번호 (레그 리스크 대비)
------------------------------------------------------------------
  주문을 내기 '전에' 추적번호(client_id)와 함께 SENT 로 기록 → 주문 → DONE / VOID
  응답이 끊겨 결과를 모르면(OrderUnknown) 추측하지 않고, 다음 스캔에 추적번호로 거래소에 조회해서 채운 뒤 이어서

청산 (세 단계)
------------------------------------------------------------------
  1) 업비트 코인 매도 → 2) 받은 원화로 USDT 매수 → 3) 바이비트 숏 닫기 (강제청산이면 3 대신 '강제청산됨' 기록)
  익절 + UPBIT_COIN_ORDER_TYPE=fok: 1) 을 판단가 지정가 FOK 로 먼저 낸다. 전량 체결 안 되면 포지션을 OPEN 으로
  되돌리고 ExitDeferred (엔진이 잠시 뒤 다시 판단). 손절·강제청산·수동 청산은 시장가 그대로.
  CLOSING 으로 바꾸고 시작. 단계마다 '목표 양에 닿을 때까지' 남은 양만 다시 주문 (일부 체결 대비, 최대 3번).
  그래도 남거나 실패하면 CLOSING 그대로 두고, 다음 스캔(sync)에서 남은 양만 이어서.
  손익은 '실제 체결값' 으로 계산한다 (kimp/pnl.compute_pnl).

송금 (리밸런싱)
------------------------------------------------------------------
  출금 요청 → Transfer(REQUESTED) → 받는 쪽 입금 확인 → CONFIRMED
  송금 중에는 또 보내지 않는다. 송금 중인 돈은 '받는 쪽' 자본으로 친다.
  페이퍼는 요청하자마자 도착, 실거래는 수 분 걸린다.

대조 (RECONCILE_INTERVAL_SEC 마다)
------------------------------------------------------------------
  DB 의 열린 포지션 ↔ 실행기가 알려주는 실제 숏 개수·업비트 코인 개수
  다르면 신규 진입 중단 + 긴급 알림 (자동으로 고치지 않는다)

숏이 거래소에서 사라졌을 때 (매 스캔, vanished_shorts → 엔진이 정리)
------------------------------------------------------------------
  봇은 '가격 ≥ 계산한 강제청산가' 로 강제청산을 알아채지만, 거래소는 수수료·유지증거금 구간 때문에
  그보다 조금 아래에서 청산할 수 있다. 그러면 봇은 헷지가 살아 있다고 믿은 채 코인만 들고 있게 된다.
  → OPEN 포지션인데 거래소 숏이 0 이면 vanished_shorts 가 찾아 주고, 엔진이 그 스캔에 바로 강제청산
    경로로 코인을 팔아 USDT 로 되돌린다 (엔진 _close 를 거쳐야 재진입 대기·요약 로그가 같이 간다).
    청산 도중에 숏이 사라진 경우도 같다 (숏 닫기 주문을 내기 전에 확인해서 거절당하지 않는다).
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kimp.config import FeeSettings
from kimp.db import (
    ExitReason,
    Fill,
    FillState,
    FundingEvent,
    Position,
    PositionStatus,
    Transfer,
    TransferStatus,
    utcnow,
)
from kimp.executor import (
    BYBIT_USDT,
    UPBIT_KRW,
    UPBIT_USDT,
    LegFill,
    OrderFailed,
    OrderUnknown,
    PaperExecutor,
    coin_key,
    new_client_id,
)
from kimp.pnl import EntryLegs, ExitLegs, PnLBreakdown, compute_pnl, liquidation_price, liquidation_price_from_margin
from kimp.premium import ExitPlan, Quote, premium_percent, upbit_limit_price
from kimp.safety import Safety, kst_midnight_utc

# 송금 금액 소수점 자리수 상한. 거래소마다 다르다 — 둘 중 작은 쪽에 맞춘다
#   업비트: 6자리 초과 거절 (2026-10-22: 846.6747287521739 → "6 이하 소수점 자리수만 적용됩니다")
#   바이비트: 4자리 초과 거절 (2026-10-24: 73.684303 → "withdraws amount scale more than 4")
# 내림으로 자른다 (올리면 잔고보다 많이 보내려 할 수 있다)
LF = chr(10)

TRANSFER_DECIMALS = 4

# 거래소가 '규정상 지금은 안 된다' 고 거절할 때 나오는 말들.
# 우리 잘못이 아니므로 연속 실패로 세지 않고(세면 신규 진입이 막힌다) 잠시 뒤 다시 시도한다.
#   예) 업비트 출금지연제: 원화 입금 후 24시간 동안 그 금액 상당의 코인을 못 보낸다
#       "출금 가능한 수량을 초과하였습니다. 출금지연제 적용(공지 참고)에 따라 24시간 이내 입금하신
#        원화 입금액 상당의 코인은 출금 가능 수량에서 제외됩니다."
POLICY_REJECT_WORDS = ("출금지연", "출금 가능한 수량", "한도를 초과", "withdraw_delay", "exceeded",
                       "출금 가능 수량", "일일 출금", "출금이 제한")
# 규정 거절 뒤 다시 시도하기까지 기다리는 시간 (초). 매 스캔마다 찔러서 로그를 더럽히지 않으려고
POLICY_RETRY_SEC = 600.0


def dir_label(direction: str) -> str:
    """송금 방향 코드 → 알림용 한글. 'upbit->bybit' → '업비트 → 바이비트'"""
    return "업비트 → 바이비트" if direction == "upbit->bybit" else "바이비트 → 업비트"


def is_policy_reject(message: str) -> bool:
    """거래소 규정 때문에 거절된 것인가 (우리 코드·잔고 문제가 아닌 것)"""
    return any(word in message for word in POLICY_REJECT_WORDS)


logger = logging.getLogger(__name__)


class EntryRejected(Exception):
    """진입 못 함 (호가 부족, 잔고 부족, 너무 많이 밀림, 주문 실패 후 되돌림 등)"""


class EntryMissed(EntryRejected):
    """지정가(FOK) 코인 매수가 전량 체결되지 않아 진입을 접음. 판 USDT 는 되삼 (비용은 USDT 왕복 수수료 정도)"""


class ExitDeferred(Exception):
    """익절 지정가(FOK) 매도가 체결되지 않아 이번에는 청산하지 않음. 포지션은 OPEN 그대로, 엔진이 잠시 뒤 다시 판단"""


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
    "지금 청산하면 얼마?" 계산 결과 (판단용. 실제 청산은 이 순간 시세 quote 로 주문)

    Attributes:
        plan: 체결 계산 (업비트 코인 매도, USDT 매수, 바이비트 숏 닫기)
        legs: 손익 계산용 청산 값 / pnl: 손익 상세
        quote: 판단한 그 순간 시세 (페이퍼는 이 호가로 체결)
    """

    plan: ExitPlan
    legs: ExitLegs
    pnl: PnLBreakdown
    tether_premium: Optional[float] = None
    site_premium: Optional[float] = None
    fx_krw: Optional[float] = None
    quote: Optional[Quote] = None

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


# Fill.side 이름 → (거래소, 수수료 단위)
#   진입: open_short, sell_usdt, buy / 크기 맞추기: trim_short, trim_coin, trim_buy_usdt
#   되돌리기: unwind_sell, unwind_buy_usdt, unwind_close_short / 청산: sell, buy_usdt, close_short, liquidated
LEG_INFO = {
    "open_short": ("bybit", "USDT"), "sell_usdt": ("upbit", "KRW"), "buy": ("upbit", "KRW"),
    "trim_short": ("bybit", "USDT"), "trim_coin": ("upbit", "KRW"), "trim_buy_usdt": ("upbit", "KRW"),
    "sell": ("upbit", "KRW"), "buy_usdt": ("upbit", "KRW"), "close_short": ("bybit", "USDT"),
    "liquidated": ("bybit", "USDT"),
    "unwind_sell": ("upbit", "KRW"), "unwind_buy_usdt": ("upbit", "KRW"), "unwind_close_short": ("bybit", "USDT"),
}

# 청산 한 번에 남은 양을 다시 주문하는 최대 횟수
MAX_LEG_RETRIES = 3


@dataclass
class LegSum:
    """같은 단계 체결 여러 건의 합계. avg = 금액 합 ÷ 개수 합 (개수 가중 평균가)"""

    qty: float = 0.0
    cost: float = 0.0
    fee: float = 0.0

    @property
    def avg(self) -> float:
        """개수 가중 평균가. 같은 단계를 두 번 나눠 체결했을 때 단순 평균을 쓰면 틀린다"""
        return self.cost / self.qty if self.qty else 0.0


class HedgeBroker:
    """
    진입·청산·송금·대조 공통 로직. 체결은 executor 가 한다 (페이퍼/드라이런/실거래)

    설계의 핵심: **모드가 달라도 이 클래스의 판단은 똑같다.** 순서·되돌리기·손익·대조가
    여기 한 곳에만 있어서, 페이퍼에서 맞으면 실거래에서도 같은 판단이 나온다.

    중간에 끊겨도 이어서 할 수 있게 만든 장치 셋
      1) 주문을 내기 **전에** 체결 기록을 SENT 로 먼저 적고 추적번호를 같이 저장한다
         → 응답이 끊겨도 다음 스캔에 추적번호로 조회해서 결과를 채운다 (_resolve_sent)
      2) 남은 양은 '체결 기록이 있나' 가 아니라 **'목표 양에 닿았나'** 로 판단한다
         → 30개 중 20개만 팔렸으면 남은 10개만 다시 주문한다 (_continue_close)
      3) 결과를 모르는 주문은 **추측하지 않는다**. 포지션을 그대로 두고 신규 진입만 막는다
    """

    def __init__(self, executor, fees: FeeSettings, max_slippage_percent: float, leverage: float = 1.0,
                 mmr: float = 0.005, now_fn: Callable[[], datetime] = utcnow, safety: Optional[Safety] = None,
                 auto_transfer: Optional[bool] = None, network: str = "PAPER", transfer_min_usdt: float = 0.0,
                 transfer_max_usdt_per_day: Optional[float] = None, transfer_alert_minutes: float = 60.0,
                 reconcile_interval_sec: float = 600.0, hedge_tolerance_percent: float = 1.0,
                 coin_order_type: str = "market"):
        """
        Args:
            executor: 실행기 (PaperExecutor / DryRunExecutor / LiveExecutor)
            max_slippage_percent: 진입할 때 맨 앞 호가보다 이 % 이상 밀리면 진입 안 함
            coin_order_type: 업비트 코인 주문 방식 'market'(시장가) / 'fok'(판단가 지정가, 전량 즉시 체결 아니면 취소)
            leverage / mmr: 바이비트 레버리지, 유지증거금률 (증거금·강제청산 가격 계산)
            now_fn: 시각 (시뮬레이션은 과거 시각)
            safety: 안전장치 (없으면 막지 않음)
            auto_transfer: 송금이 필요할 때 자동으로 보낼지. None 이면 엔진이 설정(PAPER_AUTO_REBALANCE)을 따른다
        """
        self.ex = executor
        self.fees = fees
        self.max_slippage_percent = max_slippage_percent
        self.leverage = leverage
        self.mmr = mmr
        self._now = now_fn
        self.safety = safety or Safety(max_consecutive_failures=10 ** 9)
        self.auto_transfer = auto_transfer
        self.network = network
        self.transfer_min_usdt = transfer_min_usdt
        self.transfer_max_usdt_per_day = transfer_max_usdt_per_day
        self.transfer_alert_minutes = transfer_alert_minutes
        self.reconcile_interval_sec = reconcile_interval_sec
        self.hedge_tolerance_percent = hedge_tolerance_percent
        self.coin_order_type = coin_order_type
        self._last_reconcile = -1e18
        self._last_repeat: Dict[str, float] = {}   # 반복 알림 억제용 {키: 마지막으로 알린 시각}

    @property
    def is_paper(self) -> bool:
        return self.ex.is_paper

    def _log_repeat(self, key: str, level: int, message: str, cooldown_sec: float = 300.0) -> None:
        """
        매 스캔 되풀이되는 같은 문제는 처음 한 번만 그 레벨로, 그 뒤 cooldown 동안은 DEBUG 로

        청산이 막힌 포지션은 10초마다 다시 시도하는데, 그때마다 CRITICAL 을 찍으면 텔레그램이
        도배되어 정작 새로운 문제를 놓친다. 문제가 풀리면(성공) 키를 지워서 다음 번엔 다시 알린다.
        """
        now = self._now().timestamp()
        if now - self._last_repeat.get(key, -1e18) >= cooldown_sec:
            self._last_repeat[key] = now
            logger.log(level, message)
        else:
            logger.debug("(반복, 알림 생략) %s", message)

    # ------------------------------------------------------------------
    # 잔고
    # ------------------------------------------------------------------
    def ensure_initial_balances(self, session: Session, total_usdt: float, upbit_share: float) -> bool:
        """페이퍼: 처음 한 번 시작 자본 나눠 넣기 / 드라이런: 실제 잔고 복사 / 실거래: 할 일 없음"""
        return self.ex.ensure_initial(session, total_usdt, upbit_share)

    def balance(self, session: Session, asset: str) -> float:
        """쓸 수 있는 잔고 (UPBIT_USDT / BYBIT_USDT / UPBIT_KRW / coin_key(...))"""
        return self.ex.free(session, asset)

    def capital_split(self, session: Session, positions: Iterable[Position],
                      quotes: Dict[str, Quote]) -> Tuple[float, float]:
        """
        지금 양쪽 거래소에 있는 자본 (USDT 로 환산)

        업비트 = 남은 USDT + 자투리 원화 + 들고 있는 코인을 지금 팔아 USDT 로 되사면 받을 개수
        바이비트 = 남은 USDT + 포지션 증거금 + 숏 평가 손익
        송금 중인 돈은 받는 쪽으로 친다 (곧 도착하니까). 시세가 없는 코인은 진입 때 판 USDT 개수로.
        """
        any_quote = next(iter(quotes.values()), None)
        usdt_mid = any_quote.usdt_krw if any_quote else None
        upbit = self.balance(session, UPBIT_USDT)
        if usdt_mid:
            upbit += self.balance(session, UPBIT_KRW) / usdt_mid
        bybit = self.balance(session, BYBIT_USDT)
        for pos in positions:
            quote = quotes.get(pos.coin)
            plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_rate(pos.coin)) if quote else None
            upbit += plan.usdt_bought if plan else pos.usdt_sold
            short_pnl = (pos.entry_bybit_price_usdt - plan.bybit.avg_price) * pos.bybit_qty if plan else 0.0
            bybit += pos.bybit_margin_usdt + short_pnl
        for t in self.pending_transfers(session):
            if t.to_bybit:
                bybit += t.amount_usdt - t.fee_usdt
            else:
                upbit += t.amount_usdt - t.fee_usdt
        return upbit, bybit

    # ------------------------------------------------------------------
    # 주문 한 동작 + 기록
    # ------------------------------------------------------------------
    def mark_decided(self) -> None:
        """
        엔진이 '주문하기로 정한' 순간을 표시한다 (익절·손절·진입 판단 직후)

        다음 주문 전송 때 '판단 → 전송 N ms' 를 로그로 남겨, 판단과 거래소 매칭 사이에서 우리 쪽이 먹는
        시간을 잰다 (시세 지연·체결 밀림을 줄이려면 어디를 줄여야 하는지 보려고). 한 번 쓰면 지운다.
        """
        self._decided_at = time.monotonic()

    def _log_send(self, pos: Position, sides: List[str]) -> None:
        """주문을 거래소로 보내기 직전: 판단 → 전송 걸린 시간 (mark_decided 가 없었으면 조용히)"""
        decided = getattr(self, "_decided_at", None)
        if decided is None:
            return
        self._decided_at = None
        logger.info("[주문 전송] #%d %s | 판단 → 전송 %d ms | %s",
                    pos.id, pos.coin, (time.monotonic() - decided) * 1000, ", ".join(sides))

    def _leg(self, session: Session, pos: Position, side: str, call: Callable[[str], LegFill],
             expected_price: Optional[float] = None) -> LegFill:
        """
        주문 한 동작: 추적번호를 붙여 SENT 로 먼저 저장 → 주문 → 결과로 DONE (안 들어갔으면 VOID)

        결과를 모르면(OrderUnknown) SENT 그대로 둔다 → 다음 스캔(sync)에서 추적번호로 조회해서 채운다.
        expected_price 는 분석용: 판단 때 호가 소진 계산으로 예상한 평균가 (실제 avg_price 와 비교하려고 남긴다).
        """
        row = self._sent_row(session, pos, side, expected_price)
        self._log_send(pos, [side])
        try:
            f = call(row.client_id)
        except OrderUnknown:
            raise
        except OrderFailed:
            self._settle(session, row, failed=True)
            raise
        self._settle(session, row, f)
        return f

    def _sent_row(self, session: Session, pos: Position, side: str, expected_price: Optional[float],
                  commit: bool = True) -> Fill:
        """
        주문을 내기 전에 SENT 기록을 먼저 적는다 (추적번호 포함). 메인 스레드에서만 부른다

        commit=False 면 flush 만 한다 — 동시 주문은 다리마다 커밋하지 않고 묶어서 한 번 커밋한다
        (주문 앞 커밋 하나가 SQLite 에서 수십 ms). 주문이 나가기 전에 커밋되는 것은 같다.
        """
        exchange, cur = LEG_INFO[side]
        row = Fill(position_id=pos.id, exchange=exchange, side=side, qty=0.0, avg_price=0.0, cost=0.0, fee=0.0,
                   fee_currency=cur, slippage_percent=0.0, client_id=new_client_id(), state=FillState.SENT,
                   expected_price=expected_price, created_at=self._now())
        session.add(row)
        if commit:
            session.commit()
        else:
            session.flush()
        return row

    def _settle(self, session: Session, row: Fill, f: Optional[LegFill] = None, failed: bool = False) -> None:
        """SENT 기록에 결과를 채운다: 체결값이면 DONE, 안 들어갔으면 VOID. 메인 스레드에서만 부른다"""
        if failed:
            row.state = FillState.VOID
        else:
            self._fill_row(row, f)
        row.done_at = self._now()
        session.commit()

    def _legs_parallel(self, session: Session, pos: Position,
                       specs: List[Tuple[str, Callable[[str], LegFill], Optional[float]]]) -> Dict[str, LegFill]:
        """
        서로 독립인 주문 여러 개를 **동시에** 낸다 (실거래). 페이퍼는 장부(DB)를 쓰므로 순차로

        왜 동시인가: 청산의 '코인 매도' 와 '숏 닫기', 진입의 '숏 열기' 와 'USDT 매도' 는 서로 기다릴 이유가
        없다. 순차로 내면 0.5~1초 사이 가격이 움직여 한쪽만 먼저 체결된 창이 생긴다 (IRYS #3: −1.6%).
        DB 는 스레드에서 안 쓴다 — SENT 기록은 여기서 먼저, 스레드는 거래소 호출만, 결과 기록은 다시 여기서.
        각 호출은 서로 다른 거래소 객체를 쓴다 (업비트 ∥ 바이비트) — 같은 ccxt 객체를 두 스레드가 쓰면 안 된다.

        Args:
            specs: [(단계 이름, 추적번호 → 체결값 함수, 예상가)]
        Returns:
            {단계 이름: 체결값} — 실패한 단계는 빠진다
        Raises:
            OrderUnknown: 어느 하나라도 결과를 모름 (그 기록은 SENT 로 남고, 나머지 결과는 이미 적혔다)
            OrderFailed: 어느 하나가 안 들어감 (그 기록은 VOID, 나머지 결과는 이미 적혔다)
        """
        if self.ex.is_paper or len(specs) <= 1:
            return {side: self._leg(session, pos, side, call, expected_price=exp) for side, call, exp in specs}
        rows = [(side, self._sent_row(session, pos, side, exp, commit=False), call) for side, call, exp in specs]
        session.commit()   # SENT 기록들을 한 번에 — 주문은 이 뒤에 나간다
        self._log_send(pos, [side for side, _, _ in rows])
        with ThreadPoolExecutor(max_workers=len(rows)) as pool:
            futures = [(side, row, pool.submit(call, row.client_id)) for side, row, call in rows]
            results: Dict[str, LegFill] = {}
            unknown: Optional[Exception] = None
            failed: Optional[Exception] = None
            for side, row, fut in futures:
                try:
                    f = fut.result()
                except OrderUnknown as exc:
                    unknown = exc            # SENT 그대로 → 다음 스캔에 추적번호로 조회
                except OrderFailed as exc:
                    failed = exc
                    self._settle(session, row, failed=True)
                else:
                    results[side] = f
                    self._settle(session, row, f)
        if unknown is not None:
            raise unknown
        if failed is not None:
            raise failed
        return results

    @staticmethod
    def _fill_row(row: Fill, f: LegFill) -> None:
        """미리 적어 둔 체결 기록(SENT)에 실제 결과를 채우고 DONE 으로 바꾼다"""
        row.qty, row.avg_price, row.cost, row.fee = f.qty, f.avg_price, f.cost, f.fee
        row.order_id, row.slippage_percent, row.state = f.order_id, f.slippage_percent, FillState.DONE

    @staticmethod
    def _sums(session: Session, pos: Position) -> Dict[str, LegSum]:
        """이 포지션의 체결 합계 {단계: LegSum} (같은 단계가 여러 건이면 더함, 확인된 것만)"""
        out: Dict[str, LegSum] = {}
        for f in session.scalars(select(Fill).where(Fill.position_id == pos.id).order_by(Fill.id)):
            if f.state not in (None, FillState.DONE):
                continue
            s = out.setdefault(f.side, LegSum())
            s.qty += f.qty
            s.cost += f.cost if f.cost is not None else f.qty * f.avg_price
            s.fee += f.fee
        return out

    def _resolve_sent(self, session: Session, pos: Position) -> bool:
        """
        결과를 모르는 주문(SENT)을 추적번호로 조회해서 채운다

        Returns:
            전부 정리됐으면 True. 아직 모르는 게 남았으면 False (이 포지션은 이번 스캔에 손대지 않음)
        """
        rows = list(session.scalars(select(Fill).where(Fill.position_id == pos.id, Fill.state == FillState.SENT)))
        for row in rows:
            try:
                f = self.ex.lookup(session, row.side, pos.coin, row.client_id)
            except Exception as exc:
                self._log_repeat(f"lookup-{pos.id}", logging.WARNING,
                                 f"#{pos.id} {pos.coin} 주문 {row.client_id} 결과 조회 실패 (다음에 다시): {exc}")
                return False
            self._last_repeat.pop(f"lookup-{pos.id}", None)
            if f is None or f.qty <= 0:
                row.state = FillState.VOID
            else:
                self._fill_row(row, f)
                logger.warning("[복구] #%d %s | %s 주문 결과를 조회로 찾음 | %g개 평균 %.8g",
                               pos.id, pos.coin, row.side, f.qty, f.avg_price)
            row.done_at = self._now()
            session.commit()
        self.safety.clear(f"unknown-{pos.id}")
        return True

    def _order_unknown(self, session: Session, pos: Position, exc: Exception) -> None:
        """주문 결과를 모름: 포지션은 그대로(OPENING/CLOSING) 두고 막아둔 뒤, 다음 스캔에 조회해서 이어서"""
        self.safety.record_failure(f"{pos.coin}: {exc}")
        self.safety.block(f"unknown-{pos.id}", f"#{pos.id} {pos.coin} 주문 결과를 모름 — 추적번호로 계속 조회 중")
        logger.critical("[주문 결과 모름] #%d %s | %s | 추측하지 않고 다음 스캔에 추적번호로 조회해 이어서",
                        pos.id, pos.coin, exc)

    # ------------------------------------------------------------------
    # 진입
    # ------------------------------------------------------------------
    def _coin_limit(self, price: Optional[float], side: str) -> Optional[float]:
        """
        업비트 코인 주문에 쓸 지정가. market 모드면 None (= 시장가)

        판단 때 호가 소진 계산으로 나온 평균가를 호가 단위에 맞춘다 — 살 때 내림, 팔 때 올림 (판단보다 나쁘게 안 되게).
        예) 매수 판단 140.0원 → 140원 / 매도 판단 93.47원 → 93.5원
        """
        if self.coin_order_type != "fok" or not price:
            return None
        return upbit_limit_price(price, side)

    def _abort_missed_entry(self, session: Session, pos: Position, quote: Optional[Quote], limit: float) -> None:
        """
        지정가 코인 매수가 전량 체결되지 않음(놓침): 숏은 아직 없으니 판 USDT 만 되산다 → ABORTED / ENTRY_MISSED

        비용 = 되산 USDT − 판 USDT (USDT 왕복 수수료 0.1% + USDT 호가 한 칸 정도, 20 USDT 면 약 0.03 USDT).
        되사는 주문의 결과를 모르면 OPENING 으로 두고 다음 스캔 sync 가 추적번호로 채운 뒤 되돌리기를 이어서 한다.
        """
        try:
            self._return_leftover_krw(session, pos, quote)
        except OrderUnknown as exc:
            self._order_unknown(session, pos, exc)
            raise EntryRejected(f"진입 놓침 뒤 USDT 되사기 결과 모름 → 다음 스캔에 조회: {exc}") from exc
        sums = self._sums(session, pos)
        net = sums.get("trim_buy_usdt", LegSum()).qty - sums["sell_usdt"].qty
        pos.status, pos.closed_at, pos.exit_reason = PositionStatus.ABORTED, self._now(), "ENTRY_MISSED"
        pos.net_pnl_usdt = net
        session.commit()
        logger.info("[진입 놓침] #%d %s | 지정가 %g원에 전량 체결 안 됨 (호가가 비껴감) → 판 USDT 되삼 | 비용 %+.4f USDT",
                    pos.id, pos.coin, limit, net)

    def open_hedge(self, session: Session, quote: Quote, bybit_qty: float) -> Position:
        """
        진입 (파일 맨 위 설명 순서) → 양쪽 크기 맞추기 → OPEN

        Raises:
            InsufficientBalance: 한쪽 USDT 가 모자람 (아무 주문도 안 냄)
            EntryMissed: fok 모드에서 코인 매수가 전량 체결 안 됨 → 판 USDT 되삼 (ABORTED / ENTRY_MISSED)
            EntryRejected: 호가 부족 / 너무 밀림 (아무 주문도 안 냄), 주문 실패 후 되돌림 (ABORTED),
                           주문 결과 모름 (OPENING 으로 남고 다음 스캔에 이어서)
        """
        rate = self.fees.upbit_trade_rate(quote.coin)   # 코인별 업비트 요율 (USDT/KRW 매매는 공통값)
        plan = quote.entry_plan(bybit_qty, rate)
        if plan is None or not plan.bybit.is_complete:
            raise EntryRejected("바이비트 매수 호가가 모자람")
        if not plan.usdt.is_complete:
            raise EntryRejected("업비트 USDT 매수 호가가 모자람")
        if not plan.upbit.is_complete:
            raise EntryRejected("업비트 코인 매도 호가가 모자람")
        for name, fill in (("업비트 코인", plan.upbit), ("업비트 USDT", plan.usdt), ("바이비트", plan.bybit)):
            if fill.slippage_percent > self.max_slippage_percent:
                raise EntryRejected(f"{name}에서 너무 밀림 {fill.slippage_percent:.3f}%")

        margin = plan.bybit.cost / self.leverage
        by_fee = plan.bybit.cost * self.fees.bybit_taker_rate(quote.coin)   # 코인별 요율
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

        # 0) 시작 기록 (중간에 꺼져도 남도록)
        pos = Position(
            coin=quote.coin, status=PositionStatus.OPENING, is_paper=self.ex.is_paper, opened_at=self._now(),
            usdt_sold=0.0, entry_usdt_price_krw=0.0, entry_usdt_fee_krw=0.0, upbit_qty=0.0,
            entry_upbit_price_krw=0.0, entry_upbit_fee_krw=0.0, bybit_qty=0.0, entry_bybit_price_usdt=0.0,
            entry_bybit_fee_usdt=0.0, entry_premium=plan.premium, entry_tether_premium=quote.tether_premium,
            entry_site_premium=quote.site_premium, entry_fx_krw=quote.fx_krw, leverage=self.leverage,
            bybit_margin_usdt=0.0, liquidation_price=0.0, funding_usdt=0.0,
            planned_entry_premium=plan.premium,   # 분석용: 실제 entry_premium 과의 차이 = 주문 사이 밀림
        )
        session.add(pos)
        session.commit()

        coin, ex = quote.coin, self.ex
        limit = self._coin_limit(plan.upbit.avg_price, "buy")   # fok 면 판단가 (호가 단위 내림), market 이면 None
        try:
            # 1) USDT 매도 (호가가 깊어 밀림 0) → 2) 그 원화로 코인 매수 → 3) 실제로 산 개수만큼 숏.
            #    코인이 먼저라 가격이 정해지는 두 다리(코인·숏) 사이가 0.2초 안팎이고, 코인을 못 사면 되돌릴 게 USDT 뿐이다.
            usdt = self._leg(session, pos, "sell_usdt",
                             lambda cid: ex.sell_usdt(session, quote, plan.usdt_sold, client_id=cid),
                             expected_price=plan.usdt.avg_price)
            krw_for_coin = (usdt.cost - usdt.fee) / (1 + rate)  # 2)
            bought = self._leg(session, pos, "buy",
                               lambda cid: ex.buy_coin_with_krw(session, quote, coin, krw_for_coin, client_id=cid,
                                                                limit_price=limit),
                               expected_price=plan.upbit.avg_price)
            if bought.qty <= 0:   # 지정가 FOK 취소 (시장가는 체결 0 이면 OrderFailed 로 아래 되돌리기 경로)
                self._abort_missed_entry(session, pos, quote, limit or 0.0)
                raise EntryMissed(f"지정가 {limit:g}원에 전량 체결 안 됨 → 판 USDT 되삼")
            self._leg(session, pos, "open_short",   # 3) 산 만큼 숏 (거래소 주문 단위로 맞춰진다)
                      lambda cid: ex.open_short(session, quote, coin, bought.qty, client_id=cid),
                      expected_price=plan.bybit.avg_price)
        except OrderUnknown as exc:
            self._order_unknown(session, pos, exc)
            raise EntryRejected(f"주문 결과 모름 → 다음 스캔에 조회: {exc}") from exc
        except OrderFailed as exc:
            self.safety.record_failure(f"{coin} 진입: {exc}")
            if not self._unwind_entry(session, pos, quote, str(exc)):
                raise EntryRejected(f"주문 실패 → 되돌리는 중 결과 모름, 다음 스캔에 이어서: {exc}") from exc
            raise EntryRejected(f"주문 실패 → 되돌림: {exc}") from exc

        # 4) 크기 맞추기 + 남은 원화 되돌리기. 여기서 주문 결과를 모르면 OPEN 으로 끝내면 안 된다 —
        #    OPEN 포지션의 SENT 기록은 아무도 다시 조회하지 않아서 DB 숏 개수가 실제와 어긋난 채 굳는다.
        #    OPENING 으로 두고 멈추면 다음 스캔 sync 가 추적번호로 채운 뒤 이 단계부터 이어서 한다.
        try:
            self._balance_hedge(session, pos, quote)
            self._return_leftover_krw(session, pos, quote)
        except OrderUnknown as exc:
            self._order_unknown(session, pos, exc)
            raise EntryRejected(f"크기 맞추기 결과 모름 → 다음 스캔에 조회해서 이어서: {exc}") from exc
        self._finish_entry(session, pos)
        self.safety.record_success()
        # 주문 세 개를 순서대로 내는 사이(보통 1~3초)에 가격이 움직이면 실제 진입 김프가 판단 때보다 나빠진다.
        # 포지션은 양쪽 다 들어간 정상 헷지라 되돌리지 않지만, 목표까지 그만큼 더 올라야 하므로 알린다.
        drift = pos.entry_premium - plan.premium
        if drift > self.max_slippage_percent:
            logger.warning("[진입 김프 밀림] #%d %s | 판단 %+.3f%% → 실제 %+.3f%% (%+.3f%%p 불리) | 주문 사이에 가격이 움직임 | "
                           "헷지는 정상, 목표까지 그만큼 더 올라야 합니다",
                           pos.id, coin, plan.premium, pos.entry_premium, drift)
        return pos

    def _balance_hedge(self, session: Session, pos: Position, quote: Optional[Quote]) -> None:
        """
        양쪽 크기 맞추기 (진입 직후): 코인 개수와 숏 개수가 HEDGE_TOLERANCE_PERCENT(1%) 넘게 다르면 큰 쪽을 줄인다

        **개수**로 비교한다 (금액이 아니라). 헷지는 '코인 N개 ↔ 숏 N개' 라야 가격이 어디로 가든 손익이 0 이다.
        금액으로 비교하면 김프 −1% 로 진입한 정상 헷지도 1% 어긋난 것으로 보인다 (업비트 쪽이 1% 싸니까).
          - 숏이 많으면: 남는 숏 닫기 (trim_short) — 숏이 부분 체결보다 많이 들어갈 일은 거의 없지만 주문 단위 반올림용
          - 코인이 많으면: 남는 코인 팔기 (trim_coin) — 숏이 일부만 체결됐을 때. 남은 원화는 _return_leftover_krw 가 USDT 로
        맞추는 비용은 이 포지션 진입 비용에 들어간다 (_finish_entry). 실패하면 그대로 두고 긴급 알림.
        예) 코인 50개 샀는데 숏이 40개만 체결 → 코인 10개 팔기 (수수료 0.05% + 호가 한 칸)
        보통은 숏을 '산 코인 개수' 로 치니 여기 올 일이 거의 없다 (2026-10-26 순서 변경 전에는 숏이 먼저라 늘 0.2% 쯤 달랐다)
        """
        sums = self._sums(session, pos)
        o = sums["open_short"]
        short_qty = o.qty - sums.get("trim_short", LegSum()).qty
        coin_qty = sums["buy"].qty - sums.get("trim_coin", LegSum()).qty
        if short_qty <= 0:
            return
        diff = short_qty - coin_qty
        if abs(diff) <= short_qty * self.hedge_tolerance_percent / 100:
            return
        logger.warning("[크기 맞추기] #%d %s | 코인 %g개 / 숏 %g개 (차이 %.2f%%) | 큰 쪽을 줄입니다",
                       pos.id, pos.coin, coin_qty, short_qty, abs(diff) / short_qty * 100)
        try:
            if diff > 0:
                self._leg(session, pos, "trim_short", lambda cid: self.ex.close_short(
                    session, quote, pos.coin, diff, o.avg, diff * o.avg / self.leverage, client_id=cid))
            else:
                qty = -diff
                coin_bid = quote.upbit.best_bid if quote else None
                if coin_bid is None or qty * coin_bid < self.ex.min_order_krw(pos.coin):
                    return  # 업비트 최소 주문보다 작아서 못 팖 (그대로 둠) / 시세가 없어 금액을 모름
                self._leg(session, pos, "trim_coin",
                          lambda cid: self.ex.sell_coin(session, quote, pos.coin, qty, client_id=cid))
        except OrderUnknown:
            raise  # 결과를 모르면 추측하지 않는다 → 부르는 쪽이 OPENING 으로 두고 다음 스캔에 조회
        except OrderFailed as exc:
            self.safety.record_failure(f"{pos.coin} 크기 맞추기: {exc}")
            logger.critical("[크기 맞추기 실패] #%d %s | 코인 %g개 / 숏 %g개 그대로 OPEN | %s", pos.id, pos.coin,
                            coin_qty, short_qty, exc)

    def _return_leftover_krw(self, session: Session, pos: Position, quote: Optional[Quote]) -> None:
        """
        진입하고 남은 원화를 USDT 로 되돌리기 (trim_buy_usdt)

        예) USDT 100개 판 원화로 코인을 사려 했는데 80% 만 사짐 → 약 20 USDT 어치 원화가 남음
            → 그 원화로 USDT 를 되사고, '판 USDT' 에서 되산 만큼 뺀다 (손익 기록이 실제 잔고와 맞게)
        업비트 최소 주문 금액(기본 5,000원) 미만 자투리는 그냥 둔다.
        """
        rate = self.fees.upbit_trade_rate(pos.coin)
        sums = self._sums(session, pos)
        s = lambda k: sums.get(k, LegSum())  # noqa: E731
        krw_in = sum(s(k).cost - s(k).fee for k in ("sell_usdt", "trim_coin"))
        krw_out = sum(s(k).cost + s(k).fee for k in ("buy", "trim_buy_usdt"))
        left = krw_in - krw_out
        if left < self.ex.min_order_krw("USDT"):
            return
        try:
            self._leg(session, pos, "trim_buy_usdt", lambda cid: self.ex.buy_usdt_with_krw(
                session, quote, left / (1 + rate), client_id=cid))
        except OrderUnknown:
            raise  # 위와 같다 — 추측해서 OPEN 으로 끝내지 않는다
        except OrderFailed as exc:
            self.safety.record_failure(f"{pos.coin} 남은 원화 되돌리기: {exc}")
            logger.critical("[남은 원화 되돌리기 실패] #%d %s | 업비트에 %s원 남음 | %s",
                            pos.id, pos.coin, f"{left:,.0f}", exc)

    def _finish_entry(self, session: Session, pos: Position) -> None:
        """
        체결 합계로 포지션 채우기 → OPEN

        크기 맞추기 비용도 여기서 진입 비용에 넣는다:
          - 숏을 줄였으면: 줄인 부분의 손익 (팔 때 − 되살 때) − 수수료 → 바이비트 진입 비용에 더함
          - 코인을 줄였으면: 되산 USDT 만큼 '판 USDT' 에서 빼고, 수수료는 업비트 진입 비용에 더함
        """
        sums = self._sums(session, pos)
        o, u, b = sums["open_short"], sums["sell_usdt"], sums["buy"]
        ts, tc, tb = (sums.get(k, LegSum()) for k in ("trim_short", "trim_coin", "trim_buy_usdt"))
        trim_pnl = (o.avg - ts.avg) * ts.qty - ts.fee if ts.qty else 0.0
        pos.bybit_qty, pos.entry_bybit_price_usdt = o.qty - ts.qty, o.avg
        pos.entry_bybit_fee_usdt = o.fee - trim_pnl
        pos.usdt_sold, pos.entry_usdt_price_krw, pos.entry_usdt_fee_krw = u.qty - tb.qty, u.avg, u.fee + tb.fee
        pos.upbit_qty, pos.entry_upbit_price_krw, pos.entry_upbit_fee_krw = b.qty - tc.qty, b.avg, b.fee + tc.fee
        pos.entry_premium = premium_percent(b.avg, o.avg, u.avg)
        pos.bybit_margin_usdt = pos.bybit_qty * o.avg / self.leverage
        pos.liquidation_price = liquidation_price(o.avg, self.leverage, self.mmr)
        pos.status = PositionStatus.OPEN
        session.commit()

    def _unwind_entry(self, session: Session, pos: Position, quote: Optional[Quote], why: str) -> bool:
        """
        진입 중 실패: 끝난 단계를 거꾸로 되돌린다 → ABORTED (되돌리기도 실패하면 ERROR)

        남은 것을 계산해서 그만큼만 되돌린다 (일부 체결·크기 맞추기까지 포함)
          코인이 남았으면 팔고 → 남은 원화로 USDT 되사고 → 남은 숏 닫기
        예) 숏 100개 + USDT 50개 판 뒤 코인 매수 실패 → 받은 원화로 USDT 되사기 → 숏 100개 닫기

        되돌리는 주문의 **결과를 모르면** ERROR 로 끝내지 않는다. OPENING 그대로 두면 다음 스캔 sync 가
        추적번호로 조회해 채운 뒤 이 함수를 다시 불러 남은 것만 되돌린다 (남은 양은 체결 합계로 다시 센다).

        Returns:
            끝났으면 True (ABORTED 또는 ERROR). 결과를 몰라 멈췄으면 False (OPENING 유지)
        """
        rate = self.fees.upbit_trade_rate(pos.coin)
        s = lambda k, sums: sums.get(k, LegSum())  # noqa: E731
        try:
            sums = self._sums(session, pos)
            coin_left = s("buy", sums).qty - s("trim_coin", sums).qty - s("unwind_sell", sums).qty
            if coin_left > 1e-12:
                self._leg(session, pos, "unwind_sell",
                          lambda cid: self.ex.sell_coin(session, quote, pos.coin, coin_left, client_id=cid))
            sums = self._sums(session, pos)
            krw_in = sum(s(k, sums).cost - s(k, sums).fee for k in ("sell_usdt", "trim_coin", "unwind_sell"))
            krw_out = sum(s(k, sums).cost + s(k, sums).fee for k in ("buy", "trim_buy_usdt", "unwind_buy_usdt"))
            if krw_in - krw_out >= self.ex.min_order_krw("USDT"):
                self._leg(session, pos, "unwind_buy_usdt", lambda cid: self.ex.buy_usdt_with_krw(
                    session, quote, (krw_in - krw_out) / (1 + rate), client_id=cid))
            sums = self._sums(session, pos)
            o = s("open_short", sums)
            short_left = o.qty - s("trim_short", sums).qty - s("unwind_close_short", sums).qty
            if short_left > o.qty * 1e-9:
                self._leg(session, pos, "unwind_close_short", lambda cid: self.ex.close_short(
                    session, quote, pos.coin, short_left, o.avg, short_left * o.avg / self.leverage, client_id=cid))
        except OrderUnknown as exc:
            self._order_unknown(session, pos, exc)
            return False
        except OrderFailed as exc:
            pos.status = PositionStatus.ERROR
            pos.exit_reason = "UNWIND_FAILED"
            session.commit()
            self.safety.record_failure(f"{pos.coin} 되돌리기: {exc}")
            logger.critical("[되돌리기 실패] #%d %s | 진입 실패: %s | 되돌리기도 실패: %s | 거래소에서 직접 확인·정리하세요 | "
                            "신규 진입 중단", pos.id, pos.coin, why, exc)
            return True

        # 되돌림 손익 (수수료·호가 간격 정도 손해)
        sums = self._sums(session, pos)
        o = s("open_short", sums)
        net = s("unwind_buy_usdt", sums).qty + s("trim_buy_usdt", sums).qty - s("sell_usdt", sums).qty
        for k in ("trim_short", "unwind_close_short"):
            c = s(k, sums)
            if c.qty:
                net += (o.avg - c.avg) * c.qty - c.fee
        net -= o.fee
        pos.status, pos.closed_at, pos.exit_reason = PositionStatus.ABORTED, self._now(), "ENTRY_FAILED"
        pos.net_pnl_usdt = net
        session.commit()
        logger.error("[진입 되돌림] #%d %s | %s | 끝난 단계를 되돌림 | 손익 %+.4f USDT", pos.id, pos.coin, why, net)
        return True

    # ------------------------------------------------------------------
    # 평가 / 청산
    # ------------------------------------------------------------------
    def value_exit(self, pos: Position, quote: Quote) -> Optional[ExitValuation]:
        """
        "지금 청산하면 얼마?" 계산 (주문은 안 냄)

        Returns:
            ExitValuation. 호가가 모자라서 다 못 팔면 None
        """
        plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_rate(pos.coin))
        if plan is None or not plan.is_complete:
            return None
        by_fee = plan.bybit.cost * self.fees.bybit_taker_rate(pos.coin)   # 코인별 요율 (CAP 0.11% / MINA 0.055%)
        legs = ExitLegs(upbit_price_krw=plan.upbit.avg_price, upbit_fee_krw=plan.coin_fee_krw,
                        usdt_price_krw=plan.usdt.avg_price, usdt_fee_krw=plan.usdt_fee_krw,
                        usdt_bought=plan.usdt_bought, bybit_price_usdt=plan.bybit.avg_price, bybit_fee_usdt=by_fee)
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees)
        return ExitValuation(plan=plan, legs=legs, pnl=pnl, tether_premium=quote.tether_premium,
                             site_premium=quote.site_premium, fx_krw=quote.fx_krw, quote=quote)

    def value_liquidation(self, pos: Position, quote: Quote) -> Optional[ExitValuation]:
        """
        바이비트 숏이 강제청산됐을 때 결과 계산

        바이비트: 증거금을 전부 잃는다 (숏 손익 = −증거금) / 업비트: 남은 코인을 지금 팔아 USDT 로 되산다
        """
        plan = quote.exit_plan(pos.upbit_qty, pos.bybit_qty, self.fees.upbit_trade_rate(pos.coin))
        if plan is None or not plan.upbit.is_complete or not plan.usdt.is_complete:
            return None
        legs = ExitLegs(upbit_price_krw=plan.upbit.avg_price, upbit_fee_krw=plan.coin_fee_krw,
                        usdt_price_krw=plan.usdt.avg_price, usdt_fee_krw=plan.usdt_fee_krw,
                        usdt_bought=plan.usdt_bought, bybit_price_usdt=pos.liquidation_price, bybit_fee_usdt=0.0)
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees,
                          bybit_leg_override_usdt=-pos.bybit_margin_usdt)
        return ExitValuation(plan=plan, legs=legs, pnl=pnl, tether_premium=quote.tether_premium,
                             site_premium=quote.site_premium, fx_krw=quote.fx_krw, quote=quote)

    def close_hedge(self, session: Session, pos: Position, valuation: ExitValuation, reason: str,
                    liquidated: bool = False) -> Optional[PnLBreakdown]:
        """
        청산 시작 (CLOSING) → 세 단계 실행

        Returns:
            확정 손익. 다 못 끝냈으면 None (CLOSING 으로 남고 다음 스캔에서 남은 양만 이어서)
        """
        pos.status = PositionStatus.CLOSING
        pos.exit_reason = ExitReason.LIQUIDATION if liquidated else reason
        pos.exit_tether_premium, pos.exit_site_premium, pos.exit_fx_krw = (
            valuation.tether_premium, valuation.site_premium, valuation.fx_krw)
        session.commit()
        return self._continue_close(session, pos, valuation.quote, valuation.plan)

    def _continue_close(self, session: Session, pos: Position, quote: Optional[Quote],
                        plan: Optional[ExitPlan] = None) -> Optional[PnLBreakdown]:
        """
        청산 남은 양만 실행 ('체결 기록이 있나' 가 아니라 '목표 양에 닿았나' 로 판단)

          1) 코인: 목표 = 진입 때 산 개수 (실제로 가진 만큼까지). 30개 중 20개만 팔렸으면 남은 10개만 다시
          2) USDT: 판 코인에서 받은 원화를 다 쓸 때까지 (5,000원 미만 자투리는 남김)
          3) 숏: 목표 = 숏 개수. 50개 중 30개만 닫혔으면 남은 20개만 다시
        한 번에 최대 3번까지 다시 주문. 그래도 남으면 CLOSING 으로 두고 다음 스캔에 이어서 + 긴급 알림.
        """
        if not self._resolve_sent(session, pos):
            return None
        rate = self.fees.upbit_trade_rate(pos.coin)
        liquidated = pos.exit_reason == ExitReason.LIQUIDATION
        if liquidated and not self.ex.short_liquidated(session, pos.coin):
            # 봇 계산으로는 강제청산 가격인데 거래소에서는 아직 숏이 살아 있음 (실거래) → 시장가로 바로 닫는다
            liquidated = False
            pos.exit_reason = ExitReason.STOP_LOSS
            logger.critical("[강제청산가 도달] #%d %s | 바이비트에 숏이 아직 있음 | 시장가로 바로 정리합니다", pos.id, pos.coin)
        # 단계별 체결 합계를 짧게 꺼내 쓰는 도우미 (없는 단계는 0 으로)
        s = lambda k, sums: sums.get(k, LegSum())  # noqa: E731
        coin_bid = quote.upbit.best_bid if quote else None
        # 분석용 예상가 (판단 때 호가 소진 계산). sync 에서 이어서 할 때는 plan 이 없어 비워 둔다
        exp_sell = plan.upbit.avg_price if plan else None
        exp_usdt = plan.usdt.avg_price if plan else None
        exp_short = plan.bybit.avg_price if plan else None
        done = False
        # 익절 + fok: 코인 매도를 판단가 지정가 FOK 로 먼저 낸다 (아직 아무것도 안 판 상태에서만).
        # 전량 체결 안 되면 포지션을 OPEN 으로 되돌리고 ExitDeferred — 다른 다리는 손대지 않았으니 되돌릴 게 없다.
        # sync 로 이어서 하는 청산(plan 없음)·손절·강제청산·수동 청산은 아래 시장가 경로 그대로.
        limit = self._coin_limit(exp_sell, "sell") if pos.exit_reason == ExitReason.TAKE_PROFIT else None
        try:
            if limit is not None and not s("sell", self._sums(session, pos)).qty:
                left = min(pos.upbit_qty, self.balance(session, coin_key(pos.coin)))
                sold = self._leg(session, pos, "sell", lambda cid: self.ex.sell_coin(
                    session, quote, pos.coin, left, client_id=cid, limit_price=limit), expected_price=exp_sell)
                if sold.qty <= 0:
                    pos.status, pos.exit_reason = PositionStatus.OPEN, None
                    session.commit()
                    raise ExitDeferred(f"익절 지정가 {limit:g}원에 전량 체결 안 됨 (호가가 비껴감)")
            for _ in range(MAX_LEG_RETRIES):
                sums = self._sums(session, pos)
                left = pos.upbit_qty - s("sell", sums).qty
                # 실제로 가진 것보다 많이 팔 수는 없다. 수수료가 코인으로 떼이거나
                # 대조가 어긋난 경우 진입 수량보다 적게 남아 있을 수 있다.
                left = min(left, self.balance(session, coin_key(pos.coin)))
                coin_done = left <= pos.upbit_qty * 1e-9 or (coin_bid is not None and
                                                              left * coin_bid < self.ex.min_order_krw(pos.coin))
                short_left = pos.bybit_qty - s("close_short", sums).qty
                if not liquidated and short_left > pos.bybit_qty * 1e-9 and self.ex.short_gone(session, pos.coin):
                    # 닫으려는데 거래소에 숏이 없다 (청산 도중 강제청산됐거나 누가 손으로 닫음).
                    # 주문을 내면 '포지션 없음' 으로 거절만 당하므로, 남은 양은 강제청산된 것으로 마무리한다
                    liquidated = True
                    pos.exit_reason = ExitReason.LIQUIDATION
                    session.commit()
                    logger.critical("[숏 사라짐] #%d %s | 청산 중 바이비트에 숏이 없음 (남은 %g개) | 강제청산으로 보고 "
                                    "증거금 손실로 마무리", pos.id, pos.coin, short_left)
                short_done = liquidated or short_left <= pos.bybit_qty * 1e-9

                # 1)+3) 코인 매도와 숏 닫기는 서로 독립 → 동시에 낸다 (실거래). 두 다리가 같은 순간에 체결되어
                #       급변 중 한쪽만 먼저 체결된 창이 줄어든다. 2) USDT 매수만 코인을 판 뒤에.
                specs = []
                if not coin_done:
                    specs.append(("sell", lambda cid, q=left: self.ex.sell_coin(session, quote, pos.coin, q, client_id=cid),
                                  exp_sell))
                if not short_done:
                    specs.append(("close_short", lambda cid, q=short_left: self.ex.close_short(
                        session, quote, pos.coin, q, pos.entry_bybit_price_usdt,
                        pos.bybit_margin_usdt * q / pos.bybit_qty, client_id=cid), exp_short))
                fills = self._legs_parallel(session, pos, specs) if specs else {}
                if "sell" in fills:
                    coin_done = fills["sell"].qty <= 0
                if "close_short" in fills:
                    f = fills["close_short"]
                    short_done = f.qty >= short_left * (1 - 1e-9) or f.qty <= 0

                if liquidated and not s("liquidated", sums).qty and short_left > pos.bybit_qty * 1e-9:
                    # 강제청산됨 (거래소가 이미 정리) — 남은 양만 기록한다
                    self.ex.mark_liquidated(session, pos.coin, short_left)
                    row = Fill(position_id=pos.id, exchange="bybit", side="liquidated", fee_currency="USDT",
                               slippage_percent=0.0, client_id="liquidated", qty=0, avg_price=0, cost=0, fee=0)
                    self._fill_row(row, LegFill(short_left, pos.liquidation_price,
                                                short_left * pos.liquidation_price, 0.0, "liquidated"))
                    session.add(row)
                    session.commit()

                sums = self._sums(session, pos)
                krw_left = (s("sell", sums).cost - s("sell", sums).fee) - (s("buy_usdt", sums).cost +
                                                                           s("buy_usdt", sums).fee)
                if krw_left >= self.ex.min_order_krw("USDT"):  # 2)
                    self._leg(session, pos, "buy_usdt", lambda cid: self.ex.buy_usdt_with_krw(
                        session, quote, krw_left / (1 + rate), client_id=cid), expected_price=exp_usdt)
                    sums = self._sums(session, pos)
                    krw_left = (s("sell", sums).cost - s("sell", sums).fee) - (s("buy_usdt", sums).cost +
                                                                               s("buy_usdt", sums).fee)
                if coin_done and short_done and krw_left < self.ex.min_order_krw("USDT"):
                    done = True
                    break
        except OrderUnknown as exc:
            self._order_unknown(session, pos, exc)
            return None
        except OrderFailed as exc:
            self.safety.record_failure(f"{pos.coin} 청산: {exc}")
            # 거래소 점검처럼 한참 막히면 10초마다 여기로 온다 → 5분에 한 번만 알린다
            self._log_repeat(f"close-{pos.id}", logging.CRITICAL,
                             f"[청산 실패] #{pos.id} {pos.coin} | {exc} | CLOSING 으로 두고 다음 스캔에 남은 양만 이어서 | "
                             f"같은 문제면 5분마다 알림")
            return None
        if not done:
            self.safety.record_failure(f"{pos.coin} 청산: {MAX_LEG_RETRIES}번 해도 다 안 끝남")
            self._log_repeat(f"close-{pos.id}", logging.CRITICAL,
                             f"[청산 덜 끝남] #{pos.id} {pos.coin} | {MAX_LEG_RETRIES}번 다시 주문해도 남음 | "
                             f"다음 스캔에 이어서 | 같은 문제면 5분마다 알림")
            return None
        self._last_repeat.pop(f"close-{pos.id}", None)   # 끝났으니 다음에 또 막히면 바로 알린다

        sums = self._sums(session, pos)
        sold, bought = s("sell", sums), s("buy_usdt", sums)
        closed, liq = s("close_short", sums), s("liquidated", sums)
        short = liq if (liquidated and liq.qty) else closed
        override = None
        if liquidated:
            # 바이비트 몫 = 강제청산된 양만큼 증거금 손실 + (그 전에 정상으로 닫은 양이 있으면 그 손익 − 수수료)
            override = -pos.bybit_margin_usdt * (liq.qty / pos.bybit_qty if pos.bybit_qty else 1.0)
            if closed.qty:
                override += (pos.entry_bybit_price_usdt - closed.avg) * closed.qty - closed.fee
        legs = ExitLegs(upbit_price_krw=sold.avg, upbit_fee_krw=sold.fee, usdt_price_krw=bought.avg,
                        usdt_fee_krw=bought.fee, usdt_bought=bought.qty, bybit_price_usdt=short.avg,
                        bybit_fee_usdt=short.fee)
        pnl = compute_pnl(entry_legs_of(pos), legs, pos.funding_usdt, self.fees, bybit_leg_override_usdt=override)
        pos.status, pos.closed_at = PositionStatus.CLOSED, self._now()
        pos.exit_upbit_price_krw, pos.exit_upbit_fee_krw = sold.avg, sold.fee
        pos.exit_usdt_price_krw, pos.exit_usdt_fee_krw, pos.usdt_bought = bought.avg, bought.fee, bought.qty
        pos.exit_bybit_price_usdt, pos.exit_bybit_fee_usdt = short.avg, short.fee
        pos.exit_premium = premium_percent(sold.avg, short.avg, bought.avg) if sold.qty and bought.qty else None
        pos.upbit_leg_usdt, pos.bybit_leg_usdt = pnl.upbit_leg_usdt, pnl.bybit_leg_usdt
        pos.gross_pnl_usdt, pos.total_cost_usdt = pnl.gross_usdt, pnl.total_cost_usdt
        pos.net_pnl_usdt, pos.net_pnl_percent = pnl.net_usdt, pnl.net_percent
        session.commit()
        self.safety.record_success()
        return pnl

    # ------------------------------------------------------------------
    # 펀딩비 / 조회
    # ------------------------------------------------------------------
    def add_margin(self, session: Session, pos: Position, usdt: float) -> float:
        """
        포지션 증거금을 더 넣고 기록한다 (강제청산 가드). 계좌 안 이동이라 손익 변화는 없다

        bybit_margin_usdt 에 더하고(청산 때 증거금 손실 계산에 쓴다), 누적은 margin_added_usdt 에,
        강제청산가는 새 증거금으로 다시 계산한다 (실거래는 다음 스캔에 거래소 값으로 덮인다).
        Raises:
            OrderFailed: 거래소가 거절
        """
        done = self.ex.add_margin(session, pos.coin, usdt)
        pos.bybit_margin_usdt += done
        pos.margin_added_usdt = (pos.margin_added_usdt or 0.0) + done
        pos.liquidation_price = liquidation_price_from_margin(pos.entry_bybit_price_usdt, pos.bybit_qty,
                                                              pos.bybit_margin_usdt, self.mmr)
        session.commit()
        return done

    def apply_funding(self, session: Session, pos: Position, rate: float, mark_price: float,
                      funding_time_ms: int) -> float:
        """
        펀딩비 한 번 반영 (우리는 숏). 펀딩비 = 숏 개수 × 기준 가격 × 비율 (+ 받음 / − 냄)

        예) 740.7개 × 1.35 USDT × 0.01% = +0.1 USDT. 실거래는 거래소가 잔고에 반영하고, 여기서는 기록만.
        """
        amount = pos.bybit_qty * mark_price * rate
        pos.funding_usdt += amount
        self.ex.credit_funding(session, amount)
        session.add(FundingEvent(position_id=pos.id, rate=rate, mark_price=mark_price,
                                 amount_usdt=amount, funding_time_ms=funding_time_ms))
        session.commit()
        return amount

    @staticmethod
    def open_positions(session: Session) -> List[Position]:
        """관리 중인 포지션 (OPEN). 진입 중·청산 중·확인 필요 포지션은 sync 가 따로 처리"""
        return list(session.scalars(select(Position).where(Position.status == PositionStatus.OPEN)))

    @staticmethod
    def unfinished_positions(session: Session) -> List[Position]:
        """OPENING / CLOSING / ERROR (이 코인은 새로 진입하면 안 됨)"""
        return list(session.scalars(select(Position).where(Position.status.in_(PositionStatus.UNFINISHED))))

    def entries_allowed(self, session: Session) -> Tuple[bool, str]:
        """신규 진입을 해도 되나 (판단은 kimp/safety.py). 엔진이 진입 직전에 묻는다"""
        return self.safety.entries_allowed(session)

    # ------------------------------------------------------------------
    # 매 스캔: 송금 확인 → 덜 끝난 진입/청산 이어서 → 대조
    # ------------------------------------------------------------------
    def sync(self, session: Session, quotes: Dict[str, Quote], coins: Iterable[str] = (),
             refresh_account: bool = True) -> None:
        """
        스캔 맨 앞에서 하는 정리 — 덜 끝난 일을 이어서 하고 거래소와 대조한다

        순서가 중요하다. 새 포지션을 잡기 전에 **어긋난 것을 먼저 맞춰야** 한다.
            1) 잔고·포지션 캐시 비우기 (실거래, refresh_account=True 일 때). 소켓 시세로 자주 판단할 때는
               엔진이 SCAN_INTERVAL_SEC 마다 한 번만 True 로 준다 — 이 REST 조회가 요청 제한의 본체라서
            2) 송금이 도착했나 확인 (_poll_transfers)
            3) 덜 끝난 포지션(OPENING/CLOSING)을 이어서 처리.
               ERROR 는 건드리지 않는다 — 사람이 거래소에서 직접 정리해야 하는 상태다
            4) 거래소 실제 잔고·포지션이 DB 와 맞나 대조 (reconcile, 10분마다)

        Args:
            quotes: 코인별 지금 시세. 없는 코인은 이번 스캔에 건너뛴다 (시세 없이 주문하면 안 된다)
        """
        if refresh_account:
            self.ex.refresh()
            # 비운 김에 바로 받아 둔다 — 주문 경로(청산의 잔고·숏 확인)가 REST 를 기다리지 않게.
            # 선조회는 '미리' 일 뿐이라 실패해도 스캔을 멈추지 않는다 — 필요한 쪽이 그때 다시 받는다
            try:
                self.ex.prefetch([p.coin for p in self.open_positions(session)])
            except Exception as exc:
                self._log_repeat("prefetch", logging.WARNING, f"[계좌 선조회 실패] {exc} | 주문 때 다시 받습니다")
        self._poll_transfers(session)
        for pos in self.unfinished_positions(session):
            quote = quotes.get(pos.coin)
            if pos.status == PositionStatus.ERROR:
                continue  # 사람이 정리해야 함
            if self.ex.is_paper and quote is None:
                continue  # 페이퍼는 시세가 있어야 체결 계산 가능
            if not self._resolve_sent(session, pos):
                continue  # 아직 결과 모르는 주문 → 다음 스캔에 다시 조회
            if pos.status == PositionStatus.OPENING:
                sums = self._sums(session, pos)
                if {"open_short", "sell_usdt", "buy"} <= sums.keys():
                    # 세 단계는 끝났는데 그 뒤(크기 맞추기·기록)에서 멈췄던 경우 → 크기 맞추고 OPEN.
                    # 크기 맞추기 주문의 결과를 또 모르면 그대로 OPENING 에 두고 다음 스캔에 다시 조회한다.
                    try:
                        self._balance_hedge(session, pos, quote)
                        self._return_leftover_krw(session, pos, quote)
                    except OrderUnknown as exc:
                        self._order_unknown(session, pos, exc)
                        continue
                    self._finish_entry(session, pos)
                    logger.warning("[복구] #%d %s | 진입이 끝나 있어서 OPEN 으로", pos.id, pos.coin)
                else:
                    self._unwind_entry(session, pos, quote, "진입 도중 멈춤 (재시작/결과 확인)")
            elif pos.status == PositionStatus.CLOSING:
                pnl = self._continue_close(session, pos, quote)
                if pnl:
                    logger.warning("[청산 이어서 완료] #%d %s | 순손익 %+.2f USDT (%+.2f%%)",
                                   pos.id, pos.coin, pnl.net_usdt, pnl.net_percent)
        now = self._now().timestamp()
        if now - self._last_reconcile >= self.reconcile_interval_sec:
            self._last_reconcile = now
            issues = self.reconcile(session, set(coins) | set(quotes))
            if issues:
                self.safety.block("reconcile", "대조 불일치: " + " / ".join(issues))
            else:
                self.safety.clear("reconcile")

    def vanished_shorts(self, session: Session) -> List[Position]:
        """
        OPEN 포지션인데 거래소에 숏이 없는 것들 (강제청산 또는 누가 손으로 닫음)

        10분마다 도는 대조는 신규 진입만 막는데, 그 사이 코인은 헷지 없이 가격에 노출된다.
        그래서 엔진이 매 스캔 이걸 물어서(코인당 조회 1번) 바로 코인을 판다. 정리는 엔진의 _close 가 한다 —
        거기서 재진입 대기와 "[청산 LIQUIDATION]" 요약 로그가 같이 나간다 (2026-10-24 IRYS #4 에서 빠졌던 것).
        손익은 강제청산과 같게 '증거금 전액 손실' 로 적는다 — 사람이 손으로 닫은 거라면 실제로는
        증거금이 돌아와 있으니 기록이 더 비관적일 수 있다.
        """
        found = []
        for pos in self.open_positions(session):
            if pos.bybit_qty <= 0:
                continue
            try:
                short = self.ex.short_qty(session, pos.coin)
            except Exception as exc:
                self._log_repeat(f"short-check-{pos.id}", logging.WARNING,
                                 f"#{pos.id} {pos.coin} 거래소 숏 개수 조회 실패 (다음에 다시): {exc}")
                continue
            if short is not None and short <= pos.bybit_qty * 1e-9:
                found.append(pos)  # 숏이 없다 (개수가 '다른' 것은 대조가 본다)
        return found

    def reconcile(self, session: Session, coins: Iterable[str]) -> List[str]:
        """
        DB 열린 포지션 ↔ 실제 숏 개수·업비트 코인 개수 (다르면 문제 목록)

        - 숏: DB 합계와 실제가 0.1% 넘게 다르면 문제 (예: DB 100개, 거래소 0개 → 누가 손으로 닫았거나 강제청산)
        - 업비트 코인: 실제가 DB 합계보다 0.5% 넘게 적으면 문제 (더 많은 건 원래 갖고 있던 코인일 수 있어 괜찮음)
        - 진입 중·청산 중인 코인은 건너뜀 (sync 가 처리 중)
        """
        busy = {p.coin for p in self.unfinished_positions(session)}
        want_short: Dict[str, float] = {}
        want_coin: Dict[str, float] = {}
        for p in self.open_positions(session):
            want_short[p.coin] = want_short.get(p.coin, 0.0) + p.bybit_qty
            want_coin[p.coin] = want_coin.get(p.coin, 0.0) + p.upbit_qty
        issues = []
        for coin in sorted(set(coins) | set(want_short)):
            if coin in busy:
                continue
            short = self.ex.short_qty(session, coin)
            want = want_short.get(coin, 0.0)
            if short is not None and want > 0 and short <= want * 1e-9:
                continue  # 숏이 아예 사라진 것은 '불일치' 가 아니라 강제청산 → 엔진이 이 스캔에 바로 정리한다
            if short is not None and abs(short - want) > max(1e-9, want * 0.001):
                issues.append(f"{coin} 바이비트 숏 DB {want:g}개 / 실제 {short:g}개")
            have = self.ex.coin_total(session, coin)
            need = want_coin.get(coin, 0.0)
            if need > 0 and have is not None and have < need * 0.995:
                issues.append(f"{coin} 업비트 코인 DB {need:g}개 / 실제 {have:g}개")
        return issues

    # ------------------------------------------------------------------
    # 송금
    # ------------------------------------------------------------------
    @staticmethod
    def pending_transfers(session: Session) -> List[Transfer]:
        """
        아직 도착 확인이 안 된 송금 (REQUESTED)

        하나라도 있으면 새 송금을 보내지 않는다 — 같은 돈을 두 번 보내는 사고를 막는다.
        """
        return list(session.scalars(select(Transfer).where(Transfer.status == TransferStatus.REQUESTED)))

    @staticmethod
    def transfers(session: Session) -> List[Transfer]:
        """실패하지 않은 송금 전부 (시간 순)"""
        return list(session.scalars(select(Transfer).where(Transfer.status != TransferStatus.FAILED)
                                    .order_by(Transfer.id)))

    def transfer(self, session: Session, amount_usdt: float, fee_usdt: float, reason: str = "") -> float:
        """
        송금 요청: amount 가 + 면 업비트 → 바이비트, − 면 바이비트 → 업비트

        보내는 쪽 '남는 USDT' 까지만, 하루 한도까지만, 최소 금액 이상만. 송금 중이면 안 보냄.
        예) 업비트 → 바이비트 30 USDT, 수수료 1 → 업비트 −30, (도착하면) 바이비트 +29

        Returns:
            실제로 요청한 양 (부호 포함). 안 보냈으면 0
        """
        if self.pending_transfers(session):
            logger.info("송금 중인 건이 있어서 이번엔 안 보냄")
            return 0.0
        waiting = getattr(self, "_policy_block_until", 0.0) - self._now().timestamp()
        if waiting > 0:
            logger.debug("거래소 규정으로 송금 보류 중 (%.0f분 남음)", waiting / 60)
            return 0.0
        to_bybit = amount_usdt > 0
        send = min(abs(amount_usdt), self.balance(session, UPBIT_USDT if to_bybit else BYBIT_USDT))
        if self.transfer_max_usdt_per_day is not None:
            since = kst_midnight_utc(self._now().timestamp())
            today = session.scalar(select(func.coalesce(func.sum(Transfer.amount_usdt), 0.0)).where(
                Transfer.status != TransferStatus.FAILED, Transfer.requested_at >= since)) or 0.0
            send = min(send, self.transfer_max_usdt_per_day - today)
        send = math.floor(send * 10 ** TRANSFER_DECIMALS) / 10 ** TRANSFER_DECIMALS  # 거래소 자리수 제한
        if send <= max(fee_usdt, self.transfer_min_usdt) or send <= 0:
            return 0.0
        t = Transfer(direction="upbit->bybit" if to_bybit else "bybit->upbit", amount_usdt=send, fee_usdt=fee_usdt,
                     network=self.network, status=TransferStatus.REQUESTED, reason=reason, requested_at=self._now())
        session.add(t)
        session.commit()
        try:
            t.withdraw_id = self.ex.withdraw(session, to_bybit, send, self.network)
        except OrderFailed as exc:
            t.status, t.note = TransferStatus.FAILED, str(exc)[:200]
            session.commit()
            if is_policy_reject(str(exc)):
                # 거래소 규정(출금지연제 등) → 연속 실패로 세지 않는다. 그래야 신규 진입이 막히지 않는다
                self._policy_block_until = self._now().timestamp() + POLICY_RETRY_SEC
                logger.error("[송금 보류] %s | 금액 %s USDT | 거래소 규정으로 지금은 안 됨: %s | "
                             "%.0f분 뒤 다시 시도 (업비트 출금지연제면 원화 입금 후 24시간)",
                             dir_label(t.direction), f"{send:,.2f}", exc, POLICY_RETRY_SEC / 60)
            else:
                self.safety.record_failure(f"송금: {exc}")
                logger.critical("[송금 실패] #%d %s | 금액 %s USDT | %s", t.id, dir_label(t.direction), f"{send:,.2f}", exc)
            return 0.0
        session.commit()
        logger.warning("[송금 요청 · %s] #%d %s | 금액 %s USDT (수수료 %g) | 망 %s",
                       reason or "-", t.id, dir_label(t.direction), f"{send:,.2f}", fee_usdt, self.network)
        self._poll_transfers(session)
        return send if to_bybit else -send

    def _poll_transfers(self, session: Session) -> None:
        """송금 중인 건이 받는 쪽에 들어왔는지 확인. 늦으면 한 번 긴급 알림"""
        for t in self.pending_transfers(session):
            try:
                arrived = self.ex.transfer_arrived(session, t)
            except Exception as exc:  # 조회 실패는 다음에 다시 (거래소가 한참 안 되면 5분에 한 번만 알림)
                self._log_repeat(f"transfer-{t.id}", logging.WARNING,
                                 f"[송금 확인 실패] #{t.id} {dir_label(t.direction)} | {exc} | 다음 스캔에 다시")
                continue
            self._last_repeat.pop(f"transfer-{t.id}", None)
            requested = t.requested_at if t.requested_at.tzinfo else t.requested_at.replace(tzinfo=timezone.utc)
            minutes = (self._now() - requested).total_seconds() / 60
            if arrived:
                t.status, t.confirmed_at = TransferStatus.CONFIRMED, self._now()
                logger.warning("[송금 도착] #%d %s | 받은 금액 %s USDT | 걸린 시간 %.0f분",
                               t.id, dir_label(t.direction), f"{t.amount_usdt - t.fee_usdt:,.2f}", minutes)
            elif arrived is False:
                t.status = TransferStatus.FAILED
                logger.critical("[송금 실패] #%d %s | 금액 %s USDT | 거래소 출금 내역을 확인하세요",
                                t.id, dir_label(t.direction), f"{t.amount_usdt:,.2f}")
            else:
                if minutes > self.transfer_alert_minutes and not t.alerted:
                    t.alerted = 1
                    logger.critical("[송금 지연] #%d %s | 금액 %s USDT | %.0f분째 도착 안 함 | 출금·입금 내역을 확인하세요",
                                    t.id, dir_label(t.direction), f"{t.amount_usdt:,.2f}", minutes)
            session.commit()


def broker_kwargs(settings, safety: Optional[Safety] = None) -> dict:
    """설정 → HedgeBroker 공통 옵션 (실시간 봇과 시뮬레이션이 같은 값을 쓰게)"""
    return dict(safety=safety, network=settings.transfer_network_label, transfer_min_usdt=settings.transfer_min_usdt,
                transfer_max_usdt_per_day=settings.transfer_max_usdt_per_day,
                transfer_alert_minutes=settings.transfer_alert_minutes,
                reconcile_interval_sec=settings.reconcile_interval_sec,
                hedge_tolerance_percent=settings.hedge_tolerance_percent,
                coin_order_type=settings.upbit_coin_order_type)


class PaperBroker(HedgeBroker):
    """페이퍼 트레이딩: HedgeBroker + PaperExecutor (예전 사용법 그대로: PaperBroker(fees, 0.3, leverage=…))"""

    def __init__(self, fees: FeeSettings, max_slippage_percent: float, leverage: float = 1.0,
                 mmr: float = 0.005, now_fn: Callable[[], datetime] = utcnow,
                 upbit_min_order_krw: float = 5_000.0, **kwargs):
        super().__init__(PaperExecutor(fees, leverage, upbit_min_order_krw), fees, max_slippage_percent,
                         leverage=leverage, mmr=mmr, now_fn=now_fn, **kwargs)
